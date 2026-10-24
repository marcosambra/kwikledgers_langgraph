from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
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
from kwikledgers_langgraph.day_end_closure_cli import main as run_day_end_cli
from kwikledgers_langgraph.observability import build_observability_context
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


class DayEndEntryPointTestCase(unittest.TestCase):
    def test_cli_defaults_to_backend_selection_without_forcing_a_backend(self) -> None:
        state = {"day_end_closure_result": {"status": "skipped", "reason": "no_activity", "changed": False}}
        with patch("kwikledgers_langgraph.day_end_closure_cli.run_day_end_closure", return_value=state) as run, patch("sys.stdout", io.StringIO()):
            self.assertEqual(run_day_end_cli(["--disable-observability-sync"]), 0)
        self.assertIsNone(run.call_args.args[0].backend)

    def test_notification_can_sign_with_key_from_native_environment_file(self) -> None:
        state = {"day_end_closure_result": {"status": "preview_ready", "changed": False},
                 "observability": {"active_run_id": "01234567-89ab-4cde-abcd-0123456789ab"}}
        with tempfile.TemporaryDirectory() as directory:
            environment_file = Path(directory) / ".env"
            environment_file.write_text("KWIKLEDGERS_FASTMCP_EXECUTION_KEY=" + "n" * 32 + "\n", encoding="utf-8")
            with patch.dict(os.environ, {"KWIKLEDGERS_ENV_FILE": str(environment_file)}), patch("kwikledgers_langgraph.day_end_closure_cli.run_day_end_closure", return_value=state), patch("kwikledgers_langgraph.day_end_closure_cli.FastMCPNotificationAdapter") as adapter, patch("sys.stdout", io.StringIO()):
                os.environ.pop("KWIKLEDGERS_FASTMCP_EXECUTION_KEY", None)
                self.assertEqual(run_day_end_cli(["--notify", "--disable-observability-sync"]), 0)
                provider = adapter.call_args.kwargs["execution_capability_provider"]
                token = provider("send_notification", {"title": "Preview", "message": "No Azure writes", "urgency": "normal"})
                self.assertIn(".", token)
                adapter.return_value.send_notification.assert_called_once()

    def test_cron_generator_is_native_automatic_and_rejects_unsafe_environment_paths(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scripts" / "register-day-end-closure.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "kwikledgers_langgraph" / "scripts" / source.name
            script.parent.mkdir(parents=True)
            shutil.copyfile(source, script)
            commands = root / "commands"
            commands.mkdir()
            captured = root / "installed-cron"
            for name, content in {
                "cmp": "#!/bin/sh\nexit 0\n",
                "crontab": '#!/bin/sh\nif [ "$1" = "-l" ]; then\n echo "no crontab for fixture" >&2\n exit 1\nfi\ncp "$1" "$TEST_CRON_CAPTURE"\n',
            }.items():
                command = commands / name
                command.write_text(content, encoding="utf-8")
                command.chmod(0o700)
            for project in ("kwikledgers_langgraph", "kwikledgers_fastmcp"):
                interpreter = root / project / ".venv" / "bin" / "python"
                interpreter.parent.mkdir(parents=True)
                interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                interpreter.chmod(0o700)
            env_file = root / "native config.env"
            env_file.write_text("", encoding="utf-8")
            environment = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                           "TEST_CRON_CAPTURE": str(captured), "KWIKLEDGERS_ENV_FILE": str(env_file)}
            result = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            scheduled = captured.read_text(encoding="utf-8")
            self.assertIn("--backend fastmcp --timezone", scheduled)
            self.assertNotIn("--dry-run", scheduled)
            self.assertIn("KWIKLEDGERS_ROOT_DIR=", scheduled)
            self.assertIn("KWIKLEDGERS_FASTMCP_COMMAND=", scheduled)
            self.assertNotIn("agent/.env", scheduled)
            captured.unlink()
            unsafe = root / "unsafe%config.env"
            unsafe.write_text("", encoding="utf-8")
            for path in ("relative.env", str(unsafe), str(root / "missing.env")):
                with self.subTest(path=path):
                    result = subprocess.run(["bash", str(script)], env={**environment, "KWIKLEDGERS_ENV_FILE": path}, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(captured.exists())


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


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph não está instalado neste ambiente")
class DayEndGraphTestCase(unittest.TestCase):
    def _run(self, adapter: FakeAdapter, **request: Any) -> dict[str, Any]:
        runtime = build_day_end_closure_graph(DayEndClosurePorts(day_end=adapter))
        return runtime.invoke({
            "tracking_events": [],
            "observability": build_observability_context(flow_name="day_end_closure", source="test", backend_name="fastmcp"),
            "day_end_closure_request": {**REQUEST, **request},
        })

    def test_executes_autonomously_without_approval(self) -> None:
        adapter = FakeAdapter([TASK])
        result = self._run(adapter)
        self.assertEqual(len(adapter.executed), 1)
        self.assertEqual(result["day_end_closure_result"]["status"], "closed")

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
