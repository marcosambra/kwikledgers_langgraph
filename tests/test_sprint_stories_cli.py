from __future__ import annotations

import unittest
from unittest.mock import patch

from kwikledgers_langgraph.sprint_stories_cli import (
    _build_initial_state,
    main,
    run_sprint_stories,
)


class FakeRuntime:
    def __init__(self, *, result: object) -> None:
        self.result = result
        self.invocations: list[dict[str, object]] = []

    def invoke(self, state: dict[str, object]) -> object:
        self.invocations.append(state)
        return self.result


class SprintStoriesCliTestCase(unittest.TestCase):
    def test_build_initial_state_seeds_observability(self) -> None:
        state = _build_initial_state("test-source", "fastmcp")

        self.assertEqual(state["source"], "test-source")
        self.assertEqual(state["sprint_stories_request"], {})
        self.assertEqual(state["observability"]["flow_name"], "sprint_stories")
        self.assertEqual(state["observability"]["backend_name"], "fastmcp")

    def test_run_sprint_stories_invokes_runtime_with_built_state(self) -> None:
        runtime = FakeRuntime(result={"sprint_stories_result": {"counts": {"total_stories": 2}}})

        with patch("kwikledgers_langgraph.sprint_stories_cli.build_sprint_stories_runtime", return_value=runtime):
            result = run_sprint_stories(source="test-source")

        self.assertEqual(result["sprint_stories_result"]["counts"]["total_stories"], 2)
        self.assertEqual(len(runtime.invocations), 1)

    def test_run_sprint_stories_rejects_invalid_runtime_result(self) -> None:
        runtime = FakeRuntime(result="invalid")

        with patch("kwikledgers_langgraph.sprint_stories_cli.build_sprint_stories_runtime", return_value=runtime):
            with self.assertRaises(RuntimeError):
                run_sprint_stories(source="test-source")

    def test_main_prints_summary_and_state(self) -> None:
        result = {"sprint_stories_result": {"counts": {"total_stories": 5}}}

        with patch("kwikledgers_langgraph.sprint_stories_cli.run_sprint_stories", return_value=result):
            with patch("kwikledgers_langgraph.sprint_stories_cli._record_runtime_observability"):
                exit_code = main(["--print-state"])

        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
