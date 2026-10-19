from __future__ import annotations

import importlib.util
import unittest
from typing import Any

from kwikledgers_langgraph.adapters import StoryBranchAssociationPorts
from kwikledgers_langgraph.guardrails import build_approval_confirmation, build_bound_approval
from kwikledgers_langgraph.observability import build_observability_context

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None

if LANGGRAPH_AVAILABLE:
    from kwikledgers_langgraph.graphs.story_branch_association import build_story_branch_association_graph


class FakeStoryBranchAssociationAdapter:
    def __init__(self) -> None:
        self.preview_calls: list[dict[str, Any]] = []
        self.execute_calls: list[dict[str, Any]] = []

    def preview_story_branch_association(self, request: dict[str, Any]) -> dict[str, Any]:
        self.preview_calls.append(request)
        return {
            "story_id": request.get("story_id"),
            "repository_name": request.get("repository_name"),
            "work_items": [request.get("story_id")],
        }

    def associate_story_branch(self, request: dict[str, Any]) -> dict[str, Any]:
        self.execute_calls.append(request)
        return {
            "status": "associated",
            "story_id": request.get("story_id"),
            "repository_name": request.get("repository_name"),
        }


class FailingPreviewStoryBranchAssociationAdapter(FakeStoryBranchAssociationAdapter):
    def preview_story_branch_association(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("preview boom")


class FailingExecuteStoryBranchAssociationAdapter(FakeStoryBranchAssociationAdapter):
    def associate_story_branch(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("execute boom")


def _base_state() -> dict[str, Any]:
    return {
        "tracking_events": [],
        "observability": build_observability_context(
            flow_name="story_branch_association",
            source="test",
            backend_name="fastmcp",
        ),
        "branch_association_request": {
            "story_id": 120204,
            "repository_name": "accounting",
            "source_branch": "feature/KL-120204-balance-sheet-by-class",
            "related_work_item_ids": [120205],
            "include_child_work_items": True,
        },
    }


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph não está instalado neste ambiente")
class StoryBranchAssociationGraphTestCase(unittest.TestCase):
    def test_invalid_payload_stops_before_preview(self) -> None:
        adapter = FakeStoryBranchAssociationAdapter()
        runtime = build_story_branch_association_graph(StoryBranchAssociationPorts(branches=adapter))
        state = _base_state()
        state["branch_association_request"].pop("source_branch")

        result = runtime.invoke(state)

        self.assertIn("errors", result)
        self.assertEqual(adapter.preview_calls, [])
        self.assertEqual(adapter.execute_calls, [])
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_association")

    def test_preview_adapter_failure_stops_cleanly(self) -> None:
        adapter = FailingPreviewStoryBranchAssociationAdapter()
        runtime = build_story_branch_association_graph(StoryBranchAssociationPorts(branches=adapter))

        result = runtime.invoke(_base_state())

        self.assertIn("errors", result)
        self.assertEqual(result["branch_association_result"]["status"], "preview_failed")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_association")

if __name__ == "__main__":
    unittest.main()