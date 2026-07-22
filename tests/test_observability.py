from __future__ import annotations

import unittest

from kwikledgers_langgraph.observability import build_observability_context, record_runtime_event


class ObservabilityTestCase(unittest.TestCase):
    def test_build_observability_context_initializes_timeline(self) -> None:
        context = build_observability_context(
            flow_name="daily_summary",
            source="test",
        )

        self.assertIn("correlation_id", context)
        self.assertIn("active_run_id", context)
        self.assertEqual(context["flow_name"], "daily_summary")
        self.assertEqual(context["timeline"][0]["status"], "initialized")

    def test_record_runtime_event_appends_timeline_entry(self) -> None:
        state = {
            "observability": build_observability_context(
                flow_name="daily_summary",
                source="test",
            )
        }

        result = record_runtime_event(
            state,
            node="fetch_summary",
            status="completed",
            message="Resumo diário carregado.",
            payload={"counts": {"assigned_items": 3}},
        )

        self.assertIn("observability", result)
        timeline = result["observability"]["timeline"]
        self.assertEqual(timeline[-1]["node"], "fetch_summary")
        self.assertEqual(timeline[-1]["status"], "completed")
        self.assertEqual(timeline[-1]["payload"]["counts"]["assigned_items"], 3)


if __name__ == "__main__":
    unittest.main()