from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.adapters import StoryPullRequestPorts
from kwikledgers_langgraph.story_pr_cli import (
    _build_initial_state,
    _build_request,
    _build_runtime_state,
    main,
    _write_state_file,
    run_story_pull_request,
)

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


def _build_arguments(**overrides: object) -> argparse.Namespace:
    base = {
        "story_id": 120204,
        "repository_name": "accounting",
        "source_branch": "feature/KL-120204-balance-sheet-by-class",
        "what_was_changed": "Ajustado o fluxo de PR da story.",
        "affected_processes": "Daily summary e PR preview.",
        "expected_impacts": "Melhor previsibilidade do runtime.",
        "important_points": "Validar approval gate.",
        "mermaid_diagram": "graph TD; A-->B;",
        "source": "test-story-pr-cli",
        "backend": None,
        "title": "KL-120204 Draft PR",
        "related_work_item_ids": [120205, 120206],
        "exclude_child_work_items": False,
        "tests_updated": True,
        "tests_unchanged": False,
        "new_library_name": None,
        "env_changes": None,
        "generated_migration_or_seed": None,
        "new_queue_or_command": None,
        "approve_create": False,
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


class FakeStoryPullRequestAdapter:
    def __init__(self) -> None:
        self.preview_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []

    def preview_story_pull_request(self, request: dict[str, object]) -> dict[str, object]:
        self.preview_calls.append(request)
        return {
            "title": "KL-120204 Draft PR",
            "story_id": request.get("story_id"),
        }

    def create_story_pull_request(self, request: dict[str, object]) -> dict[str, object]:
        self.create_calls.append(request)
        return {
            "status": "created",
            "title": "KL-120204 Draft PR",
            "story_id": request.get("story_id"),
        }


class StoryPullRequestCliTestCase(unittest.TestCase):
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
        self.assertEqual(request["repository_name"], "accounting")
        self.assertEqual(request["related_work_item_ids"], [120205, 120206])
        self.assertEqual(request["include_child_work_items"], True)
        self.assertTrue(request["tests_updated"])

    def test_build_initial_state_seeds_observability_without_approval_by_default(self) -> None:
        arguments = _build_arguments()

        state = _build_initial_state(arguments)

        self.assertEqual(state["source"], "test-story-pr-cli")
        self.assertEqual(state["pull_request_request"]["story_id"], 120204)
        self.assertNotIn("pending_approval", state)
        self.assertEqual(state["observability"]["flow_name"], "story_pull_request")
        self.assertEqual(state["observability"]["backend_name"], "fastmcp")
        self.assertEqual(state["observability"]["timeline"][0]["status"], "initialized")

    def test_build_initial_state_does_not_allow_cli_approval_simulation(self) -> None:
        arguments = _build_arguments(approve_create=True)

        state = _build_initial_state(arguments)

        self.assertNotIn("approval_simulation_requested", state)
        self.assertNotIn("pending_approval", state)

    def test_build_runtime_state_rehydrates_saved_state_with_approval_confirmation(self) -> None:
        saved_state = {
            "pull_request_request": {"story_id": 120204, "repository_name": "accounting"},
            "pull_request_preview": {"title": "KL-120204 Draft PR", "story_id": 120204},
            "pending_approval": {
                "action": "story_pull_request.create",
                "preview": {"title": "KL-120204 Draft PR", "story_id": 120204},
                "approved": False,
                "request_id": "req-123",
                "run_id": "run-123",
                "approval_source": "pending_review",
            },
            "observability": {"active_run_id": "run-123", "flow_name": "story_pull_request"},
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

    def test_build_runtime_state_rejects_mismatched_resume_request_id(self) -> None:
        saved_state = {
            "pull_request_request": {"story_id": 120204, "repository_name": "accounting"},
            "pull_request_preview": {"title": "KL-120204 Draft PR", "story_id": 120204},
            "pending_approval": {
                "action": "story_pull_request.create",
                "preview": {"title": "KL-120204 Draft PR", "story_id": 120204},
                "approved": False,
                "request_id": "req-123",
                "run_id": "run-123",
                "approval_source": "pending_review",
            },
            "observability": {"active_run_id": "run-123", "flow_name": "story_pull_request"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/saved-state.json"
            _write_state_file(saved_state, state_file)

            with self.assertRaises(RuntimeError):
                _build_runtime_state(
                    _build_arguments(
                        resume_state_file=state_file,
                        approval_request_id="req-999",
                    )
                )

    def test_run_story_pull_request_invokes_runtime_with_built_state(self) -> None:
        arguments = _build_arguments()
        runtime = FakeRuntime(result={"pull_request_result": {"status": "approval_required"}})

        with patch("kwikledgers_langgraph.story_pr_cli.build_story_pull_request_runtime", return_value=runtime):
            result = run_story_pull_request(arguments)

        self.assertEqual(result["pull_request_result"]["status"], "approval_required")
        self.assertEqual(len(runtime.invocations), 1)
        self.assertEqual(
            runtime.invocations[0]["pull_request_request"]["repository_name"],
            "accounting",
        )

    def test_run_story_pull_request_rejects_invalid_runtime_result(self) -> None:
        arguments = _build_arguments()
        runtime = FakeRuntime(result="invalid")

        with patch("kwikledgers_langgraph.story_pr_cli.build_story_pull_request_runtime", return_value=runtime):
            with self.assertRaises(RuntimeError):
                run_story_pull_request(arguments)

    def test_main_writes_state_file_when_requested(self) -> None:
        result = {"pull_request_result": {"status": "approval_required"}, "pending_approval": {"request_id": "req-123"}}

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/cli-state.json"
            argv = [
                "--story-id",
                "120204",
                "--repository-name",
                "accounting",
                "--source-branch",
                "feature/KL-120204-balance-sheet-by-class",
                "--what-was-changed",
                "Ajustado o fluxo de PR da story.",
                "--affected-processes",
                "Daily summary e PR preview.",
                "--expected-impacts",
                "Melhor previsibilidade do runtime.",
                "--important-points",
                "Validar approval gate.",
                "--mermaid-diagram",
                "graph TD; A-->B;",
                "--save-state-file",
                state_file,
            ]

            with patch("kwikledgers_langgraph.story_pr_cli.run_story_pull_request", return_value=result):
                with patch("kwikledgers_langgraph.story_pr_cli._record_runtime_observability"):
                    exit_code = main(argv)

            with open(state_file, "r", encoding="utf-8") as saved_file:
                saved_state = json.load(saved_file)

        self.assertEqual(exit_code, 0)
        self.assertEqual(saved_state["state"]["pending_approval"]["request_id"], "req-123")
        self.assertIn("signature", saved_state)

if __name__ == "__main__":
    unittest.main()