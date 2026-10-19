from __future__ import annotations

import json
import os
from pathlib import Path
from collections.abc import Callable
from typing import Any

from kwikledgers_langgraph.fastmcp_adapters import technical_arguments_for_capability
from kwikledgers_langgraph.fastmcp_execution_capability import (
    DEFAULT_EXECUTION_CAPABILITY_LEDGER, EXECUTION_CAPABILITY_LEDGER_ENV_VAR, issue_execution_capability,
)
from kwikledgers_langgraph.lifecycle_adapters import LifecycleJournal
from kwikledgers_langgraph.guardrails import (
    ApprovalRequiredError,
    OperationKind,
    allow_without_approval,
    evaluate_operation,
    require_execution_approval,
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
        capability_name = {
            "story_branch_association.preview": "associate_story_branch",
            "story_pull_request.preview": "create_story_pull_request",
            "story_status_transition.preview": "update_story_status",
            "task_closure.preview": "update_work_item_effort",
        }.get(action)
        if capability_name and preview.get("closure_allowed") is not False:
            preview = {
                **preview,
                "execution_arguments": technical_arguments_for_capability(
                    capability_name, {**request, "preview": preview},
                ),
            }
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
    lifecycle = preview.get("execution_requires_lifecycle")
    if lifecycle:
        message = f"Execution requires lifecycle {lifecycle}; this entry provides preview only."
        return {**append_error(state, message),
                **record_runtime_event(state, node=node, status="blocked", message=message),
                "pending_approval": {},
                result_key: {"status": "lifecycle_required", "changed": False,
                             "preview_available": True, "required_lifecycle": lifecycle}}
    updates = evaluate_operation(
        state,
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
    attempted = False
    replayed = False
    try:
        effective_request = request_builder(request, state) if request_builder is not None else dict(request)
        effective_request.pop("execution_capability", None)
        if fastmcp_capability_name:
            require_execution_approval(state, action=approval_action or node)
            approved_preview = dict(state["pending_approval"]["preview"])
            arguments = technical_arguments_for_capability(fastmcp_capability_name, effective_request)
            approved_arguments = approved_preview.get("execution_arguments")
            if approved_arguments is None:
                approved_arguments = technical_arguments_for_capability(
                    fastmcp_capability_name, {**approved_preview, "preview": approved_preview},
                )
            if json.dumps(arguments, sort_keys=True, allow_nan=False) != json.dumps(
                approved_arguments, sort_keys=True, allow_nan=False,
            ):
                raise ApprovalRequiredError("Execution payload does not match the approved preview.")
            def invoke() -> dict[str, Any]:
                nonlocal attempted
                effective_request["execution_capability"] = issue_execution_capability(
                    state, action=approval_action or node,
                    capability_name=fastmcp_capability_name, arguments=arguments,
                )
                attempted = True
                return execute_operation(effective_request)

            ledger = Path(os.environ.get(EXECUTION_CAPABILITY_LEDGER_ENV_VAR,
                                         str(DEFAULT_EXECUTION_CAPABILITY_LEDGER)))
            journal = LifecycleJournal(ledger.with_name("mutable-operations.sqlite3"))
            try:
                pending = state["pending_approval"]
                result = journal.once(f'mutable:{pending["run_id"]}:{pending["request_id"]}',
                                      {"action": pending["action"], "arguments": arguments}, invoke)
                replayed = not attempted
            finally:
                journal.close()
        else:
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
        unknown = attempted or str(error) in (
            "Execution outcome unknown; reconcile before retry",
            "Execution already reserved; reconcile before retry",
        )
        return {
            **started,
            **error_updates,
            **failed,
            result_key: {"status": "reconciliation_required" if unknown else "execute_failed",
                         "changed": None if unknown else False},
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
    if replayed:
        completed = record_runtime_event({**state, **completed}, node="duplicate_prevented", status="completed",
                                         message="Durable operation result reused; duplicate write prevented.")
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