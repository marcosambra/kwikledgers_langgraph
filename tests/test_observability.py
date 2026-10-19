from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kwikledgers_langgraph.observability import (
    append_runtime_usage_metrics,
    append_request_metrics,
    build_observability_context,
    build_runtime_usage_metrics_entry,
    record_runtime_event,
    resolve_ai_tracking_root,
    workflow_completion,
)


class ObservabilityTestCase(unittest.TestCase):
    def test_completion_contract_and_metrics_share_debt_and_preview_only_evidence(self) -> None:
        debt = {"outcome": "completed", "results": {
            "debt_parent": {"id": 10}, "debt_child_0_snapshot": {"snapshot_confirmed": True}}}
        self.assertEqual(workflow_completion(debt)["snapshot"], "confirmed")
        self.assertTrue(workflow_completion(debt)["work_item_persisted"])
        self.assertEqual(workflow_completion({**debt, "outcome": "reconciliation_required"})["snapshot"],
                         "not_checked")
        state = {"task_closure_result": {"status": "lifecycle_required"},
                 "observability": {"active_run_id": "run", "flow_name": "task_closure"}}
        with tempfile.TemporaryDirectory() as directory:
            path = append_request_metrics(state, tracking_root=directory)
            events = [json.loads(line) for line in path.read_text().splitlines()]
        outcome = next(event for event in events if event.get("details", {}).get("node") == "workflow_result")
        self.assertEqual(outcome["details"]["outcome"], "lifecycle_required")
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(events[-1]["status"], "blocked")

    def test_resolves_native_workspace_tracking_without_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tracking = Path(directory) / "AI_Tracking"
            tracking.mkdir()
            with patch.dict(os.environ, {"KWIKLEDGERS_ROOT_DIR": directory}, clear=True):
                self.assertEqual(resolve_ai_tracking_root(), tracking)

    def test_explicit_tracking_overrides_workspace_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                "KWIKLEDGERS_ROOT_DIR": "/missing/native/workspace",
                "KWIKLEDGERS_AI_TRACKING_ROOT": directory,
            }, clear=True):
                self.assertEqual(resolve_ai_tracking_root(), Path(directory))

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
        self.assertIn("at", timeline[-1])
        self.assertGreaterEqual(timeline[-1]["duration_ms"], 0)

    def test_request_metrics_preserve_graph_steps_without_payloads(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_REQUEST_ID": ""}):
            state = {"observability": build_observability_context(flow_name="daily_summary", source="test")}
        state.update(record_runtime_event(
            state, node="fetch_summary", status="completed",
            message="private-message", payload={"token": "private-token"},
        ))
        with tempfile.TemporaryDirectory() as directory:
            tracking = Path(directory) / "AI_Tracking"
            tracking.mkdir()
            path = append_request_metrics(state, tracking_root=tracking)
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records[0]["kind"], "request_started")
        self.assertEqual(records[-1]["kind"], "request_finished")
        self.assertGreaterEqual(records[-1]["duration_ms"], 0)
        self.assertEqual(records[2]["details"]["node"], "fetch_summary")
        self.assertNotIn("private-token", json.dumps(records))
        self.assertNotIn("private-message", json.dumps(records))

    def test_correlated_graph_does_not_finish_user_request(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_REQUEST_ID": "request-explicit"}):
            state = {"observability": build_observability_context(flow_name="daily_summary", source="test")}
        with tempfile.TemporaryDirectory() as directory:
            tracking = Path(directory) / "AI_Tracking"
            tracking.mkdir()
            path = append_request_metrics(state, tracking_root=tracking)
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertTrue(all(row["request_id"] == "request-explicit" for row in records))
        self.assertTrue(all(row["kind"] == "step" for row in records))

    def test_waiting_graph_is_not_reported_as_completed(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_REQUEST_ID": ""}):
            state = {"outcome": "waiting", "observability": build_observability_context(flow_name="approval", source="test")}
        with tempfile.TemporaryDirectory() as directory:
            tracking = Path(directory) / "AI_Tracking"
            tracking.mkdir()
            path = append_request_metrics(state, tracking_root=tracking)
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records[-1]["status"], "blocked")

    def test_workflow_result_preserves_snapshot_evidence_without_inventing_duration(self) -> None:
        state = {"outcome": "completed",
                 "results": {"confirm_snapshot": {"snapshot_confirmed": True}},
                 "observability": {"active_run_id": "observed-run", "flow_name": "work_item_creation"}}
        with tempfile.TemporaryDirectory() as directory:
            path = append_request_metrics(state, tracking_root=directory)
            records = [json.loads(line) for line in path.read_text().splitlines()]
        event = next(row for row in records if row.get("details", {}).get("node") == "workflow_result")
        self.assertEqual(event["details"]["outcome"], "completed")
        self.assertEqual(event["details"]["reason"], "snapshot_confirmed")
        self.assertIsNone(event.get("duration_ms"))

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