from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.adapters import StoryStatusTransitionPorts
from kwikledgers_langgraph.story_status_cli import (
    _build_initial_state,
    _build_request,
    _build_runtime_state,
    _write_state_file,
    main,
    run_story_status_transition,
)

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


def _build_arguments(**overrides: object) -> argparse.Namespace:
    base = {
        "story_id": 120204,
        "target_status": "Aguardando PR",
        "expected_current_status": "Desenvolvimento",
        "source": "test-story-status-cli",
        "backend": None,
        "approve_transition": False,
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


class FakeStoryStatusTransitionAdapter:
    def __init__(self) -> None:
        self.preview_calls: list[dict[str, object]] = []
        self.execute_calls: list[dict[str, object]] = []

    def preview_story_status_transition(self, request: dict[str, object]) -> dict[str, object]:
        self.preview_calls.append(request)
        return {
            "story_id": request.get("story_id"),
            "current_status": "Desenvolvimento",
            "target_status": request.get("target_status"),
            "transition_allowed": True,
            "validation_messages": [],
            "story_revision": "rev-1",
        }

    def update_story_status(self, request: dict[str, object]) -> dict[str, object]:
        self.execute_calls.append(request)
        return {
            "status": "updated",
            "story_id": request.get("story_id"),
            "previous_status": "Desenvolvimento",
            "current_status": request.get("target_status"),
            "changed": True,
        }


class StoryStatusCliTestCase(unittest.TestCase):
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
        arguments = _build_arguments()

        request = _build_request(arguments)

        self.assertEqual(request["story_id"], 120204)
        self.assertEqual(request["target_status"], "Aguardando PR")
        self.assertEqual(request["expected_current_status"], "Desenvolvimento")

    def test_build_initial_state_seeds_observability_without_approval_by_default(self) -> None:
        arguments = _build_arguments()

        state = _build_initial_state(arguments)

        self.assertEqual(state["source"], "test-story-status-cli")
        self.assertEqual(state["story_status_request"]["story_id"], 120204)
        self.assertNotIn("pending_approval", state)
        self.assertEqual(state["observability"]["flow_name"], "story_status_transition")
        self.assertEqual(state["observability"]["backend_name"], "legacy")
        self.assertEqual(state["observability"]["timeline"][0]["status"], "initialized")

    def test_build_initial_state_does_not_allow_cli_approval_simulation(self) -> None:
        arguments = _build_arguments(approve_transition=True)

        state = _build_initial_state(arguments)

        self.assertNotIn("approval_simulation_requested", state)
        self.assertNotIn("pending_approval", state)

    def test_build_runtime_state_rehydrates_saved_state_with_approval_confirmation(self) -> None:
        saved_state = {
            "story_status_request": {"story_id": 120204, "target_status": "Aguardando PR"},
            "story_status_preview": {
                "story_id": 120204,
                "current_status": "Desenvolvimento",
                "target_status": "Aguardando PR",
                "transition_allowed": True,
                "validation_messages": [],
                "story_revision": "rev-1",
            },
            "pending_approval": {
                "action": "story_status_transition.execute",
                "preview": {
                    "story_id": 120204,
                    "current_status": "Desenvolvimento",
                    "target_status": "Aguardando PR",
                    "transition_allowed": True,
                    "validation_messages": [],
                    "story_revision": "rev-1",
                },
                "approved": False,
                "request_id": "req-123",
                "run_id": "run-123",
                "approval_source": "pending_review",
            },
            "observability": {"active_run_id": "run-123", "flow_name": "story_status_transition"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/saved-state.json"
            _write_state_file(saved_state, state_file)

            state = _build_runtime_state(
                _build_arguments(
                    resume_state_file=state_file,
                    approval_request_id="req-123",
                )
            )

        self.assertEqual(state["approval_confirmation"]["request_id"], "req-123")
        self.assertEqual(state["approval_confirmation"]["approval_source"], "human_cli_confirmation")
        self.assertEqual(state["observability"]["active_run_id"], "run-123")

    def test_run_story_status_transition_invokes_runtime_with_built_state(self) -> None:
        arguments = _build_arguments()
        runtime = FakeRuntime(result={"story_status_result": {"status": "approval_required"}})

        with patch("kwikledgers_langgraph.story_status_cli.build_story_status_transition_runtime", return_value=runtime):
            result = run_story_status_transition(arguments)

        self.assertEqual(result["story_status_result"]["status"], "approval_required")
        self.assertEqual(len(runtime.invocations), 1)
        self.assertEqual(
            runtime.invocations[0]["story_status_request"]["target_status"],
            "Aguardando PR",
        )

    def test_run_story_status_transition_rejects_invalid_runtime_result(self) -> None:
        arguments = _build_arguments()
        runtime = FakeRuntime(result="invalid")

        with patch("kwikledgers_langgraph.story_status_cli.build_story_status_transition_runtime", return_value=runtime):
            with self.assertRaises(RuntimeError):
                run_story_status_transition(arguments)

    def test_build_runtime_state_rejects_backend_mismatch_on_resume(self) -> None:
        saved_state = {
            "story_status_request": {"story_id": 120204, "target_status": "Aguardando PR"},
            "story_status_preview": {
                "story_id": 120204,
                "current_status": "Desenvolvimento",
                "target_status": "Aguardando PR",
                "transition_allowed": True,
                "validation_messages": [],
                "story_revision": "rev-1",
            },
            "pending_approval": {
                "action": "story_status_transition.execute",
                "preview": {
                    "story_id": 120204,
                    "current_status": "Desenvolvimento",
                    "target_status": "Aguardando PR",
                    "transition_allowed": True,
                    "validation_messages": [],
                    "story_revision": "rev-1",
                },
                "approved": False,
                "request_id": "req-123",
                "run_id": "run-123",
                "approval_source": "pending_review",
            },
            "observability": {
                "active_run_id": "run-123",
                "flow_name": "story_status_transition",
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
                        approval_request_id="req-123",
                        backend="fastmcp",
                    )
                )

    def test_main_writes_state_file_when_requested(self) -> None:
        result = {
            "story_status_result": {"status": "approval_required"},
            "pending_approval": {"request_id": "req-123"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/cli-state.json"
            argv = [
                "--story-id",
                "120204",
                "--target-status",
                "Aguardando PR",
                "--save-state-file",
                state_file,
            ]

            with patch("kwikledgers_langgraph.story_status_cli.run_story_status_transition", return_value=result):
                with patch("kwikledgers_langgraph.story_status_cli._record_runtime_observability"):
                    exit_code = main(argv)

            with open(state_file, "r", encoding="utf-8") as saved_file:
                saved_state = json.load(saved_file)

        self.assertEqual(exit_code, 0)
        self.assertEqual(saved_state["state"]["pending_approval"]["request_id"], "req-123")
        self.assertIn("signature", saved_state)

    @unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph nao esta instalado neste ambiente")
    def test_run_story_status_transition_replays_saved_state_and_executes_after_human_approval(self) -> None:
        adapter = FakeStoryStatusTransitionAdapter()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/preview-state.json"
            with patch(
                "kwikledgers_langgraph.story_status_cli.build_story_status_transition_ports",
                return_value=StoryStatusTransitionPorts(statuses=adapter),
            ):
                preview_result = run_story_status_transition(_build_arguments())
                _write_state_file(preview_result, state_file)

                resume_result = run_story_status_transition(
                    _build_arguments(
                        resume_state_file=state_file,
                        approval_request_id=preview_result["pending_approval"]["request_id"],
                    )
                )

        self.assertEqual(preview_result["story_status_result"]["status"], "approval_required")
        self.assertEqual(len(adapter.preview_calls), 2)
        self.assertEqual(len(adapter.execute_calls), 1)
        self.assertEqual(resume_result["story_status_result"]["status"], "updated")


if __name__ == "__main__":
    unittest.main()