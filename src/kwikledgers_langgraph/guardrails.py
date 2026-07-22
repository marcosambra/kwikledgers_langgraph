from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from kwikledgers_langgraph.state import (
    AgentState,
    append_decision,
    append_error,
    append_tracking_event,
)


class OperationKind(str, Enum):
    READ_ONLY = "read_only"
    AZURE_MUTATION = "azure_mutation"
    LOCAL_TRACKING_WRITE = "local_tracking_write"
    NOTIFICATION = "notification"


class ApprovalRequiredError(RuntimeError):
    """Raised when a guarded operation requires explicit approval."""


def build_bound_approval(
    state: AgentState,
    *,
    action: str,
    preview: dict[str, Any] | None = None,
    approved: bool,
    approval_source: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    observability = dict(state.get("observability", {}))
    approval: dict[str, Any] = {
        "action": action,
        "preview": preview or {},
        "approved": approved,
        "request_id": request_id or str(uuid.uuid4()),
        "approval_source": approval_source,
    }
    active_run_id = observability.get("active_run_id")
    if active_run_id:
        approval["run_id"] = active_run_id
    return approval


def build_approval_confirmation(
    *,
    request_id: str,
    approved: bool,
    approval_source: str,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "approved": approved,
        "approval_source": approval_source,
    }


def evaluate_operation(
    state: AgentState,
    *,
    action: str,
    operation_kind: OperationKind,
    preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pending = dict(state.get("pending_approval", {}))
    confirmation = dict(state.get("approval_confirmation", {}))
    observability = dict(state.get("observability", {}))
    requires_approval = operation_kind == OperationKind.AZURE_MUTATION
    approved = False
    reason = "read_only_auto_approved"

    if requires_approval:
        same_action = pending.get("action") == action
        same_preview = pending.get("preview", {}) == (preview or {})
        same_run = pending.get("run_id") == observability.get("active_run_id")
        replay_ready = bool(state.get("approval_resume_ready"))
        matching_request = pending.get("request_id") if same_action and same_preview and same_run else None
        same_request = bool(
            matching_request and confirmation.get("request_id") == matching_request
        )
        approved = bool(
            replay_ready and same_request and confirmation.get("approved")
        )
        if confirmation.get("approved") and not approved:
            reason = "approval_context_mismatch"
        else:
            reason = "approved_by_state" if approved else "explicit_approval_required"
    else:
        approved = True

    updates: dict[str, Any] = {}
    if requires_approval:
        approval_source = confirmation.get(
            "approval_source",
            pending.get("approval_source", "pending_review"),
        )
        updates["pending_approval"] = build_bound_approval(
            state,
            action=action,
            preview=preview,
            approved=approved,
            approval_source=approval_source,
            request_id=matching_request,
        )

    decision = append_decision(state, f"guardrail.{action}.{reason}")
    event = append_tracking_event(
        state,
        category="guardrail",
        message=f"Guardrail avaliado para '{action}' com resultado '{reason}'.",
        payload={
            "operation_kind": operation_kind.value,
            "requires_approval": requires_approval,
            "approved": approved,
            "reason": reason,
        },
    )
    return {**decision, **event, **updates}


def require_explicit_approval(
    state: AgentState,
    *,
    action: str,
    preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updates = evaluate_operation(
        state,
        action=action,
        operation_kind=OperationKind.AZURE_MUTATION,
        preview=preview,
    )
    pending = updates.get("pending_approval", {})
    if pending.get("approved"):
        return updates

    error_updates = append_error(
        state,
        f"Ação '{action}' bloqueada ate aprovação explícita.",
    )
    raise ApprovalRequiredError(error_updates["errors"][-1])


def allow_without_approval(
    state: AgentState,
    *,
    action: str,
    operation_kind: OperationKind,
) -> dict[str, Any]:
    return evaluate_operation(
        state,
        action=action,
        operation_kind=operation_kind,
    )