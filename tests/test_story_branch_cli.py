from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.adapters import StoryBranchAssociationPorts
from kwikledgers_langgraph.story_branch_cli import (
    _build_initial_state,
    _build_request,
    _build_runtime_state,
    _write_state_file,
    main,
    run_story_branch_association,
)

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


def _build_arguments(**overrides: object) -> argparse.Namespace:
    base = {
        "story_id": 120204,
        "repository_name": "accounting",
        "source_branch": "feature/KL-120204-balance-sheet-by-class",
        "source": "test-story-branch-cli",
        "backend": None,
        "related_work_item_ids": [120205, 120206],
        "exclude_child_work_items": False,
        "approve_association": False,
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


class FakeStoryBranchAssociationAdapter:
    def __init__(self) -> None:
        self.preview_calls: list[dict[str, object]] = []
        self.execute_calls: list[dict[str, object]] = []

    def preview_story_branch_association(self, request: dict[str, object]) -> dict[str, object]:
        self.preview_calls.append(request)
        return {
            "story_id": request.get("story_id"),
            "repository_name": request.get("repository_name"),
        }

    def associate_story_branch(self, request: dict[str, object]) -> dict[str, object]:
        self.execute_calls.append(request)
        return {
            "status": "associated",
            "story_id": request.get("story_id"),
            "repository_name": request.get("repository_name"),
        }


class StoryBranchCliTestCase(unittest.TestCase):
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

    def test_build_initial_state_seeds_observability_without_approval_by_default(self) -> None:
        arguments = _build_arguments()

        state = _build_initial_state(arguments)

        self.assertEqual(state["source"], "test-story-branch-cli")
        self.assertEqual(state["branch_association_request"]["story_id"], 120204)
        self.assertNotIn("pending_approval", state)
        self.assertEqual(state["observability"]["flow_name"], "story_branch_association")
        self.assertEqual(state["observability"]["backend_name"], "legacy")
        self.assertEqual(state["observability"]["timeline"][0]["status"], "initialized")

    def test_build_initial_state_allows_explicit_local_approval_simulation(self) -> None:
        arguments = _build_arguments(approve_association=True)

        state = _build_initial_state(arguments)

        self.assertTrue(state["approval_simulation_requested"])
        self.assertNotIn("pending_approval", state)

    def test_build_runtime_state_rehydrates_saved_state_with_approval_confirmation(self) -> None:
        saved_state = {
            "branch_association_request": {"story_id": 120204, "repository_name": "accounting"},
            "branch_association_preview": {"story_id": 120204, "repository_name": "accounting"},
            "pending_approval": {
                "action": "story_branch_association.execute",
                "preview": {"story_id": 120204, "repository_name": "accounting"},
                "approved": False,
                "request_id": "req-123",
                "run_id": "run-123",
                "approval_source": "pending_review",
            },
            "observability": {"active_run_id": "run-123", "flow_name": "story_branch_association"},
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

    def test_run_story_branch_association_invokes_runtime_with_built_state(self) -> None:
        arguments = _build_arguments()
        runtime = FakeRuntime(result={"branch_association_result": {"status": "approval_required"}})

        with patch("kwikledgers_langgraph.story_branch_cli.build_story_branch_association_runtime", return_value=runtime):
            result = run_story_branch_association(arguments)

        self.assertEqual(result["branch_association_result"]["status"], "approval_required")
        self.assertEqual(len(runtime.invocations), 1)
        self.assertEqual(
            runtime.invocations[0]["branch_association_request"]["repository_name"],
            "accounting",
        )

    def test_run_story_branch_association_rejects_invalid_runtime_result(self) -> None:
        arguments = _build_arguments()
        runtime = FakeRuntime(result="invalid")

        with patch("kwikledgers_langgraph.story_branch_cli.build_story_branch_association_runtime", return_value=runtime):
            with self.assertRaises(RuntimeError):
                run_story_branch_association(arguments)

    def test_build_runtime_state_rejects_backend_mismatch_on_resume(self) -> None:
        saved_state = {
            "branch_association_request": {"story_id": 120204, "repository_name": "accounting"},
            "branch_association_preview": {"story_id": 120204, "repository_name": "accounting"},
            "pending_approval": {
                "action": "story_branch_association.execute",
                "preview": {"story_id": 120204, "repository_name": "accounting"},
                "approved": False,
                "request_id": "req-123",
                "run_id": "run-123",
                "approval_source": "pending_review",
            },
            "observability": {
                "active_run_id": "run-123",
                "flow_name": "story_branch_association",
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
            "branch_association_result": {"status": "approval_required"},
            "pending_approval": {"request_id": "req-123"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/cli-state.json"
            argv = [
                "--story-id",
                "120204",
                "--repository-name",
                "accounting",
                "--source-branch",
                "feature/KL-120204-balance-sheet-by-class",
                "--save-state-file",
                state_file,
            ]

            with patch("kwikledgers_langgraph.story_branch_cli.run_story_branch_association", return_value=result):
                with patch("kwikledgers_langgraph.story_branch_cli._record_runtime_observability"):
                    exit_code = main(argv)

            with open(state_file, "r", encoding="utf-8") as saved_file:
                saved_state = json.load(saved_file)

        self.assertEqual(exit_code, 0)
        self.assertEqual(saved_state["state"]["pending_approval"]["request_id"], "req-123")
        self.assertIn("signature", saved_state)

    @unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph nao esta instalado neste ambiente")
    def test_run_story_branch_association_replays_saved_state_and_executes_after_human_approval(self) -> None:
        adapter = FakeStoryBranchAssociationAdapter()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = f"{temp_dir}/preview-state.json"
            with patch(
                "kwikledgers_langgraph.story_branch_cli.build_story_branch_association_ports",
                return_value=StoryBranchAssociationPorts(branches=adapter),
            ):
                preview_result = run_story_branch_association(_build_arguments())
                _write_state_file(preview_result, state_file)

                resume_result = run_story_branch_association(
                    _build_arguments(
                        resume_state_file=state_file,
                        approval_request_id=preview_result["pending_approval"]["request_id"],
                    )
                )

        self.assertEqual(preview_result["branch_association_result"]["status"], "approval_required")
        self.assertEqual(len(adapter.preview_calls), 2)
        self.assertEqual(len(adapter.execute_calls), 1)
        self.assertEqual(resume_result["branch_association_result"]["status"], "associated")


if __name__ == "__main__":
    unittest.main()