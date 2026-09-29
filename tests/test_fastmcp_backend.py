from __future__ import annotations

import asyncio
import contextvars
import json
import select
import socket
import subprocess
import sys
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kwikledgers_langgraph.fastmcp_adapters import (
    FastMCPDailySummaryProvider, FastMCPNotificationAdapter, FastMCPSprintStoriesProvider,
    FastMCPStoryBranchAssociationAdapter, FastMCPStoryPullRequestAdapter,
    FastMCPStoryStatusTransitionAdapter, FastMCPTaskClosureAdapter, FastMCPTrackingSyncAdapter,
    technical_arguments_for_capability,
)
from kwikledgers_langgraph.fastmcp_client import FastMCPClient, FastMCPClientConfig, FastMCPClientError


def response(data: object = None, *, error: bool = False, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(isError=error, structuredContent=data, content=[SimpleNamespace(type="text", text=text)])


class FastMCPClientTestCase(unittest.TestCase):
    def test_normalizes_structured_and_text_envelopes(self) -> None:
        envelope = {"capability": "read", "data": {"count": 2}}
        for result in (response(envelope), response(text=json.dumps(envelope))):
            self.assertEqual(FastMCPClient._normalize(result, "read"), {"count": 2})

    def test_rejects_error_invalid_json_and_invalid_root(self) -> None:
        results = [response(error=True, text="secret"), response(text="secret"), response([]),
                   response({"capability": "wrong", "data": {}}), response({"capability": "read", "data": []})]
        for result in results:
            with self.subTest(result=result), self.assertRaises(FastMCPClientError) as raised:
                FastMCPClient._normalize(result, "read")
            self.assertNotIn("secret", str(raised.exception))

    def test_both_transports_initialize_real_sdk_session_interface(self) -> None:
        session = AsyncMock()
        session.call_tool.return_value = response({"capability": "read", "data": {"ok": True}})
        session.__aenter__.return_value = session

        @asynccontextmanager
        async def stdio(_parameters):
            yield "read", "write"

        @asynccontextmanager
        async def http(_url, **_kwargs):
            yield "read", "write", None

        for config in (FastMCPClientConfig(), FastMCPClientConfig(transport="http", url="http://localhost/mcp")):
            with patch("kwikledgers_langgraph.fastmcp_client.stdio_client", stdio), \
                 patch("kwikledgers_langgraph.fastmcp_client.streamablehttp_client", http), \
                 patch("kwikledgers_langgraph.fastmcp_client.ClientSession", return_value=session):
                self.assertEqual(FastMCPClient(config).call("read", {"id": 4}), {"ok": True})
        self.assertEqual(session.initialize.await_count, 2)
        session.call_tool.assert_awaited_with("read", arguments={"arguments": {"id": 4}})

    def test_sync_call_inside_active_event_loop(self) -> None:
        client = FastMCPClient()
        marker = contextvars.ContextVar("marker", default="missing")
        marker.set("caller")
        async def request(*_args):
            return response({"marker": marker.get()})
        with patch.object(client, "_request", request):
            async def caller():
                return client.call("read", {})
            self.assertEqual(asyncio.run(caller()), {"marker": "caller"})

    def test_timeout_and_transport_failure_do_not_leak_payload(self) -> None:
        client = FastMCPClient(FastMCPClientConfig(timeout_seconds=0.01))
        async def stalled(*_args):
            await asyncio.Event().wait()
        for operation in (stalled, AsyncMock(side_effect=RuntimeError("secret"))):
            with patch.object(client, "_request", operation), self.assertRaises(FastMCPClientError) as raised:
                client.call("execute", {"execution_capability": "secret"})
            self.assertNotIn("secret", str(raised.exception))

    def test_config_rejects_invalid_transport_url_timeout_and_args(self) -> None:
        for options in ({"transport": "bad"}, {"timeout_seconds": 0}, {"timeout_seconds": float("inf")},
                        {"transport": "http"}, {"transport": "http", "url": "http://user:secret@host/mcp"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                FastMCPClientConfig(**options)
        with patch.dict("os.environ", {"KWIKLEDGERS_FASTMCP_ARGS": '{}'}), self.assertRaises(ValueError):
            FastMCPClientConfig.from_environment()

    def test_timeout_closes_transport_and_session_contexts(self) -> None:
        closed = []
        @asynccontextmanager
        async def transport(_parameters):
            try:
                yield "read", "write"
            finally:
                closed.append("transport")
        class Session:
            def __init__(self, *_args, **_kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                closed.append("session")
            async def initialize(self):
                pass
            async def call_tool(self, *_args, **_kwargs):
                await asyncio.Event().wait()
        with patch("kwikledgers_langgraph.fastmcp_client.stdio_client", transport), \
             patch("kwikledgers_langgraph.fastmcp_client.ClientSession", Session), \
             self.assertRaises(FastMCPClientError):
            FastMCPClient(FastMCPClientConfig(timeout_seconds=0.01)).call("execute", {})
        self.assertEqual(closed, ["session", "transport"])

    def test_stdio_forwards_application_environment_or_explicit_environment(self) -> None:
        received = []
        @asynccontextmanager
        async def transport(parameters):
            received.append(parameters.env)
            yield "read", "write"
        for env in (None, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "explicit-key"}):
            client = FastMCPClient(FastMCPClientConfig(env=env))
            with patch.dict("os.environ", {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "inherited-key"}), \
                 patch("kwikledgers_langgraph.fastmcp_client.stdio_client", transport), \
                 patch.object(client, "_call_session", AsyncMock(return_value=response({"ok": True}))):
                self.assertEqual(client.call("read", {}), {"ok": True})
        self.assertEqual(received[0]["KWIKLEDGERS_FASTMCP_EXECUTION_KEY"], "inherited-key")
        self.assertEqual(received[1], {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "explicit-key"})


MCP_TEST_SERVER = '''
from mcp.server.fastmcp import FastMCP
server = FastMCP("backend-contract-test", host="127.0.0.1", port=__PORT__)
@server.tool()
def get_my_daily_summary(arguments: dict) -> dict:
    return {"capability": "get_my_daily_summary", "data": {"counts": {"assigned_items": 2}, "received": arguments}}
@server.tool()
def failing(arguments: dict) -> dict:
    raise ValueError("server-secret")
server.run(transport=__TRANSPORT__)
'''


class FastMCPTransportIntegrationTestCase(unittest.TestCase):
    def test_real_stdio_session_and_tool_error(self) -> None:
        source = MCP_TEST_SERVER.replace("__PORT__", "8000").replace("__TRANSPORT__", '"stdio"')
        client = FastMCPClient(FastMCPClientConfig(command=sys.executable, args=("-c", source), timeout_seconds=10))
        result = client.call("get_my_daily_summary", {"probe": "stdio"})
        self.assertEqual(result["received"], {"probe": "stdio"})
        self.assertEqual(result["counts"], {"assigned_items": 2})
        with self.assertRaises(FastMCPClientError) as raised:
            client.call("failing", {})
        self.assertNotIn("server-secret", str(raised.exception))

    def test_real_http_session(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        source = MCP_TEST_SERVER.replace("__PORT__", str(port)).replace("__TRANSPORT__", '"streamable-http"')
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", source], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0,
        )
        try:
            deadline = time.monotonic() + 10
            ready = False
            while time.monotonic() < deadline and process.poll() is None:
                readable, _, _ = select.select([process.stderr], [], [], max(0, deadline - time.monotonic()))
                if readable and b"Uvicorn running on" in process.stderr.readline():
                    ready = True
                    break
            self.assertTrue(ready, "Local MCP HTTP test server did not start.")
            client = FastMCPClient(FastMCPClientConfig(transport="http", url=f"http://127.0.0.1:{port}/mcp", timeout_seconds=10))
            self.assertEqual(client.call("get_my_daily_summary", {"probe": "http"})["received"], {"probe": "http"})
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stderr.close()


class RecordingClient:
    def __init__(self, data: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.data = data if data is not None else {"ok": True}

    def call(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return self.data


class FastMCPAdaptersTestCase(unittest.TestCase):
    def test_daily_and_sprint_are_reads_without_workflow_fields(self) -> None:
        client = RecordingClient()
        FastMCPDailySummaryProvider(client).fetch_daily_summary()
        FastMCPSprintStoriesProvider(client).fetch_sprint_stories_detailed({"approved": True})
        self.assertEqual(client.calls, [("get_my_daily_summary", {}), ("get_sprint_stories_detailed", {})])

    def test_branch_and_pr_previews_exclude_tokens_and_workflow_state(self) -> None:
        client = RecordingClient()
        request = {"story_id": 42, "repository_name": "repo", "source_branch": "feature/test",
                   "execution_capability": "secret", "preview": {}, "pending_approval": {"approved": True},
                   "expected_revision": 5}
        FastMCPStoryBranchAssociationAdapter(client).preview_story_branch_association(request)
        FastMCPStoryPullRequestAdapter(client).preview_story_pull_request(request)
        for _name, arguments in client.calls:
            self.assertEqual(arguments, {"story_id": 42, "repository_name": "repo", "source_branch": "feature/test"})
        self.assertIn("execution_capability", request)

    def test_mutations_require_token_and_keep_approved_technical_fields(self) -> None:
        client = RecordingClient()
        request = {"story_id": 42, "repository_name": "repo", "source_branch": "feature/test",
                   "expected_revisions": {"42": 5}, "pending_approval": {"approved": True}}
        for adapter, method, capability in (
            (FastMCPStoryBranchAssociationAdapter(client), "associate_story_branch", "associate_story_branch"),
            (FastMCPStoryPullRequestAdapter(client), "create_story_pull_request", "create_story_pull_request"),
        ):
            with self.assertRaises(ValueError):
                getattr(adapter, method)(request)
            approved = {**request, "execution_capability": "token"}
            getattr(adapter, method)(approved)
            self.assertEqual(client.calls[-1], (capability, {
                **technical_arguments_for_capability(capability, approved), "execution_capability": "token",
            }))
        self.assertEqual(len(client.calls), 2)

    def test_status_preview_uses_native_revision_not_synthetic_text(self) -> None:
        client = RecordingClient({"id": 42, "rev": 7, "fields": {"System.Title": "Story", "System.State": "Desenvolvimento"}})
        preview = FastMCPStoryStatusTransitionAdapter(client).preview_story_status_transition({"story_id": 42})
        self.assertEqual(preview["story_revision"], 7)
        self.assertEqual(preview["current_status"], "Desenvolvimento")
        self.assertEqual(client.calls, [("get_story_details", {"story_id": 42})])
        for data in ({"message": "Status: New"}, {"revision": True, "status": "New"},
                     {"id": 43, "rev": 7, "status": "New"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                FastMCPStoryStatusTransitionAdapter(RecordingClient(data)).preview_story_status_transition({"story_id": 42})

    def test_status_execute_maps_target_and_revision_without_preview(self) -> None:
        client = RecordingClient()
        request = {"story_id": 42, "target_status": "Aguardando PR", "execution_capability": "token",
                   "preview": {"story_revision": 7, "current_status": "Desenvolvimento"}}
        result = FastMCPStoryStatusTransitionAdapter(client).update_story_status(request)
        self.assertEqual(client.calls, [("update_story_status", {
            "story_id": 42, "status": "Aguardando PR", "expected_revision": 7, "execution_capability": "token",
        })])
        self.assertEqual(result["current_status"], "Aguardando PR")
        for revision in (None, "synthetic", True, 0, 8):
            invalid = {**request, "expected_revision": revision, "preview": {"story_revision": revision if revision != 8 else 7}}
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                FastMCPStoryStatusTransitionAdapter(client).update_story_status(invalid)
        self.assertEqual(len(client.calls), 1)

    def test_closure_is_one_atomic_call_preserving_empty_remaining_work(self) -> None:
        for remaining in (None, 0, 2):
            client = RecordingClient()
            request = {"task_id": 9, "execution_capability": "token", "preview": {
                "task_id": 9, "closure_allowed": True, "completed_work_hours": 3,
                "remaining_work_hours": remaining, "task_revision": 4,
            }}
            result = FastMCPTaskClosureAdapter(client).close_task(request)
            arguments = {"work_item_id": 9, "completed_work_hours": 3, "status": "Closed",
                         "expected_revision": 4, "execution_capability": "token"}
            if remaining is not None:
                arguments["remaining_work_hours"] = remaining
            self.assertEqual(client.calls, [("update_work_item_effort", arguments)])
            self.assertEqual(result["status"], "closed")

    def test_invalid_closure_never_calls_server(self) -> None:
        client = RecordingClient()
        preview = {"closure_allowed": True, "completed_work_hours": 3, "task_revision": 4}
        for update in ({"closure_allowed": False}, {"task_id": 8}, {"task_revision": None},
                       {"completed_work_hours": None}, {"completed_work_hours": float("nan")}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                FastMCPTaskClosureAdapter(client).close_task({"task_id": 9, "preview": {**preview, **update}, "execution_capability": "token"})
        self.assertEqual(client.calls, [])

    def test_internal_tracking_and_notification_tokens_bind_exact_arguments(self) -> None:
        client = RecordingClient()
        issued = []
        def issue(name, arguments):
            issued.append((name, arguments))
            return "internal-token"
        summary = {"counts": {"blocked_items": 1}, "remaining_story_points": 3}
        FastMCPTrackingSyncAdapter(client, issue).sync_daily_tracking(summary, "langgraph")
        FastMCPNotificationAdapter(client, issue).send_daily_summary_notification(summary)
        for (name, arguments), (issued_name, issued_arguments) in zip(client.calls, issued):
            self.assertEqual(name, issued_name)
            self.assertEqual(arguments, {**issued_arguments, "execution_capability": "internal-token"})
        self.assertEqual(json.loads(client.calls[0][1]["summary"]), summary)
        self.assertEqual(client.calls[1][1]["urgency"], "high")

    def test_internal_effects_fail_without_token_and_accept_explicit_token(self) -> None:
        client = RecordingClient()
        tracking = FastMCPTrackingSyncAdapter(client)
        notifications = FastMCPNotificationAdapter(client)
        with self.assertRaises(ValueError):
            tracking.sync_daily_tracking({}, "langgraph")
        with self.assertRaises(ValueError):
            notifications.send_daily_summary_notification({})
        self.assertEqual(client.calls, [])
        tracking.sync_daily_tracking({}, "langgraph", execution_capability="token")
        notifications.send_daily_summary_notification({}, execution_capability="token")
        self.assertEqual(len(client.calls), 2)