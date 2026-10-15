from __future__ import annotations

import unittest

from kwikledgers_langgraph.guardrails import (
    ApprovalRequiredError,
    OperationKind,
    allow_without_approval,
    build_approval_confirmation,
    build_bound_approval,
    evaluate_operation,
    require_explicit_approval,
)
from kwikledgers_langgraph.observability import build_observability_context


class GuardrailsTestCase(unittest.TestCase):
    def test_read_only_operation_is_auto_approved_without_pending_approval(self) -> None:
        state = {
            "observability": build_observability_context(
                flow_name="daily_summary",
                source="test",
            )
        }

        result = allow_without_approval(
            state,
            action="daily_summary.fetch_summary",
            operation_kind=OperationKind.READ_ONLY,
        )

        self.assertNotIn("pending_approval", result)
        self.assertIn("tracking_events", result)
        self.assertEqual(
            result["tracking_events"][-1]["payload"]["approved"],
            True,
        )
        self.assertEqual(
            result["tracking_events"][-1]["payload"]["operation_kind"],
            OperationKind.READ_ONLY.value,
        )

    def test_azure_mutation_creates_pending_approval_preview(self) -> None:
        state = {
            "observability": build_observability_context(
                flow_name="story_pull_request",
                source="test",
            )
        }

        result = evaluate_operation(
            state,
            action="story_pull_request.create",
            operation_kind=OperationKind.AZURE_MUTATION,
            preview={"title": "Draft PR"},
        )

        self.assertIn("pending_approval", result)
        self.assertEqual(result["pending_approval"]["approved"], False)
        self.assertEqual(result["pending_approval"]["preview"]["title"], "Draft PR")
        self.assertTrue(result["pending_approval"]["request_id"])

    def test_require_explicit_approval_raises_without_approval(self) -> None:
        state = {
            "observability": build_observability_context(
                flow_name="story_pull_request",
                source="test",
            )
        }

        with self.assertRaises(ApprovalRequiredError):
            require_explicit_approval(
                state,
                action="story_pull_request.create",
                preview={"title": "Draft PR"},
            )

    def test_preseeded_pending_approval_without_confirmation_does_not_approve_mutation(self) -> None:
        state = {
            "observability": build_observability_context(
                flow_name="story_pull_request",
                source="test",
            ),
        }
        state["pending_approval"] = build_bound_approval(
            state,
            action="story_pull_request.create",
            preview={"title": "Draft PR"},
            approved=True,
            approval_source="test",
        )

        result = evaluate_operation(
            state,
            action="story_pull_request.create",
            operation_kind=OperationKind.AZURE_MUTATION,
            preview={"title": "Draft PR"},
        )

        self.assertFalse(result["pending_approval"]["approved"])
        self.assertEqual(
            result["tracking_events"][-1]["payload"]["reason"],
            "explicit_approval_required",
        )

if __name__ == "__main__":
    unittest.main()