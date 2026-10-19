from __future__ import annotations

import asyncio
import contextvars
import json
import os
import select
import socket
import subprocess
import sys
import time
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
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
    def test_windows_deadlines_are_shared_by_wait_session_and_http(self) -> None:
        original_wait_for = asyncio.wait_for
        for transport in ("stdio", "http"):
            for name, expected in (("create_calendar_event", 105), ("schedule_reminder", 105),
                                   ("get_upcoming_deadlines", 105), ("send_notification", 45), ("read", 30)):
                session = AsyncMock()
                session.__aenter__.return_value = session
                session.call_tool.return_value = response({"ok": True})

                @asynccontextmanager
                async def stdio(_parameters):
                    yield "read", "write"

                @asynccontextmanager
                async def http(_url, **kwargs):
                    self.assertEqual(kwargs["timeout"], timedelta(seconds=expected))
                    self.assertEqual(kwargs["sse_read_timeout"], timedelta(seconds=expected))
                    yield "read", "write", None

                config = FastMCPClientConfig(transport=transport, url="http://localhost/mcp" if transport == "http" else None)
                with self.subTest(transport=transport, name=name), patch.dict(os.environ, {}, clear=True), \
                     patch("kwikledgers_langgraph.fastmcp_client.stdio_client", stdio), \
                     patch("kwikledgers_langgraph.fastmcp_client.streamablehttp_client", http), \
                     patch("kwikledgers_langgraph.fastmcp_client.ClientSession", return_value=session) as constructor, \
                     patch("kwikledgers_langgraph.fastmcp_client.asyncio.wait_for", wraps=original_wait_for) as wait:
                    self.assertEqual(FastMCPClient(config).call(name, {}), {"ok": True})
                    self.assertEqual(wait.call_args.args[1], expected)
                    self.assertEqual(constructor.call_args.kwargs["read_timeout_seconds"], timedelta(seconds=expected))
                    session.initialize.assert_awaited_once()
                    session.call_tool.assert_awaited_once_with(name, arguments={"arguments": {}})

    def test_windows_deadlines_use_bounded_server_environment_and_config_floor(self) -> None:
        for raw, outlook, notification in (("120", 135, 245), (" 3 ", 18, 11), ("1", 16, 7),
                                           ("301", 315, 605), ("999999", 315, 605),
                                           ("", 105, 45), ("secret", 105, 45), ("0", 105, 45),
                                           ("-1", 105, 45), ("nan", 105, 45), ("inf", 105, 45), ("1.5", 105, 45),
                                           ("9" * 5000, 105, 45)):
            environment = {"KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS": raw,
                           "KWIKLEDGERS_WINDOWS_NOTIFICATION_TIMEOUT_SECONDS": raw}
            for transport in ("stdio", "http"):
                with self.subTest(raw=raw, transport=transport), patch.dict(os.environ, environment, clear=True):
                    client = FastMCPClient(FastMCPClientConfig(
                        transport=transport, url="http://localhost/mcp" if transport == "http" else None,
                        timeout_seconds=0.01,
                    ))
                    for name in ("create_calendar_event", "schedule_reminder", "get_upcoming_deadlines"):
                        self.assertEqual(client._timeout_for_capability(name), outlook)
                    self.assertEqual(client._timeout_for_capability("send_notification"), notification)
                    self.assertEqual(client._timeout_for_capability("execute"), 0.01)
        with patch.dict(os.environ, {}, clear=True):
            for configured, outlook, notification in ((1, 105, 45), (60, 105, 60), (240, 240, 240)):
                client = FastMCPClient(FastMCPClientConfig(timeout_seconds=configured))
                self.assertEqual(client._timeout_for_capability("create_calendar_event"), outlook)
                self.assertEqual(client._timeout_for_capability("send_notification"), notification)
                self.assertEqual(client._timeout_for_capability("read"), configured)

    def test_windows_deadlines_use_exact_explicit_stdio_environment(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS": "300",
                                    "KWIKLEDGERS_WINDOWS_NOTIFICATION_TIMEOUT_SECONDS": "300"}):
            client = FastMCPClient(FastMCPClientConfig(env={}))
            self.assertEqual(client._timeout_for_capability("create_calendar_event"), 105)
            self.assertEqual(client._timeout_for_capability("send_notification"), 45)
            client = FastMCPClient(FastMCPClientConfig(env={
                "KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS": "120",
                "KWIKLEDGERS_WINDOWS_NOTIFICATION_TIMEOUT_SECONDS": "30",
            }))
            self.assertEqual(client._timeout_for_capability("schedule_reminder"), 135)
            self.assertEqual(client._timeout_for_capability("send_notification"), 65)

    def test_environment_client_timeout_is_safe_and_bounded(self) -> None:
        for raw, expected in (("secret", 30), ("", 30), ("nan", 30), ("inf", 30), ("-inf", 30),
                              ("0", 30), ("-1", 30), (" 0.5 ", 0.5), ("240", 240), ("999999", 3600)):
            with self.subTest(raw=raw), patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_TIMEOUT_SECONDS": raw}, clear=True):
                self.assertEqual(FastMCPClientConfig.from_environment().timeout_seconds, expected)

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
            self.assertTrue(raised.exception.execution_outcome_unknown)

    def test_config_rejects_invalid_transport_url_timeout_and_args(self) -> None:
        for options in ({"transport": "bad"}, {"timeout_seconds": 0}, {"timeout_seconds": float("inf")},
                        {"transport": "http"}, {"transport": "http", "url": "http://user:secret@host/mcp"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                FastMCPClientConfig(**options)
        with patch.dict("os.environ", {"KWIKLEDGERS_FASTMCP_ARGS": '{}'}), self.assertRaises(ValueError):
            FastMCPClientConfig.from_environment()

    def test_unauthenticated_http_is_limited_to_loopback(self) -> None:
        for host in ("example.com", "192.168.1.5", "localhost.example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                FastMCPClientConfig(transport="http", url=f"https://{host}/mcp")
        for host in ("localhost", "127.0.0.1", "[::1]"):
            with self.subTest(host=host):
                self.assertEqual(FastMCPClientConfig(transport="http", url=f"http://{host}/mcp").transport, "http")

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

    def test_branch_mutation_requires_token_and_keeps_approved_technical_fields(self) -> None:
        client = RecordingClient()
        request = {"story_id": 42, "repository_name": "repo", "source_branch": "feature/test",
                   "expected_revisions": {"42": 5}, "pending_approval": {"approved": True}}
        for adapter, method, capability in (
            (FastMCPStoryBranchAssociationAdapter(client), "associate_story_branch", "associate_story_branch"),
        ):
            with self.assertRaises(ValueError):
                getattr(adapter, method)(request)
            approved = {**request, "execution_capability": "token"}
            getattr(adapter, method)(approved)
            self.assertEqual(client.calls[-1], (capability, {
                **technical_arguments_for_capability(capability, approved), "execution_capability": "token",
            }))
        self.assertEqual(len(client.calls), 1)

    def test_isolated_pr_refuses_and_closure_requires_approved_preview(self) -> None:
        client = RecordingClient()
        for token in (None, "token"):
            with self.subTest(method="create_story_pull_request", token=token), self.assertRaisesRegex(RuntimeError, "requires lifecycle"):
                FastMCPStoryPullRequestAdapter(client).create_story_pull_request(
                    {"execution_capability": token},
                )
        for token in (None, "token"):
            with self.subTest(method="close_task", token=token), self.assertRaisesRegex(RuntimeError, "allowed approved preview"):
                FastMCPTaskClosureAdapter(client).close_task(
                    {"execution_capability": token},
                )
        self.assertEqual(client.calls, [])

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

    def test_closure_mapper_preserves_empty_remaining_work(self) -> None:
        for remaining in (None, 0, 2):
            client = RecordingClient()
            request = {"task_id": 9, "execution_capability": "token", "preview": {
                "task_id": 9, "closure_allowed": True, "completed_work_hours": 3,
                "remaining_work_hours": remaining, "task_revision": 4,
            }}
            result = technical_arguments_for_capability("update_work_item_effort", request)
            arguments = {"work_item_id": 9, "completed_work_hours": 3, "status": "Closed",
                         "expected_revision": 4}
            if remaining is not None:
                arguments["remaining_work_hours"] = remaining
            self.assertEqual(result, arguments)
            self.assertEqual(client.calls, [])

    def test_closure_preview_requires_fresh_native_revision(self) -> None:
        client = RecordingClient({"id": 9, "revision": 4})
        adapter = FastMCPTaskClosureAdapter(client)
        request = {"task_id": 9, "expected_revision": 999}
        self.assertEqual(adapter.preview_task_closure(request),
                         {"task_id": 9, "task_revision": 4, "execution_requires_lifecycle": "task_cycle"})
        client.data = {"id": 9, "revision": 5}
        self.assertEqual(adapter.preview_task_closure(request)["task_revision"], 5)
        self.assertEqual(client.calls, [("get_story_details", {"story_id": 9})] * 2)
        for data in ({"message": "revision: 4"}, {"id": 9, "revision": True},
                     {"id": 9, "revision": "4"}, {"id": 9, "revision": 0}, {"id": 8, "revision": 4}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                FastMCPTaskClosureAdapter(RecordingClient(data)).preview_task_closure(request)

    def test_association_revisions_are_taken_from_approved_preview(self) -> None:
        for capability in ("associate_story_branch", "create_story_pull_request"):
            request = {"story_id": 42, "repository_name": "repo", "source_branch": "feature/test",
                       "preview": {"expected_revisions": {"42": 7, "43": 3}}}
            arguments = technical_arguments_for_capability(capability, request)
            self.assertEqual(arguments["expected_revisions"], {"42": 7, "43": 3})
            self.assertNotIn("preview", arguments)
            for update in ({"expected_revisions": {"42": 8}}, {"expected_revision": 8},
                           {"preview": {}}, {"preview": {"expected_revisions": {"42": True}}}):
                with self.subTest(capability=capability, update=update), self.assertRaises(ValueError):
                    technical_arguments_for_capability(capability, {**request, **update})

    def test_invalid_closure_never_calls_server(self) -> None:
        client = RecordingClient()
        preview = {"closure_allowed": True, "completed_work_hours": 3, "task_revision": 4}
        for update in ({"closure_allowed": False}, {"task_id": 8}, {"task_revision": None},
                       {"completed_work_hours": None}, {"completed_work_hours": float("nan")}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                technical_arguments_for_capability("update_work_item_effort",
                    {"task_id": 9, "preview": {**preview, **update}, "execution_capability": "token"})
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
        self.assertIsInstance(client.calls[1][1]["message"], str)
        self.assertEqual(client.calls[1][1]["message"].count("Story points restantes"), 1)

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