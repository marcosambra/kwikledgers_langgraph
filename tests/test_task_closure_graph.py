from __future__ import annotations

import importlib.util
import unittest
from typing import Any

from kwikledgers_langgraph.adapters import TaskClosurePorts
from kwikledgers_langgraph.guardrails import build_approval_confirmation, build_bound_approval
from kwikledgers_langgraph.observability import build_observability_context
from kwikledgers_langgraph.task_closure_policy import build_task_closure_preview

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None

if LANGGRAPH_AVAILABLE:
    from kwikledgers_langgraph.graphs.task_closure import build_task_closure_graph


class FakeTaskClosureAdapter:
    def __init__(self) -> None:
        self.execute_calls: list[dict[str, Any]] = []

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


class FailingExecuteTaskClosureAdapter(FakeTaskClosureAdapter):
    def close_task(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("execute boom")


def _base_state() -> dict[str, Any]:
    return {
        "tracking_events": [],
        "observability": build_observability_context(
            flow_name="task_closure",
            source="test",
            backend_name="fastmcp",
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
        self.assertEqual(adapter.execute_calls, [])
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "stop_without_closure")

if __name__ == "__main__":
    unittest.main()
