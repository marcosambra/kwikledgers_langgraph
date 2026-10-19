from __future__ import annotations

import importlib.util
import unittest
from typing import Any

from kwikledgers_langgraph.adapters import StoryPullRequestPorts
from kwikledgers_langgraph.guardrails import build_approval_confirmation, build_bound_approval
from kwikledgers_langgraph.observability import build_observability_context

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None

if LANGGRAPH_AVAILABLE:
    from kwikledgers_langgraph.graphs.story_pull_request import build_story_pull_request_graph


class FakeStoryPullRequestAdapter:
    def __init__(self) -> None:
        self.preview_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    def preview_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        self.preview_calls.append(request)
        return {
            "title": "KL-120204 Draft PR",
            "story_id": request.get("story_id"),
        }

    def create_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        self.create_calls.append(request)
        return {
            "status": "created",
            "title": "KL-120204 Draft PR",
            "story_id": request.get("story_id"),
        }


class FailingPreviewStoryPullRequestAdapter(FakeStoryPullRequestAdapter):
    def preview_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("preview boom")


class FailingCreateStoryPullRequestAdapter(FakeStoryPullRequestAdapter):
    def create_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("create boom")


def _base_state() -> dict[str, Any]:
    return {
        "tracking_events": [],
        "observability": build_observability_context(
            flow_name="story_pull_request",
            source="test",
            backend_name="fastmcp",
        ),
        "pull_request_request": {
            "story_id": 120204,
            "repository_name": "accounting",
            "source_branch": "feature/KL-120204-balance-sheet-by-class",
            "what_was_changed": "Resumo formal do ajuste.",
            "affected_processes": "Fluxo de PR da story.",
            "expected_impacts": "Maior previsibilidade do runtime.",
            "important_points": "Validar preview e aprovação.",
            "mermaid_diagram": "graph TD; A-->B;",
        },
    }


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph não está instalado neste ambiente")
class StoryPullRequestGraphTestCase(unittest.TestCase):
    def test_invalid_payload_stops_before_preview(self) -> None:
        adapter = FakeStoryPullRequestAdapter()
        runtime = build_story_pull_request_graph(
            StoryPullRequestPorts(pull_requests=adapter)
        )
        state = _base_state()
        state["pull_request_request"].pop("mermaid_diagram")

        result = runtime.invoke(state)

        self.assertIn("errors", result)
        self.assertEqual(adapter.preview_calls, [])
        self.assertEqual(adapter.create_calls, [])
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_create")

    def test_preview_adapter_failure_stops_cleanly(self) -> None:
        adapter = FailingPreviewStoryPullRequestAdapter()
        runtime = build_story_pull_request_graph(
            StoryPullRequestPorts(pull_requests=adapter)
        )

        result = runtime.invoke(_base_state())

        self.assertIn("errors", result)
        self.assertEqual(result["pull_request_result"]["status"], "preview_failed")
        self.assertEqual(adapter.create_calls, [])
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_create")
