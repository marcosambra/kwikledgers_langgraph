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

    def test_fix_requires_own_task_and_summary(self) -> None:
        base = {"task_id": 10, "start_time": "2026-09-17T10:00:00+00:00", "work_kind": "fix"}
        now = datetime(2026, 9, 17, 11, 0, tzinfo=timezone.utc)

        same = build_task_closure_preview({**base, "origin_task_id": 10, "fix_summary": "x"}, now=now)
        missing = build_task_closure_preview({**base, "origin_task_id": 9}, now=now)
        ok = build_task_closure_preview({**base, "origin_task_id": 9, "fix_summary": "x"}, now=now)

        self.assertFalse(same["closure_allowed"])
        self.assertFalse(missing["closure_allowed"])
        self.assertTrue(ok["closure_allowed"])
