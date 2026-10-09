from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import TechnicalDebtPreparationPorts
from kwikledgers_langgraph.lifecycle_state import LifecycleState
from kwikledgers_langgraph.lifecycle_adapters import lifecycle_scope
from kwikledgers_langgraph.process_policy import normalize_debt_title, prepare_technical_debt
from kwikledgers_langgraph.state import append_error, append_tracking_event


def build_technical_debt_preparation_graph(ports: TechnicalDebtPreparationPorts) -> Any:
    graph = StateGraph(LifecycleState)

    def resolve(state: LifecycleState) -> dict[str, Any]:
        try:
            request = ports.preparation.resolve(deepcopy(state.get("request", {})))
            request["parent"]["title"] = normalize_debt_title(
                request["repository_name"], request["parent"]["title"],
                allow_grave=request.get("allow_grave") is True,
            )
            run_id = str(uuid4())
            return {"context": request, "run_id": run_id, "results": {},
                    "observability": {"active_run_id": run_id, "correlation_id": run_id,
                                      "flow_name": "technical_debt_preparation", "source": "lifecycle_ports"},
                    "errors": [], "pending_approval": {}, "outcome": "running"}
        except Exception as error:
            return {**append_error(state, type(error).__name__), "outcome": "blocked", "results": {}}

    def similarity(state: LifecycleState) -> dict[str, Any]:
        try:
            request = state["context"]
            result = ports.preparation.find_similar(request["repository_name"],
                request["parent"]["title"], request["parent"]["context"])
            with lifecycle_scope(state):
                path = ports.preparation.save_repository_context(request["repository_name"], result,
                                                                 key=f'{state["run_id"]}:similarity')
            return {"results": {"similarity": result, "context_path": path}}
        except Exception as error:
            return {**append_error(state, type(error).__name__), "outcome": "blocked"}

    def prepare(state: LifecycleState) -> dict[str, Any]:
        try:
            request = state["context"]
            preview = prepare_technical_debt(request, state["results"]["similarity"], state["results"]["context_path"])
            if request.get("implementation_indicated") is True:
                with lifecycle_scope(state):
                    preview["preview_cache_path"] = ports.preparation.save_preview_cache(
                        request["repository_name"], deepcopy(preview), key=f'{state["run_id"]}:preview_cache',
                    )
                if not preview["preview_cache_path"]:
                    raise ValueError("Full preview cache required")
            event = {"action": "technical_debt.prepared", "run_id": state["run_id"],
                     "repository": request["repository_name"], "children_count": len(preview["children"])}
            with lifecycle_scope(state):
                ports.tracking.record(event, key=f'{state["run_id"]}:prepared')
            return {**append_tracking_event(state, category="lifecycle", message="technical_debt.prepared", payload=event),
                    "results": {**state["results"], "technical_debt_preview": preview}, "outcome": "preview_only"}
        except Exception as error:
            return {**append_error(state, type(error).__name__), "outcome": "blocked"}

    def route(state: LifecycleState) -> str:
        return END if state.get("outcome") == "blocked" else "next"

    graph.add_node("resolve", resolve)
    graph.add_node("similarity", similarity)
    graph.add_node("prepare", prepare)
    graph.add_edge(START, "resolve")
    graph.add_conditional_edges("resolve", route, {END: END, "next": "similarity"})
    graph.add_conditional_edges("similarity", route, {END: END, "next": "prepare"})
    graph.add_edge("prepare", END)
    return graph.compile(name="technical_debt_preparation")