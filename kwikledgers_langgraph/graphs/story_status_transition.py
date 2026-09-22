from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import StoryStatusTransitionPorts
from kwikledgers_langgraph.graphs.mutable_flow import (
    route_after_errors,
    run_execute_step,
    run_mutation_gate,
    run_preview_step,
    stop_without_execution,
    validate_required_request,
)
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_error, append_tracking_event
from kwikledgers_langgraph.story_status_policy import build_story_status_transition_preview


def _required_request_fields() -> tuple[str, ...]:
    return ("story_id", "target_status")


def build_story_status_transition_graph(ports: StoryStatusTransitionPorts) -> Any:
    graph = StateGraph(AgentState)

    def validate_request(state: AgentState) -> dict[str, Any]:
        return validate_required_request(
            state,
            request_key="story_status_request",
            required_fields=_required_request_fields(),
            node="validate_request",
            started_message="Validando payload do fluxo de transicao de status da story.",
            failed_message="Payload do fluxo de transicao de status invalido.",
            completed_message="Payload do fluxo de transicao de status validado.",
            missing_error_prefix="Payload de transicao de status incompleto: ",
        )

    def preview_status_transition(state: AgentState) -> dict[str, Any]:
        def preview_validator(state: AgentState, _request: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any] | None:
            if preview.get("transition_allowed"):
                return None
            error_updates = append_error(
                state,
                "Transicao de status bloqueada: " + "; ".join(preview.get("validation_messages", [])),
            )
            failed = record_runtime_event(
                state,
                node="preview_status_transition",
                status="failed",
                message="Preview da transicao de status identificou uma transicao invalida.",
                payload={"validation_messages": preview.get("validation_messages", [])},
            )
            return {
                **error_updates,
                **failed,
                "story_status_result": {"status": "invalid_transition", "preview_available": True},
            }

        return run_preview_step(
            state,
            request_key="story_status_request",
            preview_key="story_status_preview",
            result_key="story_status_result",
            action="story_status_transition.preview",
            node="preview_status_transition",
            started_message="Gerando preview da transicao de status da story.",
            failed_message="Falha ao gerar o preview da transicao de status da story.",
            completed_message="Preview da transicao de status gerado com sucesso.",
            tracking_category="story_status_preview",
            tracking_message="Preview da transicao de status da story gerado pelo runtime LangGraph.",
            preview_operation=lambda request: build_story_status_transition_preview(
                request,
                ports.statuses.preview_story_status_transition(request),
            ),
            tracking_payload_builder=lambda request, preview: {
                "story_id": request.get("story_id"),
                "current_status": preview.get("current_status"),
                "target_status": preview.get("target_status"),
            },
            completed_payload_builder=lambda request, _preview: {"story_id": request.get("story_id")},
            preview_validator=preview_validator,
        )

    def gate_status_transition(state: AgentState) -> dict[str, Any]:
        return run_mutation_gate(
            state,
            preview_key="story_status_preview",
            result_key="story_status_result",
            action="story_status_transition.execute",
            node="gate_status_transition",
            approval_required_message="Fluxo aguardando aprovação explícita para transicionar o status da story.",
            approved_message="Aprovação explícita detectada para transicionar o status da story.",
        )

    def execute_status_transition(state: AgentState) -> dict[str, Any]:
        return run_execute_step(
            state,
            request_key="story_status_request",
            result_key="story_status_result",
            node="execute_status_transition",
            started_message="Atualizando o status da story após aprovação explícita.",
            failed_message="Falha ao atualizar o status da story.",
            completed_message="Transicao de status da story concluida com sucesso.",
            tracking_category="story_status_transition",
            tracking_message="Transicao de status da story executada pelo runtime LangGraph.",
            execute_operation=ports.statuses.update_story_status,
            request_builder=lambda request, state: {
                **request,
                "preview": dict(state.get("story_status_preview", {})),
            },
            tracking_payload_builder=lambda request, result: {
                "story_id": request.get("story_id"),
                "previous_status": result.get("previous_status"),
                "current_status": result.get("current_status"),
            },
            completed_payload_builder=lambda request, _result: {"story_id": request.get("story_id")},
        )

    def stop_without_transition(state: AgentState) -> dict[str, Any]:
        return stop_without_execution(
            state,
            node="stop_without_transition",
            message="Fluxo encerrado após preview, aguardando aprovação para transicionar o status da story.",
        )

    def route_after_validation(state: AgentState) -> str:
        return route_after_errors(state, next_node="preview_status_transition", stop_node="stop_without_transition")

    def route_after_preview(state: AgentState) -> str:
        return route_after_errors(state, next_node="gate_status_transition", stop_node="stop_without_transition")

    def route_after_gate(state: AgentState) -> str:
        pending = dict(state.get("pending_approval", {}))
        return "execute_status_transition" if pending.get("approved") else "stop_without_transition"

    graph.add_node("validate_request", validate_request)
    graph.add_node("preview_status_transition", preview_status_transition)
    graph.add_node("gate_status_transition", gate_status_transition)
    graph.add_node("execute_status_transition", execute_status_transition)
    graph.add_node("stop_without_transition", stop_without_transition)

    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges(
        "validate_request",
        route_after_validation,
        {
            "preview_status_transition": "preview_status_transition",
            "stop_without_transition": "stop_without_transition",
        },
    )
    graph.add_conditional_edges(
        "preview_status_transition",
        route_after_preview,
        {
            "gate_status_transition": "gate_status_transition",
            "stop_without_transition": "stop_without_transition",
        },
    )
    graph.add_conditional_edges(
        "gate_status_transition",
        route_after_gate,
        {
            "execute_status_transition": "execute_status_transition",
            "stop_without_transition": "stop_without_transition",
        },
    )
    graph.add_edge("execute_status_transition", END)
    graph.add_edge("stop_without_transition", END)

    return graph.compile()