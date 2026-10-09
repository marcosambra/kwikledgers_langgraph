from __future__ import annotations

from math import ceil
from typing import Any

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.graphs.lifecycle_engine import LifecycleStep, build_lifecycle_graph
from kwikledgers_langgraph.graphs.story_lifecycle import aware_time, required_ids, _require_ports
from kwikledgers_langgraph.lifecycle_state import LifecycleState


def build_task_cycle_graph(ports: LifecyclePorts, *, checkpointer: Any) -> Any:
    _require_ports(ports, "tasks", "work_items", "branches")

    def plan(state: LifecycleState) -> dict[str, Any]:
        context = state["context"]
        if ports.tracking.story_start_complete(context) is not True:
            raise ValueError("Durable completed story-start required before implementation")
        links = ports.branches.links(context)
        if not set(required_ids(context)) <= set(links.get("associated_ids", [])):
            raise ValueError("Story-start branch associations required before implementation")
        snapshot = ports.context.snapshot(context)
        if set(snapshot.get("child_ids", [])) != set(context["child_ids"]):
            raise ValueError("Story hierarchy changed before implementation")
        if snapshot.get("task_status") != "Active":
            raise ValueError("Task must be Active before implementation")
        aware_time(ports.tracking.started_at(context["repository"], context["task_id"]))
        preview = ports.tasks.plan(context)
        for field in ("plan_id", "files", "logic", "tests"):
            if not preview.get(field):
                raise ValueError(f"Incomplete task plan: {field}")
        if "risks" not in preview:
            raise ValueError("Task plan must disclose risks")
        return preview

    def implement(state: LifecycleState, preview: dict[str, Any], key: str) -> dict[str, Any]:
        if plan(state) != preview:
            raise ValueError("Authorized task plan/context changed after preview")
        result = ports.tasks.implement(preview["plan_id"], key=key)
        return {**result, "plan_id": preview["plan_id"]}

    def tests(state: LifecycleState) -> dict[str, Any]:
        plan_id = state["results"]["implement"]["plan_id"]
        result = ports.tasks.test(plan_id, key=f'{state["run_id"]}:test')
        if result.get("passed") is not True or not result.get("evidence"):
            raise ValueError("Tests failed or evidence missing; commit and close blocked")
        return {**result, "plan_id": plan_id}

    def commit_preview(state: LifecycleState) -> dict[str, Any]:
        return {"plan_id": state["results"]["test"]["plan_id"],
                "task_id": state["context"]["task_id"],
                "test_evidence": state["results"]["test"]["evidence"]}

    def commit(state: LifecycleState, preview: dict[str, Any], key: str) -> dict[str, Any]:
        result = ports.tasks.commit(preview["plan_id"], preview["task_id"], key=key)
        if not result.get("commit"):
            raise ValueError("Successful commit required before hours")
        return result

    def record_close(state: LifecycleState) -> dict[str, Any]:
        context = state["context"]
        start = ports.tracking.started_at(context["repository"], context["task_id"])
        closed_at = ports.clock.now()
        elapsed = (aware_time(closed_at) - aware_time(start)).total_seconds()
        if elapsed < 0:
            raise ValueError("Closing time precedes recorded start")
        result = {"task_id": context["task_id"], "start_time": start,
                  "close_time": closed_at, "completed_work_hours": ceil(elapsed / 3600)}
        ports.tracking.record({"action": "task_close_time", **result},
                              key=f'{state["run_id"]}:record_close')
        return result

    def hours_preview(state: LifecycleState) -> dict[str, Any]:
        snapshot = ports.context.snapshot(state["context"])
        return {**state["results"]["record_close"], "expected_revision": snapshot["revision"]}

    def hours(state: LifecycleState, preview: dict[str, Any], key: str) -> dict[str, Any]:
        if hours_preview(state) != preview:
            raise ValueError("Task changed after hours preview")
        return ports.work_items.hours(preview, key=key)

    def close_preview(state: LifecycleState) -> dict[str, Any]:
        snapshot = ports.context.snapshot(state["context"])
        return {**state["results"]["record_close"], "status": "Closed",
                "expected_revision": snapshot["revision"]}

    def close(state: LifecycleState, preview: dict[str, Any], key: str) -> dict[str, Any]:
        if close_preview(state) != preview:
            raise ValueError("Task changed after close preview")
        return ports.work_items.close(preview, key=key)

    return build_lifecycle_graph(ports, (
        LifecycleStep("implement", plan, implement),
        LifecycleStep("test", tests),
        LifecycleStep("commit", commit_preview, commit),
        LifecycleStep("record_close", record_close),
        LifecycleStep("hours", hours_preview, hours),
        LifecycleStep("close", close_preview, close),
    ), name="task_cycle", checkpointer=checkpointer)