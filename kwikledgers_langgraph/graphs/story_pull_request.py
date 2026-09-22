from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import StoryPullRequestPorts
from kwikledgers_langgraph.observability import record_runtime_event
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
    return (
        "story_id",
        "repository_name",
        "source_branch",
        "what_was_changed",
        "affected_processes",
        "expected_impacts",
        "important_points",
        "mermaid_diagram",
    )


def build_story_pull_request_graph(ports: StoryPullRequestPorts) -> Any:
    graph = StateGraph(AgentState)

    def validate_request(state: AgentState) -> dict[str, Any]:
        return validate_required_request(
            state,
            request_key="pull_request_request",
            required_fields=_required_request_fields(),
            node="validate_request",
            started_message="Validando payload do fluxo de PR da story.",
            failed_message="Payload do fluxo de PR invalido.",
            completed_message="Payload do fluxo de PR validado.",
            missing_error_prefix="Payload de PR da story incompleto: ",
        )

    def preview_pull_request(state: AgentState) -> dict[str, Any]:
        return run_preview_step(
            state,
            request_key="pull_request_request",
            preview_key="pull_request_preview",
            result_key="pull_request_result",
            action="story_pull_request.preview",
            node="preview_pull_request",
            started_message="Gerando preview da PR da story.",
            failed_message="Falha ao gerar o preview da PR da story.",
            completed_message="Preview da PR da story gerado com sucesso.",
            tracking_category="pull_request_preview",
            tracking_message="Preview da PR da story gerado pelo runtime LangGraph.",
            preview_operation=ports.pull_requests.preview_story_pull_request,
            tracking_payload_builder=lambda request, preview: {
                "title": preview.get("title"),
                "story_id": request.get("story_id"),
            },
            completed_payload_builder=lambda _request, preview: {"title": preview.get("title")},
        )

    def gate_create_pull_request(state: AgentState) -> dict[str, Any]:
        return run_mutation_gate(
            state,
            preview_key="pull_request_preview",
            result_key="pull_request_result",
            action="story_pull_request.create",
            node="gate_create_pull_request",
            approval_required_message="Fluxo aguardando aprovação explícita para criar a PR da story.",
            approved_message="Aprovação explícita detectada para criar a PR da story.",
        )

    def create_pull_request(state: AgentState) -> dict[str, Any]:
        return run_execute_step(
            state,
            request_key="pull_request_request",
            result_key="pull_request_result",
            node="create_pull_request",
            started_message="Criando a PR da story após aprovação explícita.",
            failed_message="Falha ao criar a PR da story.",
            completed_message="PR da story criada com sucesso.",
            tracking_category="pull_request",
            tracking_message="PR da story criada pelo runtime LangGraph.",
            execute_operation=ports.pull_requests.create_story_pull_request,
            fastmcp_capability_name="create_story_pull_request",
            approval_action="story_pull_request.create",
            tracking_payload_builder=lambda request, result: {
                "story_id": request.get("story_id"),
                "title": result.get("title"),
            },
            completed_payload_builder=lambda _request, result: {"title": result.get("title")},
        )

    def stop_without_create(state: AgentState) -> dict[str, Any]:
        return stop_without_execution(
            state,
            node="stop_without_create",
            message="Fluxo encerrado após preview, aguardando aprovação para criar a PR.",
        )

    def route_after_validation(state: AgentState) -> str:
        return route_after_errors(state, next_node="preview_pull_request", stop_node="stop_without_create")

    def route_after_preview(state: AgentState) -> str:
        return route_after_errors(state, next_node="gate_create_pull_request", stop_node="stop_without_create")

    def route_after_gate(state: AgentState) -> str:
        pending = dict(state.get("pending_approval", {}))
        return "create_pull_request" if pending.get("approved") else "stop_without_create"

    graph.add_node("validate_request", validate_request)
    graph.add_node("preview_pull_request", preview_pull_request)
    graph.add_node("gate_create_pull_request", gate_create_pull_request)
    graph.add_node("create_pull_request", create_pull_request)
    graph.add_node("stop_without_create", stop_without_create)

    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges(
        "validate_request",
        route_after_validation,
        {
            "preview_pull_request": "preview_pull_request",
            "stop_without_create": "stop_without_create",
        },
    )
    graph.add_conditional_edges(
        "preview_pull_request",
        route_after_preview,
        {
            "gate_create_pull_request": "gate_create_pull_request",
            "stop_without_create": "stop_without_create",
        },
    )
    graph.add_conditional_edges(
        "gate_create_pull_request",
        route_after_gate,
        {
            "create_pull_request": "create_pull_request",
            "stop_without_create": "stop_without_create",
        },
    )
    graph.add_edge("create_pull_request", END)
    graph.add_edge("stop_without_create", END)

    return graph.compile()