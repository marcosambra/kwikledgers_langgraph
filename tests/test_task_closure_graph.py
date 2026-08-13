from __future__ import annotations

import importlib.util
import unittest
from typing import Any

from kwikledgers_langgraph.adapters import TaskClosurePorts
from kwikledgers_langgraph.guardrails import build_approval_confirmation, build_bound_approval
from kwikledgers_langgraph.observability import build_observability_context

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None

if LANGGRAPH_AVAILABLE:
    from kwikledgers_langgraph.graphs.task_closure import build_task_closure_graph


class FakeTaskClosureAdapter:
    def __init__(self) -> None:
        self.preview_calls: list[dict[str, Any]] = []
        self.execute_calls: list[dict[str, Any]] = []

    def preview_task_closure(self, request: dict[str, Any]) -> dict[str, Any]:
        self.preview_calls.append(request)
        start_time = request.get("start_time")
        close_time = request.get("close_time") or "2026-07-10T13:00:00+00:00"
        closure_allowed = close_time >= start_time
        return {
            "task_id": request.get("task_id"),
            "start_time": start_time,
            "close_time": close_time,
            "elapsed_seconds": 7200.0,
            "computed_hours": 2,
            "completed_work_hours": request.get("completed_work_hours") or 2.0,
            "remaining_work_hours": request.get("remaining_work_hours"),
            "target_status": "Closed",
            "comment": request.get("comment"),
            "closure_allowed": closure_allowed,
            "validation_messages": [] if closure_allowed else ["O horario de fechamento nao pode ser anterior ao horario de inicio."],
        }

    def close_task(self, request: dict[str, Any]) -> dict[str, Any]:
        self.execute_calls.append(request)
        preview = dict(request.get("preview", {}))
        return {
            "status": "closed",
            "task_id": request.get("task_id"),
            "completed_work_hours": preview.get("completed_work_hours"),
            "remaining_work_hours": preview.get("remaining_work_hours"),
            "target_status": "Closed",
            "changed": True,
        }


class FailingPreviewTaskClosureAdapter(FakeTaskClosureAdapter):
    def preview_task_closure(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("preview boom")


class FailingExecuteTaskClosureAdapter(FakeTaskClosureAdapter):
    def close_task(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("execute boom")


def _base_state() -> dict[str, Any]:
    return {
        "tracking_events": [],
        "observability": build_observability_context(
            flow_name="task_closure",
            source="test",
        ),
        "task_closure_request": {
            "task_id": 120310,
            "start_time": "2026-07-10T11:00:00+00:00",
            "close_time": "2026-07-10T13:00:00+00:00",
        },
    }


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph não está instalado neste ambiente")
class TaskClosureGraphTestCase(unittest.TestCase):
    def test_invalid_payload_stops_before_preview(self) -> None:
        adapter = FakeTaskClosureAdapter()
        runtime = build_task_closure_graph(TaskClosurePorts(closures=adapter))
        state = _base_state()
        state["task_closure_request"].pop("start_time")

        result = runtime.invoke(state)

        self.assertIn("errors", result)
        self.assertEqual(adapter.preview_calls, [])
        self.assertEqual(adapter.execute_calls, [])
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_closure")

    def test_invalid_close_time_stops_before_gate(self) -> None:
        adapter = FakeTaskClosureAdapter()
        runtime = build_task_closure_graph(TaskClosurePorts(closures=adapter))
        state = _base_state()
        state["task_closure_request"]["close_time"] = "2026-07-10T09:00:00+00:00"

        result = runtime.invoke(state)

        self.assertEqual(len(adapter.preview_calls), 1)
        self.assertEqual(adapter.execute_calls, [])
        self.assertEqual(result["task_closure_result"]["status"], "invalid_closure")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_closure")

    def test_preview_without_approval_stops_before_execute(self) -> None:
        adapter = FakeTaskClosureAdapter()
        runtime = build_task_closure_graph(TaskClosurePorts(closures=adapter))

        result = runtime.invoke(_base_state())

        self.assertEqual(len(adapter.preview_calls), 1)
        self.assertEqual(adapter.execute_calls, [])
        self.assertEqual(result["task_closure_result"]["status"], "approval_required")
        self.assertFalse(result["pending_approval"]["approved"])
        self.assertTrue(result["pending_approval"]["request_id"])
        self.assertEqual(result["task_closure_preview"]["computed_hours"], 2)
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_closure")

    def test_preview_adapter_failure_stops_cleanly(self) -> None:
        adapter = FailingPreviewTaskClosureAdapter()
        runtime = build_task_closure_graph(TaskClosurePorts(closures=adapter))

        result = runtime.invoke(_base_state())

        self.assertIn("errors", result)
        self.assertEqual(result["task_closure_result"]["status"], "preview_failed")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_closure")

    def test_replayed_preview_with_matching_confirmation_allows_execute(self) -> None:
        adapter = FakeTaskClosureAdapter()
        runtime = build_task_closure_graph(TaskClosurePorts(closures=adapter))
        state = _base_state()
        preview = adapter.preview_task_closure(state["task_closure_request"])
        state["task_closure_preview"] = preview
        state["pending_approval"] = build_bound_approval(
            state,
            action="task_closure.execute",
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
        self.assertEqual(result["task_closure_result"]["status"], "closed")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "execute_task_closure")

    def test_local_simulation_flag_allows_execute_after_preview(self) -> None:
        adapter = FakeTaskClosureAdapter()
        runtime = build_task_closure_graph(TaskClosurePorts(closures=adapter))
        state = _base_state()
        state["approval_simulation_requested"] = True

        result = runtime.invoke(state)

        self.assertEqual(len(adapter.preview_calls), 1)
        self.assertEqual(len(adapter.execute_calls), 1)
        self.assertTrue(result["pending_approval"]["approved"])
        self.assertEqual(result["pending_approval"]["approval_source"], "local_cli_simulation")

    def test_execute_adapter_failure_sets_failed_result(self) -> None:
        adapter = FailingExecuteTaskClosureAdapter()
        runtime = build_task_closure_graph(TaskClosurePorts(closures=adapter))
        state = _base_state()
        preview = adapter.preview_task_closure(state["task_closure_request"])
        state["task_closure_preview"] = preview
        state["pending_approval"] = build_bound_approval(
            state,
            action="task_closure.execute",
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
        self.assertEqual(result["task_closure_result"]["status"], "execute_failed")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "execute_task_closure")
        self.assertEqual(result["observability"]["timeline"][-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
