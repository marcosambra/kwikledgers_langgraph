from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Callable
from uuid import uuid4

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.lifecycle_adapters import approval_binding, lifecycle_scope
from kwikledgers_langgraph.lifecycle_state import LifecycleState
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import append_error, append_tracking_event


Prepare = Callable[[LifecycleState], dict[str, Any]]
Execute = Callable[[LifecycleState, dict[str, Any], str], dict[str, Any]]


@dataclass(frozen=True)
class LifecycleStep:
    name: str
    prepare: Prepare
    execute: Execute | None = None
    post_execution: bool = False


def preview_digest(preview: dict[str, Any]) -> str:
    return sha256(json.dumps(preview, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _event(ports: LifecyclePorts, state: LifecycleState, action: str, phase: str) -> dict[str, Any]:
    timestamp = ports.clock.now()
    if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("Lifecycle clock must be timezone-aware")
    context = state.get("context", {})
    payload = {"run_id": state["run_id"], "action": action, "phase": phase,
               "timestamp": timestamp, "repository": context.get("repository"),
               "story_id": context.get("story_id"), "task_id": context.get("task_id")}
    if phase == "started":
        payload["started_at"] = timestamp
    with lifecycle_scope(state):
        ports.tracking.record(payload, key=f'{state["run_id"]}:{action}:{phase}')
    updates = append_tracking_event(
        state, category="lifecycle", message=f"{action}.{phase}", payload=payload,
    )
    updates.update(record_runtime_event(state, node=action, status=phase,
                                        message=f"{action}.{phase}", payload=payload))
    return updates


def _safe_detail(error: BaseException) -> str | None:
    return str(error) if getattr(error, "safe_detail", False) else None


def _failure(ports: LifecyclePorts, state: LifecycleState, action: str, *,
             reason: str = "pre_execution_failed", reconciliation: bool = False,
             detail: str | None = None) -> dict[str, Any]:
    outcome = "reconciliation_required" if reconciliation else "blocked"
    updates = append_error(state, f"{action}: {reason}")
    if detail:
        updates = {**updates, **append_error({**state, **updates}, f"{action}: {detail}")}
    updates.update({"outcome": outcome, "outcome_reason": reason, "pending_approval": {}})
    try:
        updates.update(_event(ports, state, action, outcome))
    except Exception:
        updates = {**updates, **append_error({**state, **updates}, f"{action}: tracking_unavailable")}
    return updates


def build_lifecycle_graph(
    ports: LifecyclePorts,
    steps: tuple[LifecycleStep, ...],
    *,
    name: str,
    checkpointer: Any,
    context_resolver: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    required_context: tuple[str, ...] = ("story_id", "task_id", "repository", "child_ids"),
) -> Any:
    if checkpointer is None:
        raise ValueError("Lifecycle approval requires a trusted checkpointer")
    graph = StateGraph(LifecycleState)

    def initialize(state: LifecycleState) -> dict[str, Any]:
        run_id = str(uuid4())
        clean: LifecycleState = {"run_id": run_id, "tracking_events": []}
        try:
            supplied = state.get("request", {})
            request = {field: deepcopy(supplied[field]) for field in ("story_id", "task_id", "repository_name") if field in supplied}
            context = (context_resolver(request) if context_resolver is not None else ports.context.resolve(request))
            if not isinstance(context, dict):
                raise ValueError("Authorized context must be a mapping")
            if name == "task_cycle":
                context = {**context, **{field: deepcopy(supplied[field])
                                        for field in ("work_kind", "fix_summary", "origin_task_id")
                                        if supplied.get(field) is not None}}
            if name == "task_hours":
                context = {**context, "completed_work_hours": deepcopy(supplied.get("completed_work_hours"))}
            preview_digest(context)
            for field in required_context:
                if not context.get(field):
                    raise ValueError(f"Missing authorized context: {field}")
            clean["context"] = deepcopy(context)
            clean["observability"] = {"active_run_id": run_id, "correlation_id": run_id,
                                      "flow_name": name, "source": "lifecycle_ports"}
            updates = _event(ports, clean, name, "started")
            return {**updates, "context": deepcopy(context), "run_id": run_id,
                    "results": {}, "errors": [], "outcome": "running",
                    "outcome_reason": "",
                    "pending_approval": {}, "approval_confirmation": {},
                    "direct_pr_url": ""}
        except Exception as error:
            return {**_failure(ports, clean, name, detail=_safe_detail(error)), "run_id": run_id,
                    "context": {}, "results": {}, "direct_pr_url": ""}

    def route(state: LifecycleState) -> str:
        return END if state.get("outcome") in ("blocked", "denied", "reconciliation_required",
                                          "not_reproduced", "retest_failed") else "next"

    def add_step(step: LifecycleStep, following: str) -> None:
        preview_node = f"{step.name}_preview"

        def prepare(state: LifecycleState) -> dict[str, Any]:
            try:
                with lifecycle_scope(state):
                    preview = deepcopy(step.prepare(deepcopy(state)))
                if not isinstance(preview, dict):
                    raise ValueError("Preview must be a mapping")
                pending = {"action": step.name, "preview": preview,
                           "run_id": state["run_id"], "request_id": str(uuid4()),
                           "approved": False, "preview_digest": preview_digest(preview),
                           "previewed_at": ports.clock.now()}
                updates = _event(ports, state, step.name, "previewed")
                if step.execute is None:
                    results = {**state.get("results", {}), step.name: preview}
                    updates = {**updates, "results": results, "pending_approval": {}}
                    terminal = preview.get("terminal_outcome")
                    if terminal in ("not_reproduced", "retest_failed"):
                        updates.update({"outcome": terminal,
                                        "outcome_reason": f"bug_{terminal}"})
                    return updates
                return {**updates, "pending_approval": pending}
            except GraphInterrupt:
                raise
            except Exception as error:
                persisted = any(previous.execute is not None and previous.name in state.get("results", {})
                                for previous in steps)
                return _failure(ports, state, step.name, reconciliation=step.post_execution and persisted,
                                detail=_safe_detail(error))

        graph.add_node(preview_node, prepare)
        if step.execute is None:
            graph.add_conditional_edges(preview_node, route, {END: END, "next": following})
            return
        gate_node = f"{step.name}_gate"
        execute_node = f"{step.name}_execute"

        def gate(state: LifecycleState) -> dict[str, Any]:
            pending = deepcopy(state["pending_approval"])
            confirmation = interrupt(deepcopy(pending))
            try:
                if ports.approvals.verify(approval_binding(pending), confirmation) is not True:
                    return {**_event(ports, state, step.name, "denied"),
                            "outcome": "denied", "outcome_reason": "approval_denied", "pending_approval": {}}
                pending["approved"] = True
                pending["confirmation"] = deepcopy(confirmation)
                return {**_event(ports, state, step.name, "approved"), "pending_approval": pending}
            except Exception:
                return _failure(ports, state, step.name, reason="approval_failed")

        def execute(state: LifecycleState) -> dict[str, Any]:
            working = state
            evidence: dict[str, Any] = {}
            attempted = False
            returned = False
            try:
                pending = deepcopy(state["pending_approval"])
                if (pending.get("approved") is not True or pending["action"] != step.name
                        or pending["run_id"] != state["run_id"]
                        or pending["preview_digest"] != preview_digest(pending["preview"])):
                    raise ValueError("Approval binding invalid")
                key = f'{state["run_id"]}:{step.name}'
                working = {**state, **_event(ports, state, step.name, "execution_started")}
                with lifecycle_scope(working, ports.approvals):
                    attempted = True
                    result = step.execute(deepcopy(working), deepcopy(pending["preview"]), key)
                returned = True
                if not isinstance(result, dict):
                    raise ValueError("Operation result must be a mapping")
                evidence = {"results": {**state.get("results", {}), step.name: result},
                            "pending_approval": {}}
                if step.name == "draft_pr":
                    evidence["direct_pr_url"] = result["url"]
                preview_digest(result)
                updates = _event(ports, {**working, **evidence}, step.name, "executed")
                return {**updates, **evidence}
            except Exception as error:
                journal_unknown = isinstance(error, RuntimeError) and str(error) in (
                    "Execution outcome unknown; reconcile before retry",
                    "Execution already reserved; reconcile before retry",
                )
                unknown = attempted and (journal_unknown or getattr(error, "execution_outcome_unknown", False)
                                         or not isinstance(error, (ValueError, PermissionError, RuntimeError)))
                reconciliation = returned or unknown
                reason = ("post_execution_evidence_failed" if returned else
                          "execution_outcome_unknown" if unknown else
                          "execution_rejected" if attempted else "pre_execution_failed")
                return {**evidence, **_failure(ports, {**working, **evidence}, step.name,
                                              reason=reason, reconciliation=reconciliation)}

        graph.add_node(gate_node, gate)
        graph.add_node(execute_node, execute)
        graph.add_conditional_edges(preview_node, route, {END: END, "next": gate_node})
        graph.add_conditional_edges(gate_node, route, {END: END, "next": execute_node})
        graph.add_conditional_edges(execute_node, route, {END: END, "next": following})

    def complete(state: LifecycleState) -> dict[str, Any]:
        try:
            return {**_event(ports, state, name, "completed"), "outcome": "completed", "outcome_reason": ""}
        except Exception:
            executed = any(step.execute is not None and step.name in state.get("results", {}) for step in steps)
            return _failure(ports, state, name, reason="completion_tracking_failed", reconciliation=executed)

    graph.add_node("initialize", initialize)
    graph.add_node("complete", complete)
    graph.add_edge(START, "initialize")
    first = f"{steps[0].name}_preview" if steps else "complete"
    graph.add_conditional_edges("initialize", route, {END: END, "next": first})
    for index, step in enumerate(steps):
        following = f"{steps[index + 1].name}_preview" if index + 1 < len(steps) else "complete"
        add_step(step, following)
    graph.add_edge("complete", END)
    return graph.compile(checkpointer=checkpointer, name=name)