from __future__ import annotations

from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.fastmcp_execution_capability import (
    ExecutionCapabilityError,
    issue_execution_capability,
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

    def test_rejects_capability_without_approved_gate(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "test-key"}, clear=False):
            with self.assertRaises(ExecutionCapabilityError):
                issue_execution_capability(
                    self._state(approved=False),
                    action="story_status_transition.execute",
                    capability_name="update_story_status",
                    arguments={"story_id": 120204, "status": "Aguardando PR"},
                )