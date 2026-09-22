from __future__ import annotations

from datetime import datetime, timezone
import unittest

from kwikledgers_langgraph.task_closure_policy import build_task_closure_preview


class TaskClosurePolicyTestCase(unittest.TestCase):
    def test_calculates_hours_with_ceil(self) -> None:
        preview = build_task_closure_preview(
            {"task_id": 120310, "start_time": "2026-09-17T10:00:00+00:00"},
            now=datetime(2026, 9, 17, 11, 1, tzinfo=timezone.utc),
        )

        self.assertTrue(preview["closure_allowed"])
        self.assertEqual(preview["computed_hours"], 2)

    def test_rejects_close_time_before_start_time(self) -> None:
        preview = build_task_closure_preview(
            {"task_id": 120310, "start_time": "2026-09-17T10:00:00+00:00", "close_time": "2026-09-17T09:00:00+00:00"},
        )

        self.assertFalse(preview["closure_allowed"])