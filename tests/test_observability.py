from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kwikledgers_langgraph.observability import (
    append_runtime_event_stream,
    append_runtime_observability_artifacts,
    append_runtime_usage_metrics,
    build_observability_context,
    build_runtime_event_stream_records,
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

    def test_build_runtime_event_stream_records_emits_structured_runtime_records(self) -> None:
        state = {
            "source": "test",
            "summary": {"counts": {"assigned_items": 2, "blocked_items": 1}},
            "tracking_events": [{"category": "runtime", "message": "Fluxo iniciado."}],
            "errors": ["Falha controlada."],
            "observability": build_observability_context(
                flow_name="story_pull_request",
                source="test",
                backend_name="legacy",
            ),
        }
        state.update(
            record_runtime_event(
                state,
                node="preview_pull_request",
                status="completed",
                message="Preview concluido.",
                payload={
                    "story_id": 120204,
                    "callback_url": "https://dev.azure.com/example",
                    "request_body": {"token": "secret-token"},
                },
            )
        )

        records = build_runtime_event_stream_records(state)

        self.assertEqual(records[0]["record_type"], "run_summary")
        self.assertEqual(records[0]["backend_name"], "legacy")
        self.assertTrue(
            any(
                record["record_type"] == "timeline_event"
                and record.get("node") == "preview_pull_request"
                and record.get("payload", {}).get("story_id") == 120204
                for record in records
            )
        )
        timeline_record = next(
            record for record in records if record["record_type"] == "timeline_event" and record.get("node") == "preview_pull_request"
        )
        self.assertEqual(timeline_record["payload"]["callback_url"], "[redacted]")
        self.assertEqual(timeline_record["payload"]["request_body"], "[redacted]")
        self.assertTrue(any(record["record_type"] == "decision" for record in records))
        self.assertTrue(any(record["record_type"] == "tracking_event" for record in records))
        self.assertTrue(any(record["record_type"] == "error" for record in records))

    def test_append_runtime_observability_artifacts_writes_markdown_and_jsonl(self) -> None:
        state = {
            "source": "test",
            "tracking_events": [{"category": "runtime", "message": "Fluxo iniciado."}],
            "observability": build_observability_context(
                flow_name="story_status_transition",
                source="test",
                backend_name="legacy",
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
            artifact_paths = append_runtime_observability_artifacts(state, tracking_root=tracking_root)
            usage_content = artifact_paths["usage_metrics_path"].read_text(encoding="utf-8")
            event_records = [
                json.loads(line)
                for line in artifact_paths["event_stream_path"].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertIn("runtime:story_status_transition:completed", usage_content)
        self.assertTrue(any(record["record_type"] == "run_summary" for record in event_records))
        self.assertTrue(
            any(
                record["record_type"] == "timeline_event"
                and record.get("node") == "execute_status_transition"
                and record.get("status") == "completed"
                for record in event_records
            )
        )

    def test_append_runtime_event_stream_writes_jsonl_records(self) -> None:
        state = {
            "source": "test",
            "tracking_events": [],
            "observability": build_observability_context(
                flow_name="daily_summary",
                source="test",
                backend_name="legacy",
            ),
        }
        state.update(
            record_runtime_event(
                state,
                node="sync_tracking",
                status="completed",
                message="Tracking sincronizado.",
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            tracking_root = Path(temp_dir)
            path = append_runtime_event_stream(state, tracking_root=tracking_root)
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertTrue(all(record["runtime"] == "kwikledgers_langgraph" for record in records))
        self.assertTrue(any(record["record_type"] == "run_summary" for record in records))
        self.assertTrue(any(record["record_type"] == "timeline_event" for record in records))


if __name__ == "__main__":
    unittest.main()