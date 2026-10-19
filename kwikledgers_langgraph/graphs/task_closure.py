from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import TaskClosurePorts
from kwikledgers_langgraph.graphs.mutable_flow import (
    route_after_errors,
    run_execute_step,
    run_mutation_gate,
    run_preview_step,
    stop_without_execution,
    validate_required_request,
)
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_error
from kwikledgers_langgraph.task_closure_policy import build_verified_task_closure_preview


def _required_request_fields() -> tuple[str, ...]:
    return ("task_id", "start_time")


def build_task_closure_graph(ports: TaskClosurePorts) -> Any:
    graph = StateGraph(AgentState)

    def validate_request(state: AgentState) -> dict[str, Any]:
        return validate_required_request(
            state,
            request_key="task_closure_request",
            required_fields=_required_request_fields(),
            node="validate_request",
            started_message="Validando payload do fluxo de fechamento da task.",
            failed_message="Payload do fluxo de fechamento da task invalido.",
            completed_message="Payload do fluxo de fechamento da task validado.",
            missing_error_prefix="Payload de fechamento de task incompleto: ",
        )

    def preview_task_closure(state: AgentState) -> dict[str, Any]:
        def build_preview(request: dict[str, Any]) -> dict[str, Any]:
            effective_request = dict(request)
            previous_preview = dict(state.get("task_closure_preview", {}))
            if not effective_request.get("close_time") and previous_preview.get("close_time"):
                effective_request["close_time"] = previous_preview["close_time"]
            preview = build_verified_task_closure_preview(effective_request)
            native_preview_operation = getattr(ports.closures, "preview_task_closure", None)
            if not callable(native_preview_operation):
                raise ValueError("Native task closure preview is required for FastMCP.")
            native_preview = native_preview_operation(effective_request)
            if not isinstance(native_preview, dict):
                raise ValueError("Native task closure preview must be structured.")
            revision = native_preview.get("task_revision")
            if native_preview.get("task_id") != preview["task_id"] or type(revision) is not int or revision <= 0:
                raise ValueError("Native task closure preview must bind the task and a positive revision.")
            preview["task_revision"] = revision
            if native_preview.get("execution_requires_lifecycle"):
                preview["execution_requires_lifecycle"] = native_preview["execution_requires_lifecycle"]
            if effective_request.get("extraordinary") is True:
                preview["extraordinary"] = True
                preview.pop("execution_requires_lifecycle", None)
            return preview

        def preview_validator(state: AgentState, _request: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any] | None:
            if preview.get("closure_allowed"):
                return None
            error_updates = append_error(
                state,
                "Fechamento de task bloqueado: " + "; ".join(preview.get("validation_messages", [])),
            )
            failed = record_runtime_event(
                state,
                node="preview_task_closure",
                status="failed",
                message="Preview do fechamento da task identificou um horario invalido.",
                payload={"validation_messages": preview.get("validation_messages", [])},
            )
            return {
                **error_updates,
                **failed,
                "task_closure_result": {"status": "invalid_closure", "preview_available": True},
            }

        return run_preview_step(
            state,
            request_key="task_closure_request",
            preview_key="task_closure_preview",
            result_key="task_closure_result",
            action="task_closure.preview",
            node="preview_task_closure",
            started_message="Calculando o preview de fechamento da task.",
            failed_message="Falha ao calcular o preview de fechamento da task.",
            completed_message="Preview de fechamento da task calculado com sucesso.",
            tracking_category="task_closure_preview",
            tracking_message="Preview de fechamento de task gerado pelo runtime LangGraph.",
            preview_operation=build_preview,
            tracking_payload_builder=lambda request, preview: {
                "task_id": request.get("task_id"),
                "computed_hours": preview.get("computed_hours"),
            },
            completed_payload_builder=lambda request, _preview: {"task_id": request.get("task_id")},
            preview_validator=preview_validator,
        )

    def gate_task_closure(state: AgentState) -> dict[str, Any]:
        return run_mutation_gate(
            state,
            preview_key="task_closure_preview",
            result_key="task_closure_result",
            action="task_closure.execute",
            node="gate_task_closure",
            approval_required_message="Fluxo aguardando aprovação explícita para fechar a task.",
            approved_message="Aprovação explícita detectada para fechar a task.",
        )

    def execute_task_closure(state: AgentState) -> dict[str, Any]:
        return run_execute_step(
            state,
            request_key="task_closure_request",
            result_key="task_closure_result",
            node="execute_task_closure",
            started_message="Fechando a task após aprovação explícita.",
            failed_message="Falha ao fechar a task.",
            completed_message="Fechamento da task concluido com sucesso.",
            tracking_category="task_closure",
            tracking_message="Fechamento de task executado pelo runtime LangGraph.",
            execute_operation=ports.closures.close_task,
            request_builder=lambda request, state: {
                **request,
                "preview": dict(state.get("task_closure_preview", {})),
            },
            fastmcp_capability_name="update_work_item_effort",
            approval_action="task_closure.execute",
            tracking_payload_builder=lambda request, result: {
                "task_id": request.get("task_id"),
                "completed_work_hours": result.get("completed_work_hours"),
            },
            completed_payload_builder=lambda request, _result: {"task_id": request.get("task_id")},
        )

    def stop_without_closure(state: AgentState) -> dict[str, Any]:
        return stop_without_execution(
            state,
            node="stop_without_closure",
            message="Fluxo encerrado após preview, aguardando aprovação para fechar a task.",
        )

    def route_after_validation(state: AgentState) -> str:
        return route_after_errors(state, next_node="preview_task_closure", stop_node="stop_without_closure")

    def route_after_preview(state: AgentState) -> str:
        return route_after_errors(state, next_node="gate_task_closure", stop_node="stop_without_closure")

    def route_after_gate(state: AgentState) -> str:
        pending = dict(state.get("pending_approval", {}))
        return "execute_task_closure" if pending.get("approved") else "stop_without_closure"

    graph.add_node("validate_request", validate_request)
    graph.add_node("preview_task_closure", preview_task_closure)
    graph.add_node("gate_task_closure", gate_task_closure)
    graph.add_node("execute_task_closure", execute_task_closure)
    graph.add_node("stop_without_closure", stop_without_closure)

    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges(
        "validate_request",
        route_after_validation,
        {
            "preview_task_closure": "preview_task_closure",
            "stop_without_closure": "stop_without_closure",
        },
    )
    graph.add_conditional_edges(
        "preview_task_closure",
        route_after_preview,
        {
            "gate_task_closure": "gate_task_closure",
            "stop_without_closure": "stop_without_closure",
        },
    )
    graph.add_conditional_edges(
        "gate_task_closure",
        route_after_gate,
        {
            "execute_task_closure": "execute_task_closure",
            "stop_without_closure": "stop_without_closure",
        },
    )
    graph.add_edge("execute_task_closure", END)
    graph.add_edge("stop_without_closure", END)

    return graph.compile()
