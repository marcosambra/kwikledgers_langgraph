from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.adapters import (
    StoryBranchAssociationPorts, StoryPullRequestPorts, StoryStatusTransitionPorts, TaskClosurePorts,
)
from kwikledgers_langgraph.fastmcp_adapters import (
    FastMCPStoryBranchAssociationAdapter, FastMCPStoryPullRequestAdapter,
    FastMCPStoryStatusTransitionAdapter, FastMCPTaskClosureAdapter, technical_arguments_for_capability,
)
from kwikledgers_langgraph.graphs.mutable_flow import run_execute_step
from kwikledgers_langgraph.guardrails import (
    OperationKind, build_approval_confirmation, build_bound_approval, evaluate_operation, load_resume_confirmation,
)
from kwikledgers_langgraph.lifecycle_adapters import ExplicitLocalApproval, canonical
from kwikledgers_langgraph.observability import build_observability_context
from kwikledgers_langgraph.state_persistence import write_state_file

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None
APPROVAL_KEY = "independent-human-approval-key-32-bytes"
EXECUTION_KEY = "independent-execution-capability-key-32-bytes"

if LANGGRAPH_AVAILABLE:
    from kwikledgers_langgraph import story_branch_cli, story_pr_cli, story_status_cli, task_closure_cli
    from kwikledgers_langgraph.graphs.story_branch_association import build_story_branch_association_graph
    from kwikledgers_langgraph.graphs.story_pull_request import build_story_pull_request_graph
    from kwikledgers_langgraph.graphs.story_status_transition import build_story_status_transition_graph
    from kwikledgers_langgraph.graphs.task_closure import build_task_closure_graph


class VerifyingClient:
    def __init__(self) -> None:
        self.revision = 7
        self.calls: list[tuple[str, dict]] = []
        self.executions: list[tuple[str, dict, dict]] = []

    def call(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, deepcopy(arguments)))
        if name.startswith("preview_"):
            return {**arguments, "expected_revisions": {"42": self.revision, "43": 3}}
        if name == "get_story_details":
            return {"id": arguments["story_id"], "rev": self.revision,
                    "fields": {"System.State": "Desenvolvimento", "System.WorkItemType": "User Story"}}
        payload = dict(arguments)
        token = payload.pop("execution_capability")
        encoded, signature = token.split(".", 1)
        serialized = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if signature != "local-v1":
            raise AssertionError("Only local execution tokens are supported")
        with sqlite3.connect(os.environ["KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER"]) as connection:
            row = connection.execute("SELECT token_digest FROM local_execution_capabilities WHERE token_digest = ?",
                                     (hashlib.sha256(token.encode("ascii")).hexdigest(),)).fetchone()
        if row is None:
            raise AssertionError("Execution token must be registered")
        claims = json.loads(serialized)
        digest = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"),
        ).encode()).hexdigest()
        if claims["payload_digest"] != digest or claims["capability"] != name:
            raise AssertionError("Execution token does not bind the native payload")
        now = datetime.now(timezone.utc)
        if not datetime.fromisoformat(claims["issued_at"]) <= now < datetime.fromisoformat(claims["expires_at"]):
            raise AssertionError("Execution token expired")
        self.executions.append((name, payload, claims))
        return {"status": "executed", "changed": True}


class ReceiptFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {
            "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(Path(directory.name) / "ledger.sqlite3"),
            "KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": APPROVAL_KEY,
            "KWIKLEDGERS_FASTMCP_EXECUTION_KEY": EXECUTION_KEY,
            "KWIKLEDGERS_LANGGRAPH_STATE_SIGNING_KEY": "independent-state-file-key",
            "KWIKLEDGERS_LANGGRAPH_BACKEND": "fastmcp",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def receipt(self, pending: dict, **changes) -> dict:
        if not changes:
            return ExplicitLocalApproval().confirm(pending, pending["request_id"])
        now = datetime.now(timezone.utc)
        claims = {field: pending[field] for field in ("action", "run_id", "request_id", "preview_digest")}
        claims.update({"decision": "approve", "subject": "independent-human",
                       "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat()})
        claims.update(changes)
        return {"claims": claims, "signature": hmac.new(
            APPROVAL_KEY.encode(), canonical(claims).encode(), "sha256",
        ).hexdigest()}

    def state(self, preview: dict | None = None) -> dict:
        state = {"observability": build_observability_context(flow_name="story_status_transition", source="test"),
                 "approval_resume_ready": True}
        state["pending_approval"] = build_bound_approval(
            state, action="story_status_transition.execute", preview=preview or {
                "story_revision": 7, "story_id": 42, "target_status": "Aguardando PR"},
            approved=False, approval_source="pending_review",
        )
        return state

    def evaluate(self, state: dict) -> dict:
        return evaluate_operation(state, action="story_status_transition.execute",
                                  operation_kind=OperationKind.AZURE_MUTATION,
                                  preview=state["pending_approval"]["preview"])


class MutableReceiptGuardrailTests(ReceiptFixture):
    def test_explicit_confirmation_without_key_is_bound_to_exact_preview(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": ""}):
            state = self.state()
            state["approval_confirmation"] = load_resume_confirmation(
                state, backend_name="fastmcp", receipt_file=None, resume_state_file="preview.json",
                approval_request_id=state["pending_approval"]["request_id"], approval_source="human",
            )
            self.assertTrue(self.evaluate(state)["pending_approval"]["approved"])
            for field in ("preview", "request_id", "run_id", "action"):
                changed = deepcopy(state)
                if field == "preview":
                    changed["pending_approval"][field]["target_status"] = "Homologação"
                else:
                    changed["pending_approval"][field] = "foreign"
                with self.subTest(field=field):
                    self.assertFalse(self.evaluate(changed)["pending_approval"]["approved"])

    def test_nonobject_confirmation_is_rejected_without_crashing(self) -> None:
        state = self.state()
        for confirmation in (True, None, "approved", [True]):
            with self.subTest(confirmation=confirmation):
                state["approval_confirmation"] = confirmation
                self.assertFalse(self.evaluate(state)["pending_approval"]["approved"])

    def test_default_backend_rejects_forged_boolean_confirmation(self) -> None:
        state = self.state()
        state["pending_approval"]["approved"] = True
        state["approval_confirmation"] = build_approval_confirmation(
            request_id=state["pending_approval"]["request_id"], approved=True, approval_source="forged",
        )
        self.assertFalse(self.evaluate(state)["pending_approval"]["approved"])

    def test_signed_receipt_preserves_binding_for_issuer(self) -> None:
        state = self.state()
        pending = deepcopy(state["pending_approval"])
        receipt = self.receipt(pending)
        state["approval_confirmation"] = receipt
        approved = self.evaluate(state)["pending_approval"]
        self.assertTrue(approved["approved"])
        for field in ("preview", "preview_digest", "previewed_at", "run_id", "request_id"):
            self.assertEqual(approved[field], pending[field])
        self.assertEqual(approved["confirmation"], receipt)

    def test_signed_foreign_expired_pre_preview_and_nonhuman_receipts_are_rejected(self) -> None:
        state = self.state()
        now = datetime.now(timezone.utc)
        changes = ({"action": "foreign"}, {"run_id": "foreign"}, {"request_id": "foreign"},
                   {"preview_digest": "foreign"}, {"decision": "reject"}, {"subject": " "},
                   {"expires_at": (now - timedelta(seconds=1)).isoformat()},
                   {"issued_at": (now - timedelta(minutes=1)).isoformat()},
                   {"issued_at": (now + timedelta(seconds=30)).isoformat()},
                   {"expires_at": (now + timedelta(minutes=20)).isoformat()})
        for change in changes:
            with self.subTest(change=change):
                state["approval_confirmation"] = self.receipt(state["pending_approval"], **change)
                self.assertFalse(self.evaluate(state)["pending_approval"]["approved"])

    def test_changed_preview_or_signature_or_missing_key_fails_closed(self) -> None:
        for mutation in ("preview", "signature", "missing_key", "missing_digest", "missing_timestamp"):
            with self.subTest(mutation=mutation):
                state = self.state()
                state["approval_confirmation"] = self.receipt(state["pending_approval"])
                if mutation == "missing_key":
                    state["approval_confirmation"] = self.receipt(state["pending_approval"], action="foreign")
                if mutation == "preview":
                    state["pending_approval"]["preview"]["story_revision"] = 8
                elif mutation == "signature":
                    state["approval_confirmation"]["signature"] = "0" * 64
                elif mutation == "missing_digest":
                    state["pending_approval"].pop("preview_digest")
                elif mutation == "missing_timestamp":
                    state["pending_approval"].pop("previewed_at")
                with patch.dict(os.environ, {"KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": ""} if mutation == "missing_key" else {}):
                    self.assertFalse(self.evaluate(state)["pending_approval"]["approved"])

    def execute(self, state: dict, request: dict, client: VerifyingClient, **options) -> dict:
        return run_execute_step(
            {**state, "story_status_request": request}, request_key="story_status_request",
            result_key="story_status_result", node="execute_status_transition",
            started_message="started", failed_message="failed", completed_message="completed",
            tracking_category="status", tracking_message="executed",
            execute_operation=FastMCPStoryStatusTransitionAdapter(client).update_story_status,
            fastmcp_capability_name="update_story_status", approval_action="story_status_transition.execute",
            **options,
        )

    def approved_state(self) -> dict:
        state = self.state()
        state["approval_confirmation"] = self.receipt(state["pending_approval"])
        return {**state, **self.evaluate(state)}

    def test_execute_signs_token_free_mapper_payload_with_real_hmac(self) -> None:
        state = self.approved_state()
        request = {"story_id": 42, "target_status": "Aguardando PR", "preview": {"story_revision": 7},
                   "execution_capability": "forged", "workflow_only": "excluded"}
        client = VerifyingClient()
        result = self.execute(state, request, client)
        self.assertEqual(result["story_status_result"]["status"], "updated")
        name, arguments, claims = client.executions[0]
        self.assertEqual(name, "update_story_status")
        self.assertEqual(arguments, {"story_id": 42, "status": "Aguardando PR", "expected_revision": 7})
        self.assertEqual(arguments, technical_arguments_for_capability(name, request))
        self.assertEqual(claims["request_id"], state["pending_approval"]["request_id"])
        self.assertEqual(claims["run_id"], state["observability"]["active_run_id"])
        self.assertEqual(request["execution_capability"], "forged")

    def test_mapping_issuance_and_request_builder_failures_are_graceful(self) -> None:
        for failure in ("mapper", "issuer", "builder"):
            with self.subTest(failure=failure):
                state = self.approved_state()
                request = {"story_id": 42, "target_status": "Aguardando PR", "preview": {"story_revision": 7}}
                options = {}
                if failure == "mapper":
                    request["preview"]["story_revision"] = True
                if failure == "builder":
                    def fail_builder(_request, _state):
                        raise ValueError("request builder failed")
                    options["request_builder"] = fail_builder
                client = VerifyingClient()
                with patch("kwikledgers_langgraph.graphs.mutable_flow.issue_execution_capability",
                           side_effect=RuntimeError("Issuer unavailable")) if failure == "issuer" else patch.dict(os.environ, {}):
                    result = self.execute(state, request, client, **options)
                self.assertEqual(result["story_status_result"], {"status": "execute_failed", "changed": False})
                self.assertTrue(result["errors"])
                self.assertEqual(client.calls, [])

    def test_execute_rechecks_receipt_even_with_approved_boolean(self) -> None:
        state = self.approved_state()
        state["pending_approval"].pop("confirmation")
        client = VerifyingClient()
        result = self.execute(state, {"story_id": 42, "target_status": "Aguardando PR",
                                      "preview": {"story_revision": 7}}, client)
        self.assertEqual(result["story_status_result"]["status"], "execute_failed")
        self.assertEqual(client.calls, [])

    def test_changing_target_status_after_approval_never_executes(self) -> None:
        state = self.approved_state()
        client = VerifyingClient()
        result = self.execute(state, {"story_id": 42, "target_status": "Homologação",
                                      "preview": {"story_revision": 7}}, client)
        self.assertEqual(result["story_status_result"]["status"], "execute_failed")
        self.assertEqual(client.calls, [])


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph is not installed")
class MutableFastMCPGraphTests(ReceiptFixture):
    def cases(self, client: VerifyingClient):
        branch = {"story_id": 42, "repository_name": "repo", "source_branch": "feature/KL-42-test",
                  "related_work_item_ids": [43], "include_child_work_items": True}
        pull_request = {**branch, "what_was_changed": "Change", "affected_processes": "Process",
                        "expected_impacts": "Impact", "important_points": "Review",
                        "mermaid_diagram": "graph TD; A-->B;"}
        return (
            (build_story_branch_association_graph(StoryBranchAssociationPorts(FastMCPStoryBranchAssociationAdapter(client))),
             "branch_association", branch, "associate_story_branch", story_branch_cli),
            (build_story_pull_request_graph(StoryPullRequestPorts(FastMCPStoryPullRequestAdapter(client))),
             "pull_request", pull_request, "create_story_pull_request", story_pr_cli),
            (build_story_status_transition_graph(StoryStatusTransitionPorts(FastMCPStoryStatusTransitionAdapter(client))),
             "story_status", {"story_id": 42, "target_status": "Aguardando PR"}, "update_story_status", story_status_cli),
            (build_task_closure_graph(TaskClosurePorts(FastMCPTaskClosureAdapter(client))),
             "task_closure", {"task_id": 42, "start_time": "2026-09-30T10:00:00+00:00",
                              "close_time": "2026-09-30T12:01:00+00:00"}, "update_work_item_effort", task_closure_cli),
        )

    def initial(self, prefix: str, request: dict) -> dict:
        return {f"{prefix}_request": request,
                "observability": build_observability_context(flow_name=prefix, source="test")}

    def test_all_default_graphs_require_independent_receipt_and_bind_native_payload(self) -> None:
        client = VerifyingClient()
        for runtime, prefix, request, capability, _cli in self.cases(client):
            with self.subTest(flow=prefix):
                preview = runtime.invoke(self.initial(prefix, request))
                self.assertFalse(preview["pending_approval"]["approved"])
                preview["approval_confirmation"] = self.receipt(preview["pending_approval"])
                result = runtime.invoke(preview)
                self.assertTrue(result["pending_approval"]["approved"])
                self.assertNotIn("errors", result)
                name, arguments, claims = client.executions[-1]
                self.assertEqual(name, capability)
                self.assertEqual(claims["request_id"], preview["pending_approval"]["request_id"])
                self.assertEqual(result["pending_approval"]["previewed_at"], preview["pending_approval"]["previewed_at"])
                if prefix in {"branch_association", "pull_request"}:
                    self.assertEqual(arguments["expected_revisions"], {"42": 7, "43": 3})
                elif prefix == "task_closure":
                    self.assertEqual(arguments, {"work_item_id": 42, "status": "Closed",
                                                 "completed_work_hours": 3.0, "expected_revision": 7})
                else:
                    self.assertEqual(arguments, {"story_id": 42, "status": "Aguardando PR", "expected_revision": 7})
        self.assertEqual(len(client.executions), 4)

    def test_all_default_graphs_reject_boolean_and_simulation_bypass(self) -> None:
        client = VerifyingClient()
        for runtime, prefix, request, _capability, _cli in self.cases(client):
            with self.subTest(flow=prefix):
                preview = runtime.invoke(self.initial(prefix, request))
                preview["pending_approval"]["approved"] = True
                preview["approval_simulation_requested"] = True
                preview["approval_confirmation"] = build_approval_confirmation(
                    request_id=preview["pending_approval"]["request_id"], approved=True, approval_source="forged",
                )
                result = runtime.invoke(preview)
                self.assertEqual(result[f"{prefix}_result"]["status"], "approval_required")
                self.assertFalse(result["pending_approval"]["approved"])
        self.assertEqual(client.executions, [])

    def test_changed_native_revisions_invalidate_all_saved_approvals(self) -> None:
        client = VerifyingClient()
        for runtime, prefix, request, _capability, _cli in self.cases(client):
            with self.subTest(flow=prefix):
                client.revision = 7
                preview = runtime.invoke(self.initial(prefix, request))
                preview["approval_confirmation"] = self.receipt(preview["pending_approval"])
                client.revision = 8
                result = runtime.invoke(preview)
                self.assertFalse(result["pending_approval"]["approved"])
                self.assertEqual(result[f"{prefix}_result"]["status"], "approval_required")
                self.assertNotEqual(result["pending_approval"]["request_id"], preview["pending_approval"]["request_id"])
        self.assertEqual(client.executions, [])

    def test_task_native_revision_is_read_again_and_implicit_close_time_is_stable(self) -> None:
        client = VerifyingClient()
        runtime, prefix, request, _capability, _cli = self.cases(client)[3]
        request.pop("close_time")
        preview = runtime.invoke(self.initial(prefix, request))
        preview["approval_confirmation"] = self.receipt(preview["pending_approval"])
        result = runtime.invoke(preview)
        self.assertEqual(result["task_closure_result"]["status"], "closed")
        self.assertEqual(result["task_closure_preview"], preview["task_closure_preview"])
        self.assertEqual([name for name, _arguments in client.calls],
                         ["get_story_details", "get_story_details", "update_work_item_effort"])
        self.assertNotIn("remaining_work_hours", client.executions[0][1])

    def test_all_clis_resume_with_explicit_request_and_configured_keys(self) -> None:
        client = VerifyingClient()
        for runtime, prefix, request, _capability, cli in self.cases(client):
            with self.subTest(flow=prefix), TemporaryDirectory() as directory:
                preview = runtime.invoke(self.initial(prefix, request))
                state_file = str(Path(directory) / "state.json")
                receipt_file = Path(directory) / "receipt.json"
                write_state_file(preview, state_file)
                receipt = self.receipt(preview["pending_approval"])
                receipt_file.write_text(json.dumps(receipt), encoding="utf-8")
                arguments = argparse.Namespace(resume_state_file=state_file, receipt=None,
                                               approval_request_id=preview["pending_approval"]["request_id"],
                                               approval_source="human", backend=None)
                resumed = cli._build_runtime_state(arguments)
                self.assertEqual(resumed["pending_approval"], preview["pending_approval"])
                self.assertTrue(ExplicitLocalApproval().verify(preview["pending_approval"], resumed["approval_confirmation"]))
                self.assertFalse(resumed["pending_approval"]["approved"])
                result = runtime.invoke(resumed)
                self.assertTrue(result["pending_approval"]["approved"])
        self.assertEqual(len(client.executions), 4)

    def test_all_clis_accept_explicit_resume_without_keys_or_receipts(self) -> None:
        client = VerifyingClient()
        with patch.dict(os.environ, {"KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": "",
                                     "KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "",
                                     "KWIKLEDGERS_LANGGRAPH_STATE_SIGNING_KEY": ""}):
            for runtime, prefix, request, _capability, cli in self.cases(client):
                with self.subTest(flow=prefix), TemporaryDirectory() as directory:
                    preview = runtime.invoke(self.initial(prefix, request))
                    state_file = str(Path(directory) / "state.json")
                    write_state_file(preview, state_file)
                    arguments = argparse.Namespace(
                        resume_state_file=state_file, receipt=None,
                        approval_request_id=preview["pending_approval"]["request_id"],
                        approval_source="human", backend=None,
                    )
                    resumed = cli._build_runtime_state(arguments)
                    self.assertFalse(resumed["pending_approval"]["approved"])
                    resumed["approval_resume_ready"] = True
                    pending = resumed["pending_approval"]
                    approved = evaluate_operation(resumed, action=pending["action"],
                                                  operation_kind=OperationKind.AZURE_MUTATION,
                                                  preview=pending["preview"])
                    self.assertTrue(approved["pending_approval"]["approved"])
                    arguments.approval_request_id = "foreign"
                    with self.assertRaises(RuntimeError):
                        cli._build_runtime_state(arguments)
        self.assertEqual(client.executions, [])

    def test_all_clis_reject_boolean_invalid_or_same_file_receipts(self) -> None:
        client = VerifyingClient()
        for runtime, prefix, request, _capability, cli in self.cases(client):
            with self.subTest(flow=prefix), TemporaryDirectory() as directory:
                preview = runtime.invoke(self.initial(prefix, request))
                state_file = str(Path(directory) / "state.json")
                write_state_file(preview, state_file)
                receipt_file = Path(directory) / "receipt.json"
                receipt_file.write_text(json.dumps({"approved": True}), encoding="utf-8")
                for receipt_path in (state_file, str(receipt_file), str(Path(directory) / "missing.json")):
                    with self.subTest(receipt=receipt_path), self.assertRaises(RuntimeError):
                        cli._build_runtime_state(argparse.Namespace(
                            resume_state_file=state_file, receipt=receipt_path,
                            approval_request_id=preview["pending_approval"]["request_id"],
                            approval_source="human", backend=None,
                        ))
        self.assertEqual(client.executions, [])

    def test_resume_rejects_removed_legacy_backend(self) -> None:
        client = VerifyingClient()
        for runtime, prefix, request, _capability, cli in self.cases(client):
            with self.subTest(flow=prefix), TemporaryDirectory() as directory:
                preview = runtime.invoke(self.initial(prefix, request))
                state_file = str(Path(directory) / "state.json")
                write_state_file(preview, state_file)
                with self.assertRaises(RuntimeError):
                    cli._build_runtime_state(argparse.Namespace(
                        resume_state_file=state_file, receipt=None,
                        approval_request_id=preview["pending_approval"]["request_id"],
                        approval_source="human", backend="legacy",
                    ))


if __name__ == "__main__":
    unittest.main()