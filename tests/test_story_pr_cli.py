from __future__ import annotations

import argparse
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.story_pr_cli import (
    _build_initial_state,
    _build_request,
    run_story_pull_request,
)


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


class StoryPullRequestCliTestCase(unittest.TestCase):
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
        self.assertEqual(state["observability"]["timeline"][0]["status"], "initialized")

    def test_build_initial_state_allows_explicit_local_approval_simulation(self) -> None:
        arguments = _build_arguments(approve_create=True)

        state = _build_initial_state(arguments)

        self.assertTrue(state["approval_simulation_requested"])
        self.assertNotIn("pending_approval", state)

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


if __name__ == "__main__":
    unittest.main()