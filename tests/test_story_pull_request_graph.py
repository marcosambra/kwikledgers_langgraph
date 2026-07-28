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

    def test_preview_without_approval_stops_before_create(self) -> None:
        adapter = FakeStoryPullRequestAdapter()
        runtime = build_story_pull_request_graph(
            StoryPullRequestPorts(pull_requests=adapter)
        )

        result = runtime.invoke(_base_state())

        self.assertEqual(len(adapter.preview_calls), 1)
        self.assertEqual(adapter.create_calls, [])
        self.assertEqual(result["pull_request_result"]["status"], "approval_required")
        self.assertFalse(result["pending_approval"]["approved"])
        self.assertTrue(result["pending_approval"]["request_id"])
        self.assertEqual(result["pull_request_preview"]["title"], "KL-120204 Draft PR")
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

    def test_replayed_preview_with_matching_confirmation_allows_create(self) -> None:
        adapter = FakeStoryPullRequestAdapter()
        runtime = build_story_pull_request_graph(
            StoryPullRequestPorts(pull_requests=adapter)
        )
        state = _base_state()
        preview = {
            "title": "KL-120204 Draft PR",
            "story_id": state["pull_request_request"]["story_id"],
        }
        state["pull_request_preview"] = preview
        state["pending_approval"] = build_bound_approval(
            state,
            action="story_pull_request.create",
            preview=preview,
            approved=False,
            approval_source="pending_review",
        )
        state["approval_confirmation"] = build_approval_confirmation(
            request_id=state["pending_approval"]["request_id"],
            approved=True,
            approval_source="test",
        )

        result = runtime.invoke(state)

        self.assertEqual(len(adapter.preview_calls), 1)
        self.assertEqual(len(adapter.create_calls), 1)
        self.assertEqual(result["pull_request_result"]["status"], "created")
        self.assertEqual(result["pull_request_result"]["title"], "KL-120204 Draft PR")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "create_pull_request")

    def test_preseeded_initial_confirmation_without_preview_replay_still_stops_before_create(self) -> None:
        adapter = FakeStoryPullRequestAdapter()
        runtime = build_story_pull_request_graph(
            StoryPullRequestPorts(pull_requests=adapter)
        )
        state = _base_state()
        preview = {
            "title": "KL-120204 Draft PR",
            "story_id": state["pull_request_request"]["story_id"],
        }
        state["pending_approval"] = build_bound_approval(
            state,
            action="story_pull_request.create",
            preview=preview,
            approved=False,
            approval_source="pending_review",
        )
        state["approval_confirmation"] = build_approval_confirmation(
            request_id=state["pending_approval"]["request_id"],
            approved=True,
            approval_source="test",
        )

        result = runtime.invoke(state)

        self.assertEqual(len(adapter.preview_calls), 1)
        self.assertEqual(adapter.create_calls, [])
        self.assertFalse(result["pending_approval"]["approved"])
        self.assertEqual(result["pull_request_result"]["status"], "approval_required")

    def test_local_simulation_flag_allows_create_after_preview(self) -> None:
        adapter = FakeStoryPullRequestAdapter()
        runtime = build_story_pull_request_graph(
            StoryPullRequestPorts(pull_requests=adapter)
        )
        state = _base_state()
        state["approval_simulation_requested"] = True

        result = runtime.invoke(state)

        self.assertEqual(len(adapter.preview_calls), 1)
        self.assertEqual(len(adapter.create_calls), 1)
        self.assertTrue(result["pending_approval"]["approved"])
        self.assertEqual(result["pending_approval"]["approval_source"], "local_cli_simulation")

    def test_execute_adapter_failure_sets_failed_result(self) -> None:
        adapter = FailingCreateStoryPullRequestAdapter()
        runtime = build_story_pull_request_graph(
            StoryPullRequestPorts(pull_requests=adapter)
        )
        state = _base_state()
        preview = {
            "title": "KL-120204 Draft PR",
            "story_id": state["pull_request_request"]["story_id"],
        }
        state["pull_request_preview"] = preview
        state["pending_approval"] = build_bound_approval(
            state,
            action="story_pull_request.create",
            preview=preview,
            approved=False,
            approval_source="pending_review",
        )
        state["approval_confirmation"] = build_approval_confirmation(
            request_id=state["pending_approval"]["request_id"],
            approved=True,
            approval_source="test",
        )

        result = runtime.invoke(state)

        self.assertIn("errors", result)
        self.assertEqual(result["pull_request_result"]["status"], "execute_failed")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "create_pull_request")
        self.assertEqual(result["observability"]["timeline"][-1]["status"], "failed")
