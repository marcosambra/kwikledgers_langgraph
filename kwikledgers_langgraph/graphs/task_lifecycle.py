from __future__ import annotations

import math
from typing import Any

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.graphs.lifecycle_engine import LifecycleStep, build_lifecycle_graph
from kwikledgers_langgraph.graphs.story_lifecycle import required_ids, _require_ports
from kwikledgers_langgraph.lifecycle_state import LifecycleState
from kwikledgers_langgraph.task_closure_policy import build_verified_task_closure_preview


def _is_bug(context: dict[str, Any]) -> bool:
    return (context.get("story_details") or {}).get("work_item_type") == "Bug"


def _nonempty(value: Any) -> bool:
    return bool(value) and not (isinstance(value, str) and not value.strip())


def build_task_cycle_graph(ports: LifecyclePorts, *, checkpointer: Any) -> Any:
    _require_ports(ports, "tasks", "work_items", "branches")

    def reproduce(state: LifecycleState) -> dict[str, Any]:
        context = state["context"]
        if not _is_bug(context):
            return {"applicable": False}
        if ports.reproduction is None:
            raise ValueError("Bug stories require the official browser reproduction capability")
        if ports.tracking.story_start_complete(context) is not True:
            raise ValueError("Durable completed story-start required before reproduction")
        saved = ports.reproduction.saved_error_path(context)
        if saved:
            return {"reproduced": True, "resumed": True, "evidence": "resumed after failed retest",
                    "error_path": saved}
        result = ports.reproduction.reproduce(context, key=f'{state["run_id"]}:reproduce')
        if type(result.get("reproduced")) is not bool or not _nonempty(result.get("evidence")):
            raise ValueError("Reproduction requires a boolean outcome and evidence")
        if not result["reproduced"]:
            return {"reproduced": False, "evidence": result["evidence"],
                    "terminal_outcome": "not_reproduced"}
        if not isinstance(result.get("error_path"), list) or not result["error_path"]:
            raise ValueError("Reproduced bug requires a mapped error path")
        ports.reproduction.save_error_path(context, result["error_path"],
                                           key=f'{state["run_id"]}:save_error_path')
        return {"reproduced": True, "evidence": result["evidence"], "error_path": result["error_path"]}

    def retest(state: LifecycleState) -> dict[str, Any]:
        if not _is_bug(state["context"]):
            return {"applicable": False}
        path = state["results"]["reproduce"]["error_path"]
        result = ports.reproduction.retest(state["context"], path, key=f'{state["run_id"]}:retest')
        if type(result.get("passed")) is not bool or not _nonempty(result.get("evidence")):
            raise ValueError("Browser retest requires a boolean outcome and evidence")
        if not result["passed"]:
            return {"passed": False, "evidence": result["evidence"], "error_path": path,
                    "terminal_outcome": "retest_failed"}
        ports.reproduction.clear_error_path(state["context"], key=f'{state["run_id"]}:clear_error_path')
        return {"passed": True, "evidence": result["evidence"], "error_path": path}

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
        start = ports.tracking.started_at(context["repository"], context["task_id"])
        closure = build_verified_task_closure_preview(
            {**context, "start_time": start, "close_time": ports.clock.now()})
        if not closure["closure_allowed"]:
            raise ValueError("; ".join(closure["validation_messages"]))
        preview = ports.tasks.plan(context)
        for field in ("plan_id", "files", "logic", "tests"):
            if not preview.get(field):
                raise ValueError(f"Incomplete task plan: {field}")
        if "risks" not in preview:
            raise ValueError("Task plan must disclose risks")
        return preview

    def implement(state: LifecycleState) -> dict[str, Any]:
        preview = state["results"]["plan"]
        if plan(state) != preview:
            raise ValueError("Authorized task plan/context changed after preview")
        result = ports.tasks.implement(preview["plan_id"], key=f'{state["run_id"]}:implement')
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

    def commit(state: LifecycleState) -> dict[str, Any]:
        preview = commit_preview(state)
        if plan(state) != state["results"]["plan"]:
            raise ValueError("Authorized task plan/context changed before commit")
        result = ports.tasks.commit(preview["plan_id"], preview["task_id"], key=f'{state["run_id"]}:commit')
        if not result.get("commit"):
            raise ValueError("Successful commit required before hours")
        return result

    def record_close(state: LifecycleState) -> dict[str, Any]:
        context = state["context"]
        start = ports.tracking.started_at(context["repository"], context["task_id"])
        closed_at = ports.clock.now()
        closure = build_verified_task_closure_preview(
            {**context, "start_time": start, "close_time": closed_at})
        if not closure["closure_allowed"]:
            raise ValueError("; ".join(closure["validation_messages"]))
        result = {"task_id": context["task_id"], "start_time": start,
                  "close_time": closed_at, "completed_work_hours": closure["completed_work_hours"],
                  "work_kind": closure["work_kind"], "fix_summary": closure["fix_summary"],
                  "origin_task_id": closure["origin_task_id"]}
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
        LifecycleStep("reproduce", reproduce),
        LifecycleStep("plan", plan),
        LifecycleStep("implement", implement),
        LifecycleStep("test", tests),
        LifecycleStep("retest", retest),
        LifecycleStep("commit", commit),
        LifecycleStep("record_close", record_close),
        LifecycleStep("hours", hours_preview, hours),
        LifecycleStep("close", close_preview, close),
    ), name="task_cycle", checkpointer=checkpointer)


def build_task_hours_graph(ports: LifecyclePorts, *, checkpointer: Any) -> Any:
    _require_ports(ports, "work_items")

    def hours_preview(state: LifecycleState) -> dict[str, Any]:
        value = state["request"].get("completed_work_hours")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("completed_work_hours must be a positive finite number")
        snapshot = ports.context.snapshot(state["context"])
        revision = snapshot.get("revision")
        if type(revision) is not int or revision <= 0:
            raise ValueError("Task snapshot requires a positive native revision")
        return {
            "task_id": state["context"]["task_id"],
            "completed_work_hours": float(value),
            "expected_revision": revision,
            "hours_only": True,
        }

    def hours(state: LifecycleState, preview: dict[str, Any], key: str) -> dict[str, Any]:
        if hours_preview(state) != preview:
            raise ValueError("Task changed after hours preview")
        return ports.work_items.hours(preview, key=key)

    return build_lifecycle_graph(
        ports,
        (LifecycleStep("hours", hours_preview, hours),),
        name="task_hours",
        checkpointer=checkpointer,
    )