from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.fastmcp_execution_capability import (
    ExecutionCapabilityError,
    issue_execution_capability,
    issue_lifecycle_execution_capability,
    issue_runtime_execution_capability,
)
from kwikledgers_langgraph.lifecycle_adapters import (
    SignedReceiptApproval, active_state, approval_binding, lifecycle_scope,
)
from kwikledgers_langgraph.guardrails import build_bound_approval
from test_lifecycle_adapters import APPROVAL_KEY, receipt_for


class FastMcpExecutionCapabilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger = Path(temporary.name) / "capabilities.sqlite3"
        environment = patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(self.ledger),
                             "KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": APPROVAL_KEY.decode()})
        environment.start()
        self.addCleanup(environment.stop)

    def _state(self, *, approved: bool = True, issued_at: datetime | None = None) -> dict[str, object]:
        state = {"observability": {"active_run_id": "run-123"}}
        pending = build_bound_approval(state, action="story_status_transition.execute", preview={},
                                       approved=False, request_id="request-123", approval_source="test")
        issued_at = issued_at or datetime.now(timezone.utc)
        pending["previewed_at"] = issued_at.isoformat()
        pending["receipt"] = receipt_for(pending, issued=issued_at)
        pending["approved"] = approved
        return {**state, "pending_approval": pending}

    def test_issues_signed_capability_after_approved_gate(self) -> None:
        issued_at = datetime.now(timezone.utc)
        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "test-key"}, clear=False):
            token = issue_execution_capability(
                self._state(issued_at=issued_at),
                action="story_status_transition.execute",
                capability_name="update_story_status",
                arguments={"story_id": 120204, "status": "Aguardando PR"},
                now=issued_at,
            )

        self.assertIn(".", token)
        encoded_payload, _signature = token.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + "=="))
        self.assertEqual(payload["issued_at"], issued_at.isoformat())
        self.assertRegex(payload["nonce"], r"^[0-9a-f]{32}$")

    def test_missing_or_blank_key_issues_registered_local_capability(self) -> None:
        for key in (None, "", "   ", "configured-signing-key"):
            with self.subTest(key=key), patch.dict(os.environ):
                os.environ.pop("KWIKLEDGERS_FASTMCP_EXECUTION_KEY", None)
                if key is not None:
                    os.environ["KWIKLEDGERS_FASTMCP_EXECUTION_KEY"] = key
                token = issue_execution_capability(
                    self._state(), action="story_status_transition.execute", capability_name="update_story_status",
                    arguments={"story_id": 1, "status": "Aguardando PR", "expected_revision": 2},
                )
            encoded, suffix = token.split(".")
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            self.assertEqual(suffix, "local-v1")
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["action"], "story_status_transition.execute")
            self.assertEqual(payload["run_id"], "run-123")
            self.assertEqual(payload["request_id"], "request-123")
            validity = datetime.fromisoformat(payload["expires_at"]) - datetime.fromisoformat(payload["issued_at"])
            self.assertGreater(validity, timedelta(0))
            self.assertLessEqual(validity, timedelta(minutes=5))
            with sqlite3.connect(self.ledger) as connection:
                row = connection.execute("SELECT nonce_digest FROM local_execution_capabilities WHERE token_digest = ?",
                                         (hashlib.sha256(token.encode("ascii")).hexdigest(),)).fetchone()
            self.assertEqual(row, (hashlib.sha256(payload["nonce"].encode("ascii")).hexdigest(),))
            self.assertNotIn(token.encode("ascii"), self.ledger.read_bytes())

    def test_local_capability_rejects_truthy_approval_and_missing_identifiers(self) -> None:
        states = []
        for approved in (False, 1, "true"):
            states.append(self._state(approved=approved))
        for field in ("run_id", "request_id"):
            for value in (None, "", " ", 1):
                state = self._state()
                state["pending_approval"][field] = value
                states.append(state)
        state = self._state()
        state["pending_approval"]["action"] = "another-action"
        states.append(state)
        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": ""}):
            for state in states:
                with self.subTest(state=state), self.assertRaises(ExecutionCapabilityError):
                    issue_execution_capability(state, action="story_status_transition.execute",
                                               capability_name="update_story_status", arguments={"story_id": 1})
        self.assertFalse(self.ledger.exists())

    def test_local_capability_fails_closed_when_registration_is_unavailable(self) -> None:
        for ledger in ("", ":memory:", str(self.ledger.parent)):
            with self.subTest(ledger=ledger), patch.dict(os.environ, {
                "KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "", "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": ledger,
            }), self.assertRaises(ExecutionCapabilityError):
                issue_execution_capability(self._state(), action="story_status_transition.execute",
                                           capability_name="update_story_status", arguments={"story_id": 1})

    def test_runtime_capability_without_key_keeps_internal_allowlist(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": ""}):
            token = issue_runtime_execution_capability(self._state(approved=False), action="daily_summary.sync_tracking",
                                                      capability_name="sync_daily_tracking", arguments={"source": "test"})
            self.assertTrue(token.endswith(".local-v1"))
            with self.assertRaises(ExecutionCapabilityError):
                issue_runtime_execution_capability(self._state(), action="daily_summary.sync_tracking",
                                                   capability_name="update_story_status", arguments={"story_id": 1})

    def test_naive_issuance_time_is_rejected_before_registration(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": ""}), self.assertRaises(ExecutionCapabilityError):
            issue_execution_capability(self._state(), action="story_status_transition.execute",
                                       capability_name="update_story_status", arguments={}, now=datetime(2026, 10, 13))
        self.assertFalse(self.ledger.exists())

    def test_non_object_and_non_finite_arguments_are_rejected_before_registration(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": ""}):
            for arguments in (None, [], {"value": float("nan")}, {"value": float("inf")}, {"value": object()}):
                with self.subTest(arguments=arguments), self.assertRaises(ExecutionCapabilityError):
                    issue_execution_capability(self._state(), action="story_status_transition.execute",
                                               capability_name="update_story_status", arguments=arguments)
        self.assertFalse(self.ledger.exists())

    def test_rejects_capability_without_approved_gate(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "test-key"}, clear=False):
            with self.assertRaises(ExecutionCapabilityError):
                issue_execution_capability(
                    self._state(approved=False),
                    action="story_status_transition.execute",
                    capability_name="update_story_status",
                    arguments={"story_id": 120204, "status": "Aguardando PR"},
                )

    def test_issues_runtime_capability_only_for_internal_effects(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "test-key"}, clear=False):
            token = issue_runtime_execution_capability(
                self._state(approved=False),
                action="daily_summary.sync_tracking",
                capability_name="sync_daily_tracking",
                arguments={"source": "test"},
                now=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
            )
            with self.assertRaises(ExecutionCapabilityError):
                issue_runtime_execution_capability(
                    self._state(),
                    action="daily_summary.sync_tracking",
                    capability_name="update_story_status",
                    arguments={"story_id": 1},
                )

        encoded_payload, _signature = token.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + "=="))
        self.assertEqual(payload["request_id"], "runtime:daily_summary.sync_tracking")


class LifecycleExecutionCapabilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger = Path(temporary.name) / "capabilities.sqlite3"
        self.approval_key = b"a" * 32
        self.approvals = SignedReceiptApproval(self.approval_key, clock=lambda: self.now)
        self.preview = {"task_id": 11, "assigned_to": "native@example.com", "area_path": "KL\\Squad",
                        "status": "Active", "expected_revision": 2}
        self.arguments = {"work_item_id": 11, "assigned_to": "native@example.com", "area_path": "KL\\Squad",
                          "status": "Active", "expected_revision": 2}
        self.state = self.approved_state("activate", self.preview)
        environment = patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "test-key",
                              "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(self.ledger)}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)

    def approved_state(self, action, preview, *, issued_at=None, expires_at=None):
        issued_at = issued_at or self.now
        expires_at = expires_at or issued_at + timedelta(minutes=5)
        pending = {"action": action, "run_id": "native-run", "request_id": "native-request",
                   "preview": deepcopy(preview), "approved": True, "previewed_at": self.now.isoformat(),
                   "preview_digest": hashlib.sha256(json.dumps(preview, sort_keys=True, allow_nan=False).encode()).hexdigest()}
        pending["receipt"] = receipt_for(pending, issued=issued_at, expires=expires_at, key=self.approval_key)
        return {"run_id": "native-run", "observability": {"active_run_id": "native-run"}, "pending_approval": pending}

    def issue(self, *, state=None, action="activate", capability_name="update_work_item_content", arguments=None):
        return issue_lifecycle_execution_capability(
            active_state(action) if state is None else state, action=action, capability_name=capability_name,
            arguments=self.arguments if arguments is None else arguments, now=self.now,
        )

    def test_authenticated_scope_issues_payload_bound_signed_capability(self):
        with lifecycle_scope(self.state, self.approvals):
            token = self.issue()
        encoded, signature = token.split(".", 1)
        payload_bytes = base64.urlsafe_b64decode(encoded + "==")
        self.assertEqual(signature, "local-v1")
        payload = json.loads(payload_bytes)
        self.assertEqual(payload["capability"], "update_work_item_content")
        serialized = json.dumps(self.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(payload["payload_digest"], hashlib.sha256(serialized.encode()).hexdigest())
        self.assertEqual(payload["run_id"], "native-run")
        self.assertEqual(payload["request_id"], "native-request")

    def test_forged_verified_state_without_active_scope_is_denied(self):
        forged = {**self.state, "verified_lifecycle_binding": approval_binding(self.state["pending_approval"])}
        with self.assertRaises(ExecutionCapabilityError):
            self.issue(state=forged)

    def test_tracking_only_scope_cannot_issue_mutation_capability(self):
        with lifecycle_scope(self.state):
            with self.assertRaises(ExecutionCapabilityError):
                self.issue(state=self.state)

    def test_caller_cannot_replace_authenticated_state(self):
        with lifecycle_scope(self.state, self.approvals):
            trusted = active_state("activate")
            for field, value in (("run_id", "foreign"), ("observability", {"active_run_id": "foreign"}),
                                 ("verified_lifecycle_binding", {}), ("pending_approval", {})):
                with self.subTest(field=field), self.assertRaises(ExecutionCapabilityError):
                    self.issue(state={**trusted, field: value})

    def test_wrong_action_capability_and_technical_arguments_are_denied(self):
        with lifecycle_scope(self.state, self.approvals):
            trusted = active_state("activate")
            for changed in ({"action": "hours"}, {"capability_name": "create_work_item"},
                            {"arguments": {**self.arguments, "work_item_id": 12}},
                            {"arguments": {**self.arguments, "expected_revision": 3}},
                            {"arguments": {**self.arguments, "remaining_work_hours": 0}},
                            {"arguments": {**self.arguments, "execution_capability": "forged"}},
                            {"arguments": {field: value for field, value in self.arguments.items() if field != "area_path"}}):
                with self.subTest(changed=changed), self.assertRaises(ExecutionCapabilityError):
                    self.issue(state=trusted, **changed)

    def test_receipt_is_reverified_before_issuance(self):
        with lifecycle_scope(self.state, self.approvals):
            trusted = active_state("activate")
            self.now -= timedelta(seconds=1)
            with self.assertRaises(ExecutionCapabilityError):
                self.issue(state=trusted)

    def test_invalid_keys_and_tampered_receipts_cannot_authorize_scope(self):
        for key in (None, b"", b"a" * 31, "a" * 32):
            with self.subTest(key=key), self.assertRaises(ValueError):
                SignedReceiptApproval(key, clock=lambda: self.now)
        for mutation in ("signature", "key", "request_id", "expiry"):
            state = deepcopy(self.state)
            pending = state["pending_approval"]
            if mutation == "signature":
                pending["receipt"]["signature"] = "0" * 64
            elif mutation == "key":
                pending["receipt"] = receipt_for(pending, issued=self.now, key=b"b" * 32)
            elif mutation == "request_id":
                pending["receipt"]["claims"]["request_id"] = "foreign-request"
            else:
                pending["receipt"] = receipt_for(pending, issued=self.now, expires=self.now)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                with lifecycle_scope(state, self.approvals):
                    self.fail("Invalid receipt entered authenticated scope")
        self.assertFalse(self.ledger.exists())

    def test_local_capability_expiration_is_capped_by_signed_receipt(self):
        with lifecycle_scope(self.state, self.approvals):
            self.now += timedelta(minutes=4)
            token = self.issue()
        encoded, _signature = token.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=="))
        self.assertEqual(datetime.fromisoformat(payload["expires_at"]) -
                 datetime.fromisoformat(payload["issued_at"]), timedelta(minutes=1))
        self.assertEqual(payload["expires_at"], self.state["pending_approval"]["receipt"]["claims"]["expires_at"])

    def test_naive_or_expired_issuance_time_is_denied(self):
        with lifecycle_scope(self.state, self.approvals):
            for now in (self.now.replace(tzinfo=None), datetime.now(timezone.utc) + timedelta(days=1)):
                with self.subTest(now=now), self.assertRaises(ExecutionCapabilityError):
                    issue_lifecycle_execution_capability(
                        active_state("activate"), action="activate", capability_name="update_work_item_content",
                        arguments=self.arguments, now=now,
                    )

    def test_receipt_predating_preview_is_denied(self):
        state = self.approved_state("activate", self.preview, issued_at=self.now - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            with lifecycle_scope(state, self.approvals):
                self.issue()

    def test_missing_signing_key_issues_local_capability(self):
        with lifecycle_scope(self.state, self.approvals), patch.dict(
                os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": ""}, clear=False):
            self.assertTrue(self.issue().endswith(".local-v1"))

    def test_verified_lifecycle_approval_without_receipt_cannot_issue(self):
        trusted = deepcopy(self.state)
        trusted["pending_approval"].pop("receipt")
        trusted["verified_lifecycle_binding"] = approval_binding(trusted["pending_approval"])
        for key in ("", "test-key"):
            with self.subTest(key=key), patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": key}), patch(
                "kwikledgers_langgraph.lifecycle_adapters.active_state", return_value=deepcopy(trusted),
            ) as active:
                with self.assertRaises(ExecutionCapabilityError):
                    self.issue(state=trusted)
                active.assert_called_once_with("activate")
                for arguments in ({**self.arguments, "expected_revision": 3},
                                  {**self.arguments, "remaining_work_hours": 0}):
                    with self.subTest(arguments=arguments), self.assertRaises(ExecutionCapabilityError):
                        self.issue(state=trusted, arguments=arguments)
        self.assertFalse(self.ledger.exists())

    def test_receiptless_issuer_requires_consistent_approved_binding(self):
        trusted = deepcopy(self.state)
        trusted["pending_approval"].pop("receipt")
        trusted["verified_lifecycle_binding"] = approval_binding(trusted["pending_approval"])
        for field, value in (("approved", False), ("run_id", "foreign-run"), ("request_id", "foreign-request"),
                             ("action", "close")):
            changed = deepcopy(trusted)
            changed["pending_approval"][field] = value
            with self.subTest(field=field), patch(
                "kwikledgers_langgraph.lifecycle_adapters.active_state", return_value=changed,
            ), self.assertRaises(ExecutionCapabilityError):
                self.issue(state=changed)

    def test_present_but_malformed_receipt_is_not_ignored(self):
        for receipt in ({}, {"claims": {}}, {"claims": {"issued_at": "invalid", "expires_at": "invalid"}}):
            trusted = deepcopy(self.state)
            trusted["pending_approval"]["receipt"] = receipt
            trusted["verified_lifecycle_binding"] = approval_binding(trusted["pending_approval"])
            with self.subTest(receipt=receipt), patch(
                "kwikledgers_langgraph.lifecycle_adapters.active_state", return_value=trusted,
            ), self.assertRaises(ExecutionCapabilityError):
                self.issue(state=trusted)

    def test_supported_stages_issue_only_their_exact_technical_arguments(self):
        native_arguments = {"story_id": 10, "repository_name": "payment", "source_branch": "feature/KL-10-story"}
        stages = (
            ("work_item_creation", "create_work_item",
             {"work_item_type": "Task", "title": "Approved task", "description": "Approved scope"},
             {"work_item_type": "Task", "title": "Approved task", "description": "Approved scope"}),
            ("atomic_add_story_comment", "add_story_comment",
             {"story_id": 10, "comment": "Approved comment", "expected_revision": 1},
             {"story_id": 10, "comment": "Approved comment", "expected_revision": 1}),
            ("awaiting_pr", "update_story_status",
             {"story_id": 10, "target_status": "Aguardando PR", "expected_revision": 1},
             {"story_id": 10, "status": "Aguardando PR", "expected_revision": 1}),
            ("hours", "update_work_item_effort",
             {"task_id": 11, "completed_work_hours": 2, "expected_revision": 2},
             {"work_item_id": 11, "completed_work_hours": 2, "expected_revision": 2}),
            ("close", "update_work_item_effort",
             {"task_id": 11, "completed_work_hours": 2, "expected_revision": 2, "status": "Closed"},
             {"work_item_id": 11, "completed_work_hours": 2, "expected_revision": 2, "status": "Closed"}),
            ("branch_links", "associate_story_branch",
             {"story_id": 10, "repository": "payment", "name": "feature/KL-10-story",
              "associated_ids": [10, 11], "expected_revisions": {"10": 1, "11": 2}},
             {**native_arguments, "related_work_item_ids": [10, 11], "include_child_work_items": True,
              "expected_revisions": {"10": 1, "11": 2}}),
            ("draft_pr", "create_story_pull_request",
             {"story_id": 10, "native_arguments": native_arguments, "expected_revision": 1,
              "expected_revisions": {"10": 1, "11": 2}},
             {**native_arguments, "expected_revision": 1, "expected_revisions": {"10": 1, "11": 2}}),
        )
        for action, capability, preview, arguments in stages:
            with self.subTest(action=action), lifecycle_scope(self.approved_state(action, preview), self.approvals):
                token = self.issue(action=action, capability_name=capability, arguments=arguments)
                self.assertIn(".", token)
                with self.assertRaises(ExecutionCapabilityError):
                    self.issue(action=action, capability_name=capability, arguments={**arguments, "unexpected": True})

    def test_conflicting_parent_revision_cannot_issue_draft_pr_capability(self):
        preview = {"story_id": 10, "native_arguments": {"story_id": 10}, "expected_revision": 99,
                   "expected_revisions": {"10": 1, "11": 2}}
        arguments = {**preview["native_arguments"], "expected_revision": 99,
                     "expected_revisions": preview["expected_revisions"]}
        with lifecycle_scope(self.approved_state("draft_pr", preview), self.approvals):
            with self.assertRaises(ExecutionCapabilityError):
                self.issue(action="draft_pr", capability_name="create_story_pull_request", arguments=arguments)

    def test_debt_items_require_their_own_exact_preview_and_action(self):
        for action in ("debt_parent", "debt_child_0", "debt_child_12"):
            preview = {"repository_name": "payment", "work_item_type": "Task", "title": "Technical debt",
                       "description": "Approved work", "iteration_path": "KL\\Sprint 1"}
            if action != "debt_parent":
                preview["parent_work_item_id"] = 10
            with self.subTest(action=action), lifecycle_scope(self.approved_state(action, preview), self.approvals):
                token = self.issue(action=action, capability_name="create_work_item", arguments=preview)
                self.assertIn(".", token)
                for arguments in ({**preview, "parent_work_item_id": 99}, {**preview, "unexpected": True}):
                    with self.subTest(arguments=arguments), self.assertRaises(ExecutionCapabilityError):
                        self.issue(action=action, capability_name="create_work_item", arguments=arguments)
                with self.assertRaises(ExecutionCapabilityError):
                    self.issue(state=active_state(action), action="debt_child_1",
                               capability_name="create_work_item", arguments=preview)
                with self.assertRaises(ExecutionCapabilityError):
                    self.issue(action=action, capability_name="update_work_item_content", arguments=preview)

    def test_unknown_stages_do_not_authorize_work_item_creation(self):
        for action in ("branch", "implement", "commit", "debt_child_-1", "debt_child_01", "debt_child_any"):
            with self.subTest(action=action), lifecycle_scope(self.approved_state(action, {}), self.approvals):
                with self.assertRaises(ExecutionCapabilityError):
                    self.issue(action=action, capability_name="create_work_item", arguments={})