from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import DayEndClosurePorts
from kwikledgers_langgraph.day_end_closure_policy import build_day_end_preview
from kwikledgers_langgraph.graphs.mutable_flow import (
    route_after_errors,
    run_preview_step,
    stop_without_execution,
    validate_required_request,
)
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_error


def build_day_end_closure_graph(ports: DayEndClosurePorts) -> Any:
    graph = StateGraph(AgentState)

    def validate_request(state: AgentState) -> dict[str, Any]:
        return validate_required_request(
            state,
            request_key="day_end_closure_request",
            required_fields=("trigger",),
            node="validate_request",
            started_message="Validando payload do fechamento de fim de dia.",
            failed_message="Payload do fechamento de fim de dia invalido.",
            completed_message="Payload do fechamento de fim de dia validado.",
            missing_error_prefix="Payload de fechamento de fim de dia incompleto: ",
        )

    def collect_context(state: AgentState) -> dict[str, Any]:
        started = record_runtime_event(
            state, node="collect_context", status="started",
            message="Coletando tasks ativas e atividade do dia.",
        )
        try:
            context = ports.day_end.collect_day_context(dict(state.get("day_end_closure_request", {})))
        except Exception as error:
            failed = record_runtime_event(
                state, node="collect_context", status="failed",
                message="Falha ao coletar o contexto de fim de dia.", payload={"error": str(error)},
            )
            return {**started, **append_error(state, f"Falha ao coletar o contexto de fim de dia: {error}"), **failed}
        completed = record_runtime_event(
            state, node="collect_context", status="completed",
            message="Contexto de fim de dia coletado.",
            payload={"active_tasks": len(context.get("active_tasks", []))},
        )
        return {**started, **completed, "day_end_closure_context": context}

    def preview_day_end_closure(state: AgentState) -> dict[str, Any]:
        context = dict(state.get("day_end_closure_context", {}))
        return run_preview_step(
            state,
            request_key="day_end_closure_request",
            preview_key="day_end_closure_preview",
            result_key="day_end_closure_result",
            action="day_end_closure.preview",
            node="preview_day_end_closure",
            started_message="Calculando o preview do fechamento de fim de dia.",
            failed_message="Falha ao calcular o preview do fechamento de fim de dia.",
            completed_message="Preview do fechamento de fim de dia calculado.",
            tracking_category="day_end_closure_preview",
            tracking_message="Preview de fechamento de fim de dia gerado pelo runtime LangGraph.",
            preview_operation=lambda request: build_day_end_preview(request, context),
            tracking_payload_builder=lambda _request, preview: {
                "status": preview.get("status"),
                "skip_reason": preview.get("skip_reason"),
                "computed_hours": preview.get("computed_hours"),
            },
        )

    def stop_without_closure(state: AgentState) -> dict[str, Any]:
        updates = stop_without_execution(
            state, node="stop_without_closure",
            message="Fechamento de fim de dia encerrado sem escrita no Azure.",
        )
        if state.get("errors"):
            return updates
        preview = dict(state.get("day_end_closure_preview", {}))
        status = "dry_run" if preview.get("status") == "ready" else "skipped"
        if preview.get("status") == "ready" and not preview.get("dry_run"):
            status = "preview_ready"
        return {
            **updates,
            "day_end_closure_result": {
                "status": status,
                "reason": "human_approval_required" if status == "preview_ready" else preview.get("skip_reason"),
                "changed": False,
            },
        }

    def route_after_validation(state: AgentState) -> str:
        return route_after_errors(state, next_node="collect_context", stop_node="stop_without_closure")

    def route_after_collect(state: AgentState) -> str:
        return route_after_errors(state, next_node="preview_day_end_closure", stop_node="stop_without_closure")

    graph.add_node("validate_request", validate_request)
    graph.add_node("collect_context", collect_context)
    graph.add_node("preview_day_end_closure", preview_day_end_closure)
    graph.add_node("stop_without_closure", stop_without_closure)

    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges(
        "validate_request", route_after_validation,
        {"collect_context": "collect_context", "stop_without_closure": "stop_without_closure"},
    )
    graph.add_conditional_edges(
        "collect_context", route_after_collect,
        {"preview_day_end_closure": "preview_day_end_closure", "stop_without_closure": "stop_without_closure"},
    )
    graph.add_edge("preview_day_end_closure", "stop_without_closure")
    graph.add_edge("stop_without_closure", END)

    return graph.compile()
