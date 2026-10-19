from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from kwikledgers_langgraph.request_metrics_hook import handle_event


class RequestMetricsHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "AI_Tracking"
        self.root.mkdir()

    def event(self, name: str, **fields):
        return handle_event(name, {"session_id": "session-test", **fields}, tracking_root=self.root)

    def records(self):
        return [json.loads(line) for line in (self.root / "Metrics/request-events.jsonl").read_text().splitlines()]

    def test_request_and_tool_lifecycle_records_elapsed_time_without_payloads(self):
        response = self.event("UserPromptSubmit", prompt="private-prompt")
        self.assertIn("Operational request ID", response["systemMessage"])
        with patch("kwikledgers_langgraph.request_metrics_hook._now", return_value=datetime(2026, 10, 16, tzinfo=timezone.utc)):
            self.event("PreToolUse", tool_name="runTests", tool_use_id="tool-1", tool_input={"password": "private-input"})
        with patch("kwikledgers_langgraph.request_metrics_hook._now", return_value=datetime(2026, 10, 16, 0, 0, 2, tzinfo=timezone.utc)):
            self.event("PostToolUse", tool_name="runTests", tool_use_id="tool-1", tool_response={"text": "private-output"})
        self.event("Stop")
        records = self.records()
        self.assertEqual([row["kind"] for row in records], ["request_started", "step", "step", "request_finished"])
        self.assertEqual(records[2]["duration_ms"], 2000)
        self.assertEqual(len({row["request_id"] for row in records}), 1)
        content = json.dumps(records)
        self.assertNotIn("private-prompt", content)
        self.assertNotIn("private-input", content)
        self.assertNotIn("private-output", content)

    def test_missing_start_does_not_fabricate_tool_duration(self):
        self.event("UserPromptSubmit")
        self.event("PostToolUse", tool_name="bash", tool_use_id="missing-start", is_error=True)
        record = self.records()[-1]
        self.assertIsNone(record.get("duration_ms"))
        self.assertEqual(record["status"], "failed")

    def test_missing_session_is_explicit_error(self):
        with self.assertRaisesRegex(ValueError, "session_id"):
            handle_event("UserPromptSubmit", {}, tracking_root=self.root)

    def test_missing_request_is_explicit_error(self):
        with self.assertRaisesRegex(ValueError, "solicitação ativa"):
            self.event("PreToolUse", tool_name="bash")

    def test_stop_reentry_does_not_write(self):
        self.event("UserPromptSubmit", stop_hook_active=True)
        self.assertFalse((self.root / "Metrics/request-events.jsonl").exists())

    def test_missing_call_id_is_not_matched_by_tool_name(self):
        self.event("UserPromptSubmit")
        self.event("PreToolUse", tool_name="bash")
        self.event("PostToolUse", tool_name="bash")
        self.assertIsNone(self.records()[-1].get("duration_ms"))

    def test_parallel_tools_are_matched_by_call_id(self):
        self.event("UserPromptSubmit")
        for second, call_id in [(0, "first"), (1, "second")]:
            with patch("kwikledgers_langgraph.request_metrics_hook._now", return_value=datetime(2026, 10, 16, 0, 0, second, tzinfo=timezone.utc)):
                self.event("PreToolUse", tool_name="bash", tool_use_id=call_id)
        for second, call_id in [(2, "second"), (4, "first")]:
            with patch("kwikledgers_langgraph.request_metrics_hook._now", return_value=datetime(2026, 10, 16, 0, 0, second, tzinfo=timezone.utc)):
                self.event("PostToolUse", tool_name="bash", tool_use_id=call_id)
        completed = [row for row in self.records() if row["kind"] == "step" and row["status"] == "completed"]
        self.assertEqual([row["duration_ms"] for row in completed], [1000, 4000])

    def test_clock_regression_is_reported_not_clamped(self):
        self.event("UserPromptSubmit")
        with patch("kwikledgers_langgraph.request_metrics_hook._now", return_value=datetime(2026, 10, 16, 0, 0, 2, tzinfo=timezone.utc)):
            self.event("PreToolUse", tool_name="bash", tool_use_id="clock")
        with patch("kwikledgers_langgraph.request_metrics_hook._now", return_value=datetime(2026, 10, 16, tzinfo=timezone.utc)):
            with self.assertRaisesRegex(ValueError, "Relógio retrocedeu"):
                self.event("PostToolUse", tool_name="bash", tool_use_id="clock")


if __name__ == "__main__":
    unittest.main()
