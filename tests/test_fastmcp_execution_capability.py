from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.fastmcp_execution_capability import (
    ExecutionCapabilityError,
    issue_execution_capability,
    issue_lifecycle_execution_capability,
    issue_runtime_execution_capability,
)
from kwikledgers_langgraph.lifecycle_adapters import (
    SignedReceiptApproval, active_state, approval_binding, canonical, lifecycle_scope,
)


class FastMcpExecutionCapabilityTestCase(unittest.TestCase):
    def _state(self, *, approved: bool = True) -> dict[str, object]:
        return {
            "pending_approval": {
                "action": "story_status_transition.execute",
                "approved": approved,
                "run_id": "run-123",
                "request_id": "request-123",
            },
            "observability": {"active_run_id": "run-123"},
        }

    def test_issues_signed_capability_after_approved_gate(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "test-key"}, clear=False):
            token = issue_execution_capability(
                self._state(),
                action="story_status_transition.execute",
                capability_name="update_story_status",
                arguments={"story_id": 120204, "status": "Aguardando PR"},
                now=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
            )

        self.assertIn(".", token)
        encoded_payload, _signature = token.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + "=="))
        self.assertEqual(payload["issued_at"], "2026-09-17T12:00:00+00:00")
        self.assertRegex(payload["nonce"], r"^[0-9a-f]{32}$")

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
        self.approval_key = b"a" * 32
        self.approvals = SignedReceiptApproval(self.approval_key, clock=lambda: self.now)
        self.preview = {"task_id": 11, "assigned_to": "native@example.com", "area_path": "KL\\Squad",
                        "status": "Active", "expected_revision": 2}
        self.arguments = {"work_item_id": 11, "assigned_to": "native@example.com", "area_path": "KL\\Squad",
                          "status": "Active", "expected_revision": 2}
        self.state = self.approved_state("activate", self.preview)
        environment = patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "test-key"}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)

    def approved_state(self, action, preview, *, issued_at=None, expires_at=None):
        issued_at = issued_at or self.now
        expires_at = expires_at or issued_at + timedelta(minutes=5)
        pending = {"action": action, "run_id": "native-run", "request_id": "native-request",
                   "preview": deepcopy(preview), "approved": True, "previewed_at": self.now.isoformat(),
                   "preview_digest": hashlib.sha256(json.dumps(preview, sort_keys=True, allow_nan=False).encode()).hexdigest()}
        claims = {field: pending[field] for field in ("action", "run_id", "request_id", "preview_digest")}
        claims.update({"decision": "approve", "subject": "external-human", "issued_at": issued_at.isoformat(),
                       "expires_at": expires_at.isoformat()})
        pending["receipt"] = {"claims": claims,
                              "signature": hmac.new(self.approval_key, canonical(claims).encode(), "sha256").hexdigest()}
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
        self.assertEqual(signature, hmac.new(b"test-key", payload_bytes, "sha256").hexdigest())
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
            self.now += timedelta(minutes=5)
            with self.assertRaises(ExecutionCapabilityError):
                self.issue(state=trusted)

    def test_capability_cannot_outlive_receipt(self):
        with lifecycle_scope(self.state, self.approvals):
            self.now += timedelta(minutes=4)
            token = self.issue()
        encoded, _signature = token.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=="))
        self.assertEqual(payload["expires_at"], self.state["pending_approval"]["receipt"]["claims"]["expires_at"])

    def test_naive_or_expired_issuance_time_is_denied(self):
        with lifecycle_scope(self.state, self.approvals):
            for now in (self.now.replace(tzinfo=None), self.now + timedelta(minutes=5)):
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

    def test_missing_signing_key_is_denied(self):
        with lifecycle_scope(self.state, self.approvals), patch.dict(
                os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": ""}, clear=False):
            with self.assertRaises(ExecutionCapabilityError):
                self.issue()

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