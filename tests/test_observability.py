from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kwikledgers_langgraph.observability import (
    append_runtime_usage_metrics,
    build_observability_context,
    build_runtime_usage_metrics_entry,
    record_runtime_event,
)


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

    def test_build_runtime_usage_metrics_entry_uses_minimal_operational_fields(self) -> None:
        state = {
            "source": "test",
            "tracking_events": [{"category": "runtime", "message": "Fluxo iniciado."}],
            "observability": build_observability_context(
                flow_name="story_pull_request",
                source="test",
            ),
        }
        state.update(
            record_runtime_event(
                state,
                node="stop_without_create",
                status="completed",
                message="Fluxo encerrado após preview.",
            )
        )

        entry = build_runtime_usage_metrics_entry(state)

        self.assertEqual(entry["title"], "runtime:story_pull_request:completed")
        self.assertEqual(entry["summary"], "Fluxo encerrado após preview.")
        self.assertTrue(any(action.startswith("run_id=") for action in entry["actions"]))

    def test_append_runtime_usage_metrics_writes_ai_tracking_entry(self) -> None:
        state = {
            "source": "test",
            "tracking_events": [],
            "observability": build_observability_context(
                flow_name="story_status_transition",
                source="test",
            ),
        }
        state.update(
            record_runtime_event(
                state,
                node="execute_status_transition",
                status="completed",
                message="Transicao concluida.",
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            tracking_root = Path(temp_dir)
            path = append_runtime_usage_metrics(state, tracking_root=tracking_root)
            content = path.read_text(encoding="utf-8")

        self.assertIn("runtime:story_status_transition:completed", content)
        self.assertIn("Resumo: Transicao concluida.", content)


if __name__ == "__main__":
    unittest.main()