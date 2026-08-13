from __future__ import annotations

import importlib.util
import unittest
from typing import Any

from kwikledgers_langgraph.adapters import SprintStoriesPorts
from kwikledgers_langgraph.observability import build_observability_context

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None

if LANGGRAPH_AVAILABLE:
    from kwikledgers_langgraph.graphs.sprint_stories import build_sprint_stories_graph


class FakeSprintStoriesProvider:
    def __init__(self, *, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def fetch_sprint_stories_detailed(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.result or {}


def _base_state() -> dict[str, Any]:
    return {
        "tracking_events": [],
        "observability": build_observability_context(
            flow_name="sprint_stories",
            source="test",
        ),
        "sprint_stories_request": {},
    }


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph não está instalado neste ambiente")
class SprintStoriesGraphTestCase(unittest.TestCase):
    def test_fetch_returns_detailed_payload(self) -> None:
        provider = FakeSprintStoriesProvider(
            result={
                "counts": {"total_stories": 3},
                "stories": [{"id": 1, "description": "..."}],
            }
        )
        runtime = build_sprint_stories_graph(SprintStoriesPorts(stories=provider))

        result = runtime.invoke(_base_state())

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result["sprint_stories_result"]["counts"]["total_stories"], 3)
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "fetch_sprint_stories")
        self.assertEqual(result["observability"]["timeline"][-1]["status"], "completed")

    def test_fetch_failure_records_error(self) -> None:
        provider = FakeSprintStoriesProvider(error=RuntimeError("boom"))
        runtime = build_sprint_stories_graph(SprintStoriesPorts(stories=provider))

        result = runtime.invoke(_base_state())

        self.assertIn("errors", result)
        self.assertEqual(result["sprint_stories_result"]["status"], "fetch_failed")
        self.assertEqual(result["observability"]["timeline"][-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
