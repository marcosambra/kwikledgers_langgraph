from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.adapters import TaskClosurePorts
from kwikledgers_langgraph.task_closure_cli import (
    _build_initial_state,
    _build_request,
    _build_runtime_state,
    _write_state_file,
    main,
    run_task_closure,
)

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


def _build_arguments(**overrides: object) -> argparse.Namespace:
    base = {
        "task_id": 120310,
        "start_time": "2026-07-10T11:00:00+00:00",
        "close_time": "2026-07-10T13:00:00+00:00",
        "completed_work_hours": None,
        "remaining_work_hours": None,
        "comment": None,
        "source": "test-task-closure-cli",
        "backend": None,
        "approve_closure": False,
        "resume_state_file": None,
        "approval_request_id": None,
        "approval_source": "human_cli_confirmation",
        "save_state_file": None,
        "print_state": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class FakeRuntime:
    def __init__(self, *, result: object) -> None:
        self.result = result
        self.invocations: list[dict[str, object]] = []

    def invoke(self, state: dict[str, object]) -> object:
        self.invocations.append(state)
        return self.result


class FakeTaskClosureAdapter:
    def __init__(self) -> None:
        self.preview_calls: list[dict[str, object]] = []
        self.execute_calls: list[dict[str, object]] = []

    def preview_task_closure(self, request: dict[str, object]) -> dict[str, object]:
        self.preview_calls.append(request)
        return {
            "task_id": request.get("task_id"),
            "start_time": request.get("start_time"),
            "close_time": request.get("close_time"),
            "computed_hours": 2,
            "completed_work_hours": 2.0,
            "remaining_work_hours": request.get("remaining_work_hours"),
            "target_status": "Closed",
            "comment": request.get("comment"),
            "closure_allowed": True,
            "validation_messages": [],
        }

    def close_task(self, request: dict[str, object]) -> dict[str, object]:
        self.execute_calls.append(request)
        return {
            "status": "closed",
            "task_id": request.get("task_id"),
            "completed_work_hours": 2.0,
            "remaining_work_hours": request.get("remaining_work_hours"),
            "target_status": "Closed",
            "changed": True,
        }


class TaskClosureCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.signing_key_patcher = patch.dict(
            os.environ,
            {"KWIKLEDGERS_LANGGRAPH_STATE_SIGNING_KEY": "test-signing-key"},
            clear=False,
        )
        self.signing_key_patcher.start()

    def tearDown(self) -> None:
        self.signing_key_patcher.stop()

    def test_build_request_maps_optional_and_required_fields(self) -> None:
        arguments = _build_arguments(completed_work_hours=3.0, remaining_work_hours=1.0, comment="fechamento")

        request = _build_request(arguments)

        self.assertEqual(request["task_id"], 120310)
        self.assertEqual(request["start_time"], "2026-07-10T11:00:00+00:00")
        self.assertEqual(request["close_time"], "2026-07-10T13:00:00+00:00")
        self.assertEqual(request["completed_work_hours"], 3.0)
        self.assertEqual(request["remaining_work_hours"], 1.0)
        self.assertEqual(request["comment"], "fechamento")

    def test_build_initial_state_seeds_observability_without_approval_by_default(self) -> None:
        arguments = _build_arguments()

        state = _build_initial_state(arguments)

        self.assertEqual(state["source"], "test-task-closure-cli")
        self.assertEqual(state["task_closure_request"]["task_id"], 120310)
        self.assertNotIn("pending_approval", state)
        self.assertEqual(state["observability"]["flow_name"], "task_closure")
        self.assertEqual(state["observability"]["backend_name"], "fastmcp")

    def test_build_initial_state_does_not_allow_cli_approval_simulation(self) -> None:
        arguments = _build_arguments(approve_closure=True)

        state = _build_initial_state(arguments)

        self.assertNotIn("approval_simulation_requested", state)
        self.assertNotIn("pending_approval", state)

    def test_build_runtime_state_rehydrates_saved_state_with_approval_confirmation(self) -> None:
        saved_state = {
            "task_closure_request": {"task_id": 120310, "start_time": "2026-07-10T11:00:00+00:00"},
            "task_closure_preview": {
                "task_id": 120310,
                "computed_hours": 2,
                "completed_work_hours": 2.0,
                "remaining_work_hours": None,
                "closure_allowed": True,
                "validation_messages": [],
            },
            "pending_approval": {
                "action": "task_closure.execute",
                "preview": {
                    "task_id": 120310,
                    "computed_hours": 2,
                    "completed_work_hours": 2.0,
                    "remaining_work_hours": None,
                    "closure_allowed": True,
                    "validation_messages": [],
                },
                "approved": False,
                "request_id": "req-456",
                "run_id": "run-456",
                "approval_source": "pending_review",
            },
            "observability": {"active_run_id": "run-456", "flow_name": "task_closure"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/saved-state.json"
            _write_state_file(saved_state, state_file)

            state = _build_runtime_state(
                _build_arguments(
                    resume_state_file=state_file,
                    approval_request_id="req-456",
                )
            )

        self.assertEqual(state["approval_confirmation"]["request_id"], "req-456")
        self.assertEqual(state["approval_confirmation"]["approval_source"], "human_cli_confirmation")
        self.assertEqual(state["observability"]["active_run_id"], "run-456")

    def test_run_task_closure_invokes_runtime_with_built_state(self) -> None:
        arguments = _build_arguments()
        runtime = FakeRuntime(result={"task_closure_result": {"status": "approval_required"}})

        with patch("kwikledgers_langgraph.task_closure_cli.build_task_closure_runtime", return_value=runtime):
            result = run_task_closure(arguments)

        self.assertEqual(result["task_closure_result"]["status"], "approval_required")
        self.assertEqual(len(runtime.invocations), 1)
        self.assertEqual(runtime.invocations[0]["task_closure_request"]["task_id"], 120310)

    def test_run_task_closure_rejects_invalid_runtime_result(self) -> None:
        arguments = _build_arguments()
        runtime = FakeRuntime(result="invalid")

        with patch("kwikledgers_langgraph.task_closure_cli.build_task_closure_runtime", return_value=runtime):
            with self.assertRaises(RuntimeError):
                run_task_closure(arguments)

    def test_build_runtime_state_rejects_backend_mismatch_on_resume(self) -> None:
        saved_state = {
            "task_closure_request": {"task_id": 120310, "start_time": "2026-07-10T11:00:00+00:00"},
            "task_closure_preview": {
                "task_id": 120310,
                "computed_hours": 2,
                "completed_work_hours": 2.0,
                "remaining_work_hours": None,
                "closure_allowed": True,
                "validation_messages": [],
            },
            "pending_approval": {
                "action": "task_closure.execute",
                "preview": {
                    "task_id": 120310,
                    "computed_hours": 2,
                    "completed_work_hours": 2.0,
                    "remaining_work_hours": None,
                    "closure_allowed": True,
                    "validation_messages": [],
                },
                "approved": False,
                "request_id": "req-456",
                "run_id": "run-456",
                "approval_source": "pending_review",
            },
            "observability": {
                "active_run_id": "run-456",
                "flow_name": "task_closure",
                "backend_name": "legacy",
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/saved-state.json"
            _write_state_file(saved_state, state_file)

            with self.assertRaises(RuntimeError):
                _build_runtime_state(
                    _build_arguments(
                        resume_state_file=state_file,
                        approval_request_id="req-456",
                        backend="fastmcp",
                    )
                )

    def test_main_writes_state_file_when_requested(self) -> None:
        result = {
            "task_closure_result": {"status": "approval_required"},
            "pending_approval": {"request_id": "req-456"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/cli-state.json"
            argv = [
                "--task-id",
                "120310",
                "--start-time",
                "2026-07-10T11:00:00+00:00",
                "--save-state-file",
                state_file,
            ]

            with patch("kwikledgers_langgraph.task_closure_cli.run_task_closure", return_value=result):
                with patch("kwikledgers_langgraph.task_closure_cli._record_runtime_observability"):
                    exit_code = main(argv)

            with open(state_file, "r", encoding="utf-8") as saved_file:
                saved_state = json.load(saved_file)

        self.assertEqual(exit_code, 0)
        self.assertEqual(saved_state["state"]["pending_approval"]["request_id"], "req-456")
        self.assertIn("signature", saved_state)

    @unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph nao esta instalado neste ambiente")
    def test_run_task_closure_replays_saved_state_and_executes_after_human_approval(self) -> None:
        adapter = FakeTaskClosureAdapter()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/preview-state.json"
            with patch(
                "kwikledgers_langgraph.task_closure_cli.build_task_closure_ports",
                return_value=TaskClosurePorts(closures=adapter),
            ):
                preview_result = run_task_closure(_build_arguments(backend="legacy"))
                _write_state_file(preview_result, state_file)

                resume_result = run_task_closure(
                    _build_arguments(
                        backend="legacy",
                        resume_state_file=state_file,
                        approval_request_id=preview_result["pending_approval"]["request_id"],
                    )
                )

        self.assertEqual(len(adapter.execute_calls), 1)
        self.assertEqual(resume_result["task_closure_result"]["status"], "closed")


if __name__ == "__main__":
    unittest.main()
