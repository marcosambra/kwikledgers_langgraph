from __future__ import annotations

import importlib.util
import unittest
from typing import Any

from kwikledgers_langgraph.adapters import StoryStatusTransitionPorts
from kwikledgers_langgraph.guardrails import build_approval_confirmation, build_bound_approval
from kwikledgers_langgraph.observability import build_observability_context

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None

if LANGGRAPH_AVAILABLE:
    from kwikledgers_langgraph.graphs.story_status_transition import build_story_status_transition_graph


class FakeStoryStatusTransitionAdapter:
    def __init__(self) -> None:
        self.preview_calls: list[dict[str, Any]] = []
        self.execute_calls: list[dict[str, Any]] = []

    def preview_story_status_transition(self, request: dict[str, Any]) -> dict[str, Any]:
        self.preview_calls.append(request)
        current_status = request.get("expected_current_status") or "Desenvolvimento"
        target_status = request.get("target_status")
        allowed = current_status == "Desenvolvimento" and target_status == "Aguardando PR"
        return {
            "story_id": request.get("story_id"),
            "title": "KL-120204 Story",
            "work_item_type": "User Story",
            "url": None,
            "current_status": current_status,
            "target_status": target_status,
            "expected_current_status": request.get("expected_current_status"),
            "transition_allowed": allowed,
            "validation_messages": [] if allowed else ["A transicao solicitada nao e a proxima etapa permitida no fluxo canonico."],
            "canonical_flow": [
                "New",
                "Refinamento",
                "Backlog Sprint",
                "Desenvolvimento",
                "Aguardando PR",
                "Homologação",
                "QA",
            ],
            "patch_preview": {"System.State": target_status},
            "story_revision": "rev-1",
        }

    def update_story_status(self, request: dict[str, Any]) -> dict[str, Any]:
        self.execute_calls.append(request)
        preview = dict(request.get("preview", {}))
        return {
            "status": "updated",
            "story_id": request.get("story_id"),
            "previous_status": preview.get("current_status"),
            "current_status": request.get("target_status"),
            "changed": True,
        }


class FailingPreviewStoryStatusTransitionAdapter(FakeStoryStatusTransitionAdapter):
    def preview_story_status_transition(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("preview boom")


class FailingExecuteStoryStatusTransitionAdapter(FakeStoryStatusTransitionAdapter):
    def update_story_status(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("execute boom")


def _base_state() -> dict[str, Any]:
    return {
        "tracking_events": [],
        "observability": build_observability_context(
            flow_name="story_status_transition",
            source="test",
            backend_name="legacy",
        ),
        "story_status_request": {
            "story_id": 120204,
            "target_status": "Aguardando PR",
            "expected_current_status": "Desenvolvimento",
        },
    }


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph não está instalado neste ambiente")
class StoryStatusTransitionGraphTestCase(unittest.TestCase):
    def test_invalid_payload_stops_before_preview(self) -> None:
        adapter = FakeStoryStatusTransitionAdapter()
        runtime = build_story_status_transition_graph(StoryStatusTransitionPorts(statuses=adapter))
        state = _base_state()
        state["story_status_request"].pop("target_status")

        result = runtime.invoke(state)

        self.assertIn("errors", result)
        self.assertEqual(adapter.preview_calls, [])
        self.assertEqual(adapter.execute_calls, [])
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_transition")

    def test_invalid_transition_stops_before_gate(self) -> None:
        adapter = FakeStoryStatusTransitionAdapter()
        runtime = build_story_status_transition_graph(StoryStatusTransitionPorts(statuses=adapter))
        state = _base_state()
        state["story_status_request"]["target_status"] = "QA"

        result = runtime.invoke(state)

        self.assertEqual(len(adapter.preview_calls), 1)
        self.assertEqual(adapter.execute_calls, [])
        self.assertEqual(result["story_status_result"]["status"], "invalid_transition")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_transition")

    def test_preview_without_approval_stops_before_execute(self) -> None:
        adapter = FakeStoryStatusTransitionAdapter()
        runtime = build_story_status_transition_graph(StoryStatusTransitionPorts(statuses=adapter))

        result = runtime.invoke(_base_state())

        self.assertEqual(len(adapter.preview_calls), 1)
        self.assertEqual(adapter.execute_calls, [])
        self.assertEqual(result["story_status_result"]["status"], "approval_required")
        self.assertFalse(result["pending_approval"]["approved"])
        self.assertTrue(result["pending_approval"]["request_id"])
        self.assertEqual(result["story_status_preview"]["target_status"], "Aguardando PR")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_transition")

    def test_preview_adapter_failure_stops_cleanly(self) -> None:
        adapter = FailingPreviewStoryStatusTransitionAdapter()
        runtime = build_story_status_transition_graph(StoryStatusTransitionPorts(statuses=adapter))

        result = runtime.invoke(_base_state())

        self.assertIn("errors", result)
        self.assertEqual(result["story_status_result"]["status"], "preview_failed")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_transition")

    def test_replayed_preview_with_matching_confirmation_allows_execute(self) -> None:
        adapter = FakeStoryStatusTransitionAdapter()
        runtime = build_story_status_transition_graph(StoryStatusTransitionPorts(statuses=adapter))
        state = _base_state()
        preview = adapter.preview_story_status_transition(state["story_status_request"])
        state["story_status_preview"] = preview
        state["pending_approval"] = build_bound_approval(
            state,
            action="story_status_transition.execute",
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

        self.assertEqual(len(adapter.preview_calls), 2)
        self.assertEqual(len(adapter.execute_calls), 1)
        self.assertEqual(result["story_status_result"]["status"], "updated")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "execute_status_transition")

    def test_preseeded_initial_confirmation_without_preview_replay_still_stops_before_execute(self) -> None:
        adapter = FakeStoryStatusTransitionAdapter()
        runtime = build_story_status_transition_graph(StoryStatusTransitionPorts(statuses=adapter))
        state = _base_state()
        preview = adapter.preview_story_status_transition(state["story_status_request"])
        state["pending_approval"] = build_bound_approval(
            state,
            action="story_status_transition.execute",
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

        self.assertEqual(len(adapter.preview_calls), 2)
        self.assertEqual(adapter.execute_calls, [])
        self.assertFalse(result["pending_approval"]["approved"])
        self.assertEqual(result["story_status_result"]["status"], "approval_required")

    def test_local_simulation_flag_allows_execute_after_preview(self) -> None:
        adapter = FakeStoryStatusTransitionAdapter()
        runtime = build_story_status_transition_graph(StoryStatusTransitionPorts(statuses=adapter))
        state = _base_state()
        state["approval_simulation_requested"] = True

        result = runtime.invoke(state)

        self.assertEqual(len(adapter.preview_calls), 1)
        self.assertEqual(len(adapter.execute_calls), 1)
        self.assertTrue(result["pending_approval"]["approved"])
        self.assertEqual(result["pending_approval"]["approval_source"], "local_cli_simulation")

    def test_execute_adapter_failure_sets_failed_result(self) -> None:
        adapter = FailingExecuteStoryStatusTransitionAdapter()
        runtime = build_story_status_transition_graph(StoryStatusTransitionPorts(statuses=adapter))
        state = _base_state()
        preview = adapter.preview_story_status_transition(state["story_status_request"])
        state["story_status_preview"] = preview
        state["pending_approval"] = build_bound_approval(
            state,
            action="story_status_transition.execute",
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
        self.assertEqual(result["story_status_result"]["status"], "execute_failed")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "execute_status_transition")
        self.assertEqual(result["observability"]["timeline"][-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()