from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import StoryBranchAssociationPorts
from kwikledgers_langgraph.graphs.mutable_flow import (
    route_after_errors,
    run_execute_step,
    run_mutation_gate,
    run_preview_step,
    stop_without_execution,
    validate_required_request,
)
from kwikledgers_langgraph.state import AgentState


def _required_request_fields() -> tuple[str, ...]:
    return ("story_id", "repository_name", "source_branch")


def build_story_branch_association_graph(ports: StoryBranchAssociationPorts) -> Any:
    graph = StateGraph(AgentState)

    def validate_request(state: AgentState) -> dict[str, Any]:
        return validate_required_request(
            state,
            request_key="branch_association_request",
            required_fields=_required_request_fields(),
            node="validate_request",
            started_message="Validando payload do fluxo de associacao de branch da story.",
            failed_message="Payload do fluxo de associacao de branch invalido.",
            completed_message="Payload do fluxo de associacao de branch validado.",
            missing_error_prefix="Payload de associacao de branch incompleto: ",
        )

    def preview_branch_association(state: AgentState) -> dict[str, Any]:
        return run_preview_step(
            state,
            request_key="branch_association_request",
            preview_key="branch_association_preview",
            result_key="branch_association_result",
            action="story_branch_association.preview",
            node="preview_branch_association",
            started_message="Gerando preview da associacao de branch da story.",
            failed_message="Falha ao gerar o preview da associacao de branch da story.",
            completed_message="Preview da associacao de branch gerado com sucesso.",
            tracking_category="branch_association_preview",
            tracking_message="Preview da associacao de branch da story gerado pelo runtime LangGraph.",
            preview_operation=ports.branches.preview_story_branch_association,
            tracking_payload_builder=lambda request, _preview: {
                "story_id": request.get("story_id"),
                "repository_name": request.get("repository_name"),
            },
            completed_payload_builder=lambda request, _preview: {"story_id": request.get("story_id")},
        )

    def gate_associate_branch(state: AgentState) -> dict[str, Any]:
        return run_mutation_gate(
            state,
            preview_key="branch_association_preview",
            result_key="branch_association_result",
            action="story_branch_association.execute",
            node="gate_associate_branch",
            approval_required_message="Fluxo aguardando aprovação explícita para associar a branch da story.",
            approved_message="Aprovação explícita detectada para associar a branch da story.",
        )

    def associate_branch(state: AgentState) -> dict[str, Any]:
        return run_execute_step(
            state,
            request_key="branch_association_request",
            result_key="branch_association_result",
            node="associate_branch",
            started_message="Associando a branch da story após aprovação explícita.",
            failed_message="Falha ao associar a branch da story.",
            completed_message="Associacao de branch da story concluida com sucesso.",
            tracking_category="branch_association",
            tracking_message="Associacao de branch da story executada pelo runtime LangGraph.",
            execute_operation=ports.branches.associate_story_branch,
            tracking_payload_builder=lambda request, _result: {
                "story_id": request.get("story_id"),
                "repository_name": request.get("repository_name"),
            },
            completed_payload_builder=lambda request, _result: {"story_id": request.get("story_id")},
        )

    def stop_without_association(state: AgentState) -> dict[str, Any]:
        return stop_without_execution(
            state,
            node="stop_without_association",
            message="Fluxo encerrado após preview, aguardando aprovação para associar a branch.",
        )

    def route_after_validation(state: AgentState) -> str:
        return route_after_errors(state, next_node="preview_branch_association", stop_node="stop_without_association")

    def route_after_preview(state: AgentState) -> str:
        return route_after_errors(state, next_node="gate_associate_branch", stop_node="stop_without_association")

    def route_after_gate(state: AgentState) -> str:
        pending = dict(state.get("pending_approval", {}))
        return "associate_branch" if pending.get("approved") else "stop_without_association"

    graph.add_node("validate_request", validate_request)
    graph.add_node("preview_branch_association", preview_branch_association)
    graph.add_node("gate_associate_branch", gate_associate_branch)
    graph.add_node("associate_branch", associate_branch)
    graph.add_node("stop_without_association", stop_without_association)

    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges(
        "validate_request",
        route_after_validation,
        {
            "preview_branch_association": "preview_branch_association",
            "stop_without_association": "stop_without_association",
        },
    )
    graph.add_conditional_edges(
        "preview_branch_association",
        route_after_preview,
        {
            "gate_associate_branch": "gate_associate_branch",
            "stop_without_association": "stop_without_association",
        },
    )
    graph.add_conditional_edges(
        "gate_associate_branch",
        route_after_gate,
        {
            "associate_branch": "associate_branch",
            "stop_without_association": "stop_without_association",
        },
    )
    graph.add_edge("associate_branch", END)
    graph.add_edge("stop_without_association", END)

    return graph.compile()