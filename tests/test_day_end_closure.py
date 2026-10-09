from __future__ import annotations

import importlib.util
import unittest
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from kwikledgers_langgraph.adapters import DayEndClosurePorts
from kwikledgers_langgraph.day_end_closure_policy import build_day_end_preview, parse_daily_log_entries
from kwikledgers_langgraph.fastmcp_adapters import FastMCPDayEndClosureAdapter
from kwikledgers_langgraph.observability import build_observability_context
from kwikledgers_langgraph.legacy_adapters import LegacyDayEndClosureAdapter
from kwikledgers_langgraph.task_completion_hook import completed_task_ids
from kwikledgers_langgraph.task_completion_hook import main as run_completion_hook
import io
from kwikledgers_langgraph.guardrails import ApprovalRequiredError, OperationKind, evaluate_operation

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None
if LANGGRAPH_AVAILABLE:
    from kwikledgers_langgraph.graphs.day_end_closure import build_day_end_closure_graph

LOG = """# Daily Action Log

### 2026-10-05 12:00:00Z - Resumo diario agendado
Fonte: task-scheduler

x

### 2026-10-05 13:00:00Z - Implementacao da task 10
Fonte: agent

y
"""
TASK = {"id": 10, "title": "Backend", "parent_id": 5, "activated_at": "2026-10-05T11:00:00Z", "completed_work_hours": 1}
REQUEST = {"trigger": "scheduled", "cutoff_time": "18:00", "timezone": "UTC", "now": "2026-10-05T18:05:00Z"}


def _context(tasks: list[dict[str, Any]], log: str = LOG) -> dict[str, Any]:
    return {"active_tasks": tasks, "activity_entries": parse_daily_log_entries(log)}


class FakeAdapter:
    def __init__(self, tasks: list[dict[str, Any]], log: str = LOG) -> None:
        self.context = _context(tasks, log)
        self.executed: list[dict[str, Any]] = []

    def collect_day_context(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.context

    def execute_day_end_closure(self, request: dict[str, Any]) -> dict[str, Any]:
        self.executed.append(request)
        return {"status": "closed", "task_id": 10, "completed_work_hours": 6.0, "continuation_task_id": 11}


class DayEndPolicyTestCase(unittest.TestCase):
    def test_ignores_scheduler_entries_and_counts_hours_until_cutoff(self) -> None:
        preview = build_day_end_preview(REQUEST, _context([TASK]))
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["computed_hours"], 5)
        self.assertEqual(preview["completed_work_hours"], 6.0)
        self.assertEqual(preview["continuation"]["title"], "Backend (continuação)")

    def test_atomic_close_is_not_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wit = Mock()
            wit.get_work_item.return_value = SimpleNamespace(rev=4, fields={
                "System.State": "Active", "System.WorkItemType": "Task",
                "System.AssignedTo": {"uniqueName": "dev@example.com"},
            })
            azure = ModuleType("azure_devops.server")
            azure.get_client = Mock(return_value=SimpleNamespace(clients=SimpleNamespace(get_work_item_tracking_client=lambda: wit)))
            azure._resolve_active_user_email = lambda: "dev@example.com"
            tracking = ModuleType("local_tracking.server")
            tracking.TRACKING_DIR = Path(temp_dir)
            tracking._append_daily_action_log = Mock()
            modules = {"azure_devops": SimpleNamespace(server=azure), "local_tracking": SimpleNamespace(server=tracking)}
            preview = build_day_end_preview({**REQUEST, "create_continuation": False}, _context([TASK]))
            preview.update(expected_revision=4, assigned_to="dev@example.com")
            adapter = LegacyDayEndClosureAdapter()
            with patch.dict(sys.modules, modules), patch("kwikledgers_langgraph.legacy_adapters._ensure_legacy_import_path"):
                result = adapter.execute_day_end_closure({"preview": preview})
                self.assertEqual(result["status"], "closed")
                with self.assertRaises(RuntimeError):
                    adapter.execute_day_end_closure({"preview": preview})
            wit.update_work_item.assert_called_once()
            document = wit.update_work_item.call_args.kwargs["document"]
            self.assertEqual(document[0], {"op": "test", "path": "/rev", "value": 4})
            self.assertIn({"op": "add", "path": "/fields/System.State", "value": "Closed"}, document)
            self.assertFalse(any("RemainingWork" in operation["path"] for operation in document))

    def test_skips_without_human_activity(self) -> None:
        log = "### 2026-10-05 12:00:00Z - Resumo\nFonte: task-scheduler\n"
        self.assertEqual(build_day_end_preview(REQUEST, _context([TASK], log))["skip_reason"], "no_activity")

    def test_skips_on_multiple_active_tasks(self) -> None:
        preview = build_day_end_preview(REQUEST, _context([TASK, {**TASK, "id": 12}]))
        self.assertEqual(preview["skip_reason"], "ambiguous_active_tasks")

    def test_unrelated_activity_does_not_charge_task(self) -> None:
        preview = build_day_end_preview(REQUEST, _context([TASK], LOG.replace("task 10", "task 100")))
        self.assertEqual(preview["skip_reason"], "no_task_activity")

    def test_manual_summary_does_not_count_as_work(self) -> None:
        preview = build_day_end_preview(REQUEST, _context([TASK], LOG.replace("Implementacao da task 10", "Resumo da task 10")))
        self.assertEqual(preview["skip_reason"], "no_activity")

    def test_scheduled_execution_before_cutoff_does_nothing(self) -> None:
        preview = build_day_end_preview({**REQUEST, "now": "2026-10-05T17:00:00Z"}, _context([TASK]))
        self.assertEqual(preview["skip_reason"], "before_cutoff")

    def test_old_activity_does_not_charge_continuation(self) -> None:
        preview = build_day_end_preview(REQUEST, _context([{**TASK, "activated_at": "2026-10-05T17:00:00Z"}]))
        self.assertEqual(preview["skip_reason"], "no_task_activity")

    def test_task_reference_in_details_is_recognized(self) -> None:
        preview = build_day_end_preview(REQUEST, _context([TASK], LOG.replace("Implementacao da task 10", "Implementacao").replace("\ny\n", "\nTrabalho na task 10.\n")))
        self.assertEqual(preview["status"], "ready")

    def test_completed_hook_closes_only_target_without_continuation(self) -> None:
        preview = build_day_end_preview({**REQUEST, "trigger": "completed", "task_id": 10}, _context([TASK, {**TASK, "id": 12}]))
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["task"]["id"], 10)
        self.assertIsNone(preview["continuation"])
        self.assertEqual(preview["window_end"], "2026-10-05T18:05:00+00:00")

    def test_hook_requires_explicit_completion_with_passing_tests(self) -> None:
        entry = {"source": "task-completed", "timestamp": "2026-10-05T18:00:00+00:00", "details": '{"task_id": 10, "completed": true, "tests_passed": true, "session_id": "session-1"}'}
        now = datetime(2026, 10, 5, 18, 5, tzinfo=timezone.utc)
        self.assertEqual(completed_task_ids([entry], now, "session-1"), [10])
        self.assertEqual(completed_task_ids([entry], now, "foreign-session"), [])
        self.assertEqual(completed_task_ids([{**entry, "source": "agent"}], now, "session-1"), [])
        self.assertEqual(completed_task_ids([{**entry, "details": entry["details"].replace('"tests_passed": true', '"tests_passed": false')}], now, "session-1"), [])
        self.assertEqual(completed_task_ids([{**entry, "timestamp": "2026-10-04T18:00:00+00:00"}], now, "session-1"), [])

    def test_hook_does_not_process_a_local_only_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("kwikledgers_langgraph.task_completion_hook.resolve_ai_tracking_root", return_value=Path(temp_dir) / "AI_Tracking"), patch("kwikledgers_langgraph.task_completion_hook.close_task") as close:
                with patch("sys.stdin", io.StringIO('{"sessionId":"idle-1", "prompt":"[kwikledgers-local-only]"}')), patch("sys.stdout", io.StringIO()):
                    self.assertEqual(run_completion_hook(["--event", "UserPromptSubmit"]), 0)
                with patch("sys.stdin", io.StringIO('{"sessionId":"idle-1"}')):
                    self.assertEqual(run_completion_hook([]), 0)
                close.assert_not_called()

    def test_delayed_hook_uses_the_recorded_completion_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_directory = Path(temp_dir) / "AI_Tracking" / "Daily_Action_Logs"
            log_directory.mkdir(parents=True)
            log_directory.joinpath("2026-10-05.md").write_text(
                '### 2026-10-05 18:00:00Z - Task KL-10 concluida\nFonte: task-completed\n'
                '{"task_id":10,"completed":true,"tests_passed":true,"session_id":"session-1"}\n',
                encoding="utf-8",
            )
            with patch("kwikledgers_langgraph.task_completion_hook.resolve_ai_tracking_root", return_value=Path(temp_dir) / "AI_Tracking"), patch("kwikledgers_langgraph.task_completion_hook.close_task", return_value=0) as close, patch("kwikledgers_langgraph.task_completion_hook.datetime", wraps=datetime) as clock, patch("sys.stdout", io.StringIO()):
                clock.now.return_value = datetime(2026, 10, 5, 19, 5, tzinfo=timezone.utc)
                with patch("sys.stdin", io.StringIO('{"sessionId":"session-1"}')):
                    self.assertEqual(run_completion_hook(["--event", "SessionStart"]), 0)
                with patch("sys.stdin", io.StringIO('{"sessionId":"session-1"}')):
                    self.assertEqual(run_completion_hook([]), 0)
                close.assert_called_once()
                arguments = close.call_args.args[0]
                self.assertEqual(arguments[arguments.index("--completed-at") + 1], "2026-10-05T18:00:00+00:00")

    def test_completion_without_separate_work_activity_does_not_charge(self) -> None:
        log = '### 2026-10-05 13:00:00Z - Task KL-10 concluida\nFonte: task-completed\n{"task_id":10,"completed":true,"tests_passed":true}\n'
        preview = build_day_end_preview({**REQUEST, "trigger": "completed", "task_id": 10}, _context([TASK], log))
        self.assertEqual(preview["skip_reason"], "no_task_activity")

    def test_hours_exception_cannot_authorize_another_flow(self) -> None:
        with self.assertRaises(ApprovalRequiredError):
            evaluate_operation({"observability": {"flow_name": "story_status_transition"}}, action="day_end_closure.execute", operation_kind=OperationKind.AUTONOMOUS_AZURE_HOURS, preview={"status": "ready"})
        with self.assertRaises(ApprovalRequiredError):
            evaluate_operation({}, action="update_story_status", operation_kind=OperationKind.AUTONOMOUS_AZURE_HOURS, preview={"status": "ready"})


class NativeDayEndTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        logs = self.root / "Daily_Action_Logs"
        logs.mkdir()
        (logs / "2026-10-05.md").write_text(LOG, encoding="utf-8")
        replacement = patch("kwikledgers_langgraph.fastmcp_adapters.resolve_ai_tracking_root", return_value=self.root)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.details = {**TASK, "revision": 4, "work_item_type": "Task", "state": "Active", "assigned_to_email": "dev@example.test"}
        self.listed_task = dict(self.details)
        self.client = Mock()
        self.client.call.side_effect = lambda name, arguments: (
            {"user_email": "dev@example.test", "items": [self.listed_task]}
            if name == "get_my_work_items" else dict(self.details)
        )
        self.adapter = FastMCPDayEndClosureAdapter(self.client)

    def test_native_context_preserves_exact_task_details(self) -> None:
        context = self.adapter.collect_day_context(REQUEST)
        preview = build_day_end_preview(REQUEST, context)
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["expected_revision"], 4)
        self.assertEqual(preview["assigned_to"], "dev@example.test")
        self.assertEqual(preview["completed_work_hours"], 6.0)
        self.assertEqual([call.args[0] for call in self.client.call.call_args_list], ["get_my_work_items", "get_story_details"])

    def test_no_activity_requires_no_azure_read(self) -> None:
        (self.root / "Daily_Action_Logs" / "2026-10-05.md").unlink()
        self.assertEqual(self.adapter.collect_day_context(REQUEST)["active_tasks"], [])
        self.client.call.assert_not_called()

    def test_invalid_task_details_fail_closed(self) -> None:
        for field, value in (("id", 11), ("revision", True), ("assigned_to_email", "other@example.test"),
                             ("activated_at", None), ("state", "Closed"), ("work_item_type", "Bug")):
            with self.subTest(field=field), patch.dict(self.details, {field: value}), self.assertRaises(ValueError):
                self.adapter.collect_day_context(REQUEST)

    def test_incomplete_journal_blocks_new_preview(self) -> None:
        journals = self.root / "Task_Control" / "day-end-closures"
        journals.mkdir(parents=True)
        (journals / "10.json").write_text('{"phase":"started"}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.adapter.collect_day_context(REQUEST)
        self.client.call.assert_not_called()

    def test_native_execution_is_not_available_without_lifecycle(self) -> None:
        with self.assertRaises(RuntimeError):
            self.adapter.execute_day_end_closure({"preview": {"status": "ready"}})
        self.client.call.assert_not_called()


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph não está instalado neste ambiente")
class DayEndGraphTestCase(unittest.TestCase):
    def _run(self, adapter: FakeAdapter, **request: Any) -> dict[str, Any]:
        runtime = build_day_end_closure_graph(DayEndClosurePorts(day_end=adapter))
        return runtime.invoke({
            "tracking_events": [],
            "observability": build_observability_context(flow_name="day_end_closure", source="test", backend_name="legacy"),
            "day_end_closure_request": {**REQUEST, **request},
        })

    def test_all_triggers_preview_without_mutation(self) -> None:
        for trigger in ("manual", "scheduled", "completed"):
            with self.subTest(trigger=trigger):
                adapter = FakeAdapter([TASK])
                result = self._run(adapter, trigger=trigger, task_id=10)
                self.assertEqual(adapter.executed, [])
                self.assertEqual(result["day_end_closure_result"]["status"], "preview_ready")
                self.assertEqual(result["day_end_closure_result"]["reason"], "human_approval_required")

    def test_unverified_approval_cannot_enable_execution(self) -> None:
        adapter = FakeAdapter([TASK])
        runtime = build_day_end_closure_graph(DayEndClosurePorts(day_end=adapter))
        result = runtime.invoke({
            "tracking_events": [],
            "day_end_closure_request": REQUEST,
            "pending_approval": {"approved": True, "action": "day_end_closure.execute"},
        })
        self.assertEqual(adapter.executed, [])
        self.assertFalse(result["day_end_closure_result"]["changed"])

    def test_dry_run_does_not_write(self) -> None:
        adapter = FakeAdapter([TASK])
        result = self._run(adapter, dry_run=True)
        self.assertEqual(adapter.executed, [])
        self.assertEqual(result["day_end_closure_result"]["status"], "dry_run")

    def test_no_activity_leaves_board_alone(self) -> None:
        adapter = FakeAdapter([TASK], log="")
        result = self._run(adapter)
        self.assertEqual(adapter.executed, [])
        self.assertEqual(result["day_end_closure_result"]["reason"], "no_activity")


if __name__ == "__main__":
    unittest.main()
