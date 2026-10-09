from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.fastmcp_execution_capability import (
    ExecutionCapabilityError,
    issue_execution_capability,
    issue_runtime_execution_capability,
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