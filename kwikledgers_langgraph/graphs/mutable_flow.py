from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kwikledgers_langgraph.fastmcp_adapters import technical_arguments_for_capability
from kwikledgers_langgraph.fastmcp_execution_capability import issue_execution_capability
from kwikledgers_langgraph.guardrails import (
    OperationKind,
    allow_without_approval,
    build_approval_confirmation,
    build_bound_approval,
    evaluate_operation,
    is_legacy_backend,
    require_signed_execution_approval,
)
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_error, append_tracking_event


PreviewValidator = Callable[[AgentState, dict[str, Any], dict[str, Any]], dict[str, Any] | None]
PayloadBuilder = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
RequestBuilder = Callable[[dict[str, Any], AgentState], dict[str, Any]]


def validate_required_request(
    state: AgentState,
    *,
    request_key: str,
    required_fields: tuple[str, ...],
    node: str,
    started_message: str,
    failed_message: str,
    completed_message: str,
    missing_error_prefix: str,
) -> dict[str, Any]:
    started = record_runtime_event(
        state,
        node=node,
        status="started",
        message=started_message,
    )
    request = dict(state.get(request_key, {}))
    missing_fields = [field for field in required_fields if not request.get(field)]
    if missing_fields:
        error_updates = append_error(
            state,
            missing_error_prefix + ", ".join(missing_fields),
        )
        failed = record_runtime_event(
            state,
            node=node,
            status="failed",
            message=failed_message,
            payload={"missing_fields": missing_fields},
        )
        return {**started, **error_updates, **failed}

    completed = record_runtime_event(
        state,
        node=node,
        status="completed",
        message=completed_message,
    )
    return {**started, **completed}


def run_preview_step(
    state: AgentState,
    *,
    request_key: str,
    preview_key: str,
    result_key: str,
    action: str,
    node: str,
    started_message: str,
    failed_message: str,
    completed_message: str,
    tracking_category: str,
    tracking_message: str,
    preview_operation: Callable[[dict[str, Any]], dict[str, Any]],
    tracking_payload_builder: PayloadBuilder | None = None,
    completed_payload_builder: PayloadBuilder | None = None,
    preview_validator: PreviewValidator | None = None,
) -> dict[str, Any]:
    guardrail = allow_without_approval(
        state,
        action=action,
        operation_kind=OperationKind.READ_ONLY,
    )
    started = record_runtime_event(
        state,
        node=node,
        status="started",
        message=started_message,
    )
    previous_preview = dict(state.get(preview_key, {}))
    request = dict(state.get(request_key, {}))
    try:
        preview = preview_operation(request)
    except Exception as error:
        error_updates = append_error(state, f"{failed_message}: {error}")
        failed = record_runtime_event(
            state,
            node=node,
            status="failed",
            message=failed_message,
            payload={"error": str(error)},
        )
        return {
            **guardrail,
            **started,
            **error_updates,
            **failed,
            result_key: {"status": "preview_failed", "preview_available": False},
        }

    preview_updates = {
        preview_key: preview,
        "approval_resume_ready": bool(previous_preview) and previous_preview == preview,
    }
    if preview_validator is not None:
        validation_updates = preview_validator(state, request, preview)
        if validation_updates is not None:
            return {
                **guardrail,
                **started,
                **preview_updates,
                **validation_updates,
            }

    event = append_tracking_event(
        state,
        category=tracking_category,
        message=tracking_message,
        payload=(tracking_payload_builder(request, preview) if tracking_payload_builder is not None else {}),
    )
    completed = record_runtime_event(
        state,
        node=node,
        status="completed",
        message=completed_message,
        payload=(completed_payload_builder(request, preview) if completed_payload_builder is not None else {}),
    )
    return {
        **guardrail,
        **started,
        **event,
        **completed,
        **preview_updates,
    }


def run_mutation_gate(
    state: AgentState,
    *,
    preview_key: str,
    result_key: str,
    action: str,
    node: str,
    approval_required_message: str,
    approved_message: str,
) -> dict[str, Any]:
    preview = dict(state.get(preview_key, {}))
    evaluation_state: AgentState = state
    simulation_updates: dict[str, Any] = {}
    if state.get("approval_simulation_requested") and is_legacy_backend(state):
        simulated_pending = build_bound_approval(
            state,
            action=action,
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
        action=action,
        operation_kind=OperationKind.AZURE_MUTATION,
        preview=preview,
    )
    approved = updates.get("pending_approval", {}).get("approved", False)
    message = approved_message if approved else approval_required_message
    event = append_tracking_event(
        state,
        category="approval",
        message=message,
        payload={"approved": approved},
    )
    runtime_event = record_runtime_event(
        state,
        node=node,
        status="completed",
        message=message,
    )
    return {
        **simulation_updates,
        **updates,
        **event,
        **runtime_event,
        result_key: {
            "status": "approved" if approved else "approval_required",
            "preview_available": bool(preview),
        },
    }


def run_execute_step(
    state: AgentState,
    *,
    request_key: str,
    result_key: str,
    node: str,
    started_message: str,
    failed_message: str,
    completed_message: str,
    tracking_category: str,
    tracking_message: str,
    execute_operation: Callable[[dict[str, Any]], dict[str, Any]],
    request_builder: RequestBuilder | None = None,
    fastmcp_capability_name: str | None = None,
    approval_action: str | None = None,
    tracking_payload_builder: PayloadBuilder | None = None,
    completed_payload_builder: PayloadBuilder | None = None,
) -> dict[str, Any]:
    started = record_runtime_event(
        state,
        node=node,
        status="started",
        message=started_message,
    )
    request = dict(state.get(request_key, {}))
    try:
        effective_request = request_builder(request, state) if request_builder is not None else dict(request)
        effective_request.pop("execution_capability", None)
        backend_name = str(dict(state.get("observability", {})).get("backend_name") or "fastmcp").strip().lower()
        if backend_name != "legacy" and fastmcp_capability_name:
            require_signed_execution_approval(state, action=approval_action or node)
            effective_request["execution_capability"] = issue_execution_capability(
                state,
                action=approval_action or node,
                capability_name=fastmcp_capability_name,
                arguments=technical_arguments_for_capability(fastmcp_capability_name, effective_request),
            )
        result = execute_operation(effective_request)
    except Exception as error:
        error_updates = append_error(state, f"{failed_message}: {error}")
        failed = record_runtime_event(
            state,
            node=node,
            status="failed",
            message=failed_message,
            payload={"error": str(error)},
        )
        return {
            **started,
            **error_updates,
            **failed,
            result_key: {"status": "execute_failed", "changed": False},
        }

    event = append_tracking_event(
        state,
        category=tracking_category,
        message=tracking_message,
        payload=(tracking_payload_builder(request, result) if tracking_payload_builder is not None else {}),
    )
    completed = record_runtime_event(
        state,
        node=node,
        status="completed",
        message=completed_message,
        payload=(completed_payload_builder(request, result) if completed_payload_builder is not None else {}),
    )
    return {**started, **event, **completed, result_key: result}


def stop_without_execution(state: AgentState, *, node: str, message: str) -> dict[str, Any]:
    return record_runtime_event(
        state,
        node=node,
        status="completed",
        message=message,
    )


def route_after_errors(state: AgentState, *, next_node: str, stop_node: str) -> str:
    return stop_node if state.get("errors") else next_node