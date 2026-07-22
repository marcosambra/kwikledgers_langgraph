from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import StoryPullRequestPorts
from kwikledgers_langgraph.guardrails import (
    OperationKind,
    allow_without_approval,
    build_approval_confirmation,
    build_bound_approval,
    evaluate_operation,
)
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_error, append_tracking_event


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
        started = record_runtime_event(
            state,
            node="validate_request",
            status="started",
            message="Validando payload do fluxo de PR da story.",
        )
        request = dict(state.get("pull_request_request", {}))
        missing_fields = [field for field in _required_request_fields() if not request.get(field)]
        if missing_fields:
            error_updates = append_error(
                state,
                "Payload de PR da story incompleto: " + ", ".join(missing_fields),
            )
            completed = record_runtime_event(
                state,
                node="validate_request",
                status="failed",
                message="Payload do fluxo de PR invalido.",
                payload={"missing_fields": missing_fields},
            )
            return {**started, **error_updates, **completed}

        completed = record_runtime_event(
            state,
            node="validate_request",
            status="completed",
            message="Payload do fluxo de PR validado.",
        )
        return {**started, **completed}

    def preview_pull_request(state: AgentState) -> dict[str, Any]:
        guardrail = allow_without_approval(
            state,
            action="story_pull_request.preview",
            operation_kind=OperationKind.READ_ONLY,
        )
        started = record_runtime_event(
            state,
            node="preview_pull_request",
            status="started",
            message="Gerando preview da PR da story.",
        )
        previous_preview = dict(state.get("pull_request_preview", {}))
        request = dict(state.get("pull_request_request", {}))
        preview = ports.pull_requests.preview_story_pull_request(request)
        event = append_tracking_event(
            state,
            category="pull_request_preview",
            message="Preview da PR da story gerado pelo runtime LangGraph.",
            payload={"title": preview.get("title"), "story_id": request.get("story_id")},
        )
        completed = record_runtime_event(
            state,
            node="preview_pull_request",
            status="completed",
            message="Preview da PR da story gerado com sucesso.",
            payload={"title": preview.get("title")},
        )
        return {
            **guardrail,
            **started,
            **event,
            **completed,
            "pull_request_preview": preview,
            "approval_resume_ready": bool(previous_preview) and previous_preview == preview,
        }

    def gate_create_pull_request(state: AgentState) -> dict[str, Any]:
        preview = dict(state.get("pull_request_preview", {}))
        evaluation_state: AgentState = state
        simulation_updates: dict[str, Any] = {}
        if state.get("approval_simulation_requested"):
            simulated_pending = build_bound_approval(
                state,
                action="story_pull_request.create",
                preview=preview,
                approved=False,
                approval_source="local_cli_simulation",
            )
            simulation_updates["pending_approval"] = simulated_pending
            evaluation_state = {
                **state,
                "pending_approval": simulated_pending,
                "approval_confirmation": build_approval_confirmation(
                    request_id=simulated_pending["request_id"],
                    approved=True,
                    approval_source="local_cli_simulation",
                ),
                "approval_resume_ready": True,
            }

        updates = evaluate_operation(
            evaluation_state,
            action="story_pull_request.create",
            operation_kind=OperationKind.AZURE_MUTATION,
            preview=preview,
        )
        message = "Fluxo aguardando aprovação explícita para criar a PR da story."
        if updates.get("pending_approval", {}).get("approved"):
            message = "Aprovação explícita detectada para criar a PR da story."

        event = append_tracking_event(
            state,
            category="approval",
            message=message,
            payload={"approved": updates.get("pending_approval", {}).get("approved", False)},
        )
        runtime_event = record_runtime_event(
            state,
            node="gate_create_pull_request",
            status="completed",
            message=message,
        )
        return {
            **simulation_updates,
            **updates,
            **event,
            **runtime_event,
            "pull_request_result": {
                "status": "approved" if updates.get("pending_approval", {}).get("approved") else "approval_required",
                "preview_available": bool(preview),
            },
        }

    def create_pull_request(state: AgentState) -> dict[str, Any]:
        started = record_runtime_event(
            state,
            node="create_pull_request",
            status="started",
            message="Criando a PR da story após aprovação explícita.",
        )
        request = dict(state.get("pull_request_request", {}))
        result = ports.pull_requests.create_story_pull_request(request)
        event = append_tracking_event(
            state,
            category="pull_request",
            message="PR da story criada pelo runtime LangGraph.",
            payload={"story_id": request.get("story_id"), "title": result.get("title")},
        )
        completed = record_runtime_event(
            state,
            node="create_pull_request",
            status="completed",
            message="PR da story criada com sucesso.",
            payload={"title": result.get("title")},
        )
        return {**started, **event, **completed, "pull_request_result": result}

    def stop_without_create(state: AgentState) -> dict[str, Any]:
        return record_runtime_event(
            state,
            node="stop_without_create",
            status="completed",
            message="Fluxo encerrado após preview, aguardando aprovação para criar a PR.",
        )

    def route_after_validation(state: AgentState) -> str:
        return "stop_without_create" if state.get("errors") else "preview_pull_request"

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
    graph.add_edge("preview_pull_request", "gate_create_pull_request")
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