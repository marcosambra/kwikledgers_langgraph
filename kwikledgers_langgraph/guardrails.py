from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any

from kwikledgers_langgraph.lifecycle_adapters import SignedReceiptApproval, approval_binding
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
    AUTONOMOUS_AZURE_HOURS = "autonomous_azure_hours"


# Única exceção autorizada para escrita no Azure sem aprovação humana prévia.
AUTONOMOUS_ACTIONS = frozenset({"day_end_closure.execute"})


class ApprovalRequiredError(RuntimeError):
    """Raised when a guarded operation requires explicit approval."""


def is_legacy_backend(state: AgentState) -> bool:
    return str(dict(state.get("observability", {})).get("backend_name") or "fastmcp").strip().lower() == "legacy"


def _preview_digest(preview: dict[str, Any]) -> str:
    return sha256(json.dumps(preview, sort_keys=True, allow_nan=False).encode()).hexdigest()


def verify_signed_approval(pending: dict[str, Any], receipt: Any) -> bool:
    try:
        if pending.get("preview_digest") != _preview_digest(pending["preview"]):
            return False
        if not pending.get("run_id") or not pending.get("request_id"):
            return False
        verifier = SignedReceiptApproval(os.environ.get("KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY", "").encode())
        return verifier.verify(approval_binding(pending), receipt) is True
    except (KeyError, TypeError, ValueError):
        return False


def require_signed_execution_approval(state: AgentState, *, action: str) -> None:
    pending = dict(state.get("pending_approval", {}))
    run_id = dict(state.get("observability", {})).get("active_run_id")
    if (pending.get("approved") is not True or pending.get("action") != action
            or not run_id or pending.get("run_id") != run_id
            or not verify_signed_approval(pending, pending.get("receipt"))):
        raise ApprovalRequiredError("Authenticated approval receipt required before execution.")


def load_resume_confirmation(
    state: AgentState, *, backend_name: str, receipt_file: str | None,
    resume_state_file: str, approval_request_id: str | None, approval_source: str,
) -> dict[str, Any]:
    saved_backend = str(dict(state.get("observability", {})).get("backend_name") or "fastmcp").strip().lower()
    if backend_name != saved_backend:
        raise ValueError("O backend solicitado não corresponde ao preview salvo.")
    pending = dict(state.get("pending_approval", {}))
    if backend_name == "legacy":
        if not approval_request_id or approval_request_id != pending.get("request_id"):
            raise ValueError("Informe '--approval-request-id' correspondente ao preview legado.")
        return build_approval_confirmation(
            request_id=approval_request_id, approved=True, approval_source=approval_source,
        )
    if not receipt_file:
        raise ValueError("Informe '--receipt' com a aprovação assinada independente.")
    if Path(receipt_file).resolve() == Path(resume_state_file).resolve():
        raise ValueError("O recibo deve estar em um arquivo separado do estado de retomada.")
    try:
        receipt = json.loads(Path(receipt_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Não foi possível ler o recibo de aprovação.") from error
    run_id = dict(state.get("observability", {})).get("active_run_id")
    if not run_id or pending.get("run_id") != run_id or not verify_signed_approval(pending, receipt):
        raise ValueError("O recibo de aprovação é inválido, expirou ou não corresponde ao preview salvo.")
    if approval_request_id is not None and approval_request_id != pending.get("request_id"):
        raise ValueError("O request_id informado não corresponde ao recibo.")
    return receipt


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
    if not is_legacy_backend(state):
        approval["preview_digest"] = _preview_digest(approval["preview"])
        approval["previewed_at"] = datetime.now(timezone.utc).isoformat()
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
    raw_pending = state.get("pending_approval", {})
    raw_confirmation = state.get("approval_confirmation", {})
    pending = dict(raw_pending) if isinstance(raw_pending, dict) else {}
    confirmation = dict(raw_confirmation) if isinstance(raw_confirmation, dict) else {}
    observability = dict(state.get("observability", {}))
    requires_approval = operation_kind == OperationKind.AZURE_MUTATION
    approved = False
    reason = "read_only_auto_approved"

    if operation_kind == OperationKind.AUTONOMOUS_AZURE_HOURS:
        if action not in AUTONOMOUS_ACTIONS:
            raise ApprovalRequiredError(f"Ação '{action}' não possui exceção autônoma e exige aprovação explícita.")
        if observability.get("flow_name") != "day_end_closure" or not preview or preview.get("status") != "ready" or preview.get("dry_run"):
            raise ApprovalRequiredError("A excecao de horas exige o preview pronto do fluxo day_end_closure.")
        reason = "autonomous_hours_exception"

    if requires_approval:
        same_action = pending.get("action") == action
        same_preview = pending.get("preview", {}) == (preview or {})
        same_run = pending.get("run_id") == observability.get("active_run_id")
        replay_ready = bool(state.get("approval_resume_ready"))
        matching_request = pending.get("request_id") if same_action and same_preview and same_run else None
        if is_legacy_backend(state):
            same_request = bool(matching_request and confirmation.get("request_id") == matching_request)
            approved = bool(replay_ready and same_request and confirmation.get("approved") is True)
        else:
            approved = bool(replay_ready and matching_request and verify_signed_approval(pending, confirmation))
        if confirmation and not approved:
            reason = "approval_context_mismatch"
        elif approved:
            reason = "approved_by_state" if is_legacy_backend(state) else "approved_by_signed_receipt"
        else:
            reason = "explicit_approval_required"
    else:
        approved = True

    updates: dict[str, Any] = {}
    if requires_approval:
        approval_source = confirmation.get(
            "approval_source",
            pending.get("approval_source", "pending_review"),
        )
        if matching_request:
            updates["pending_approval"] = {**pending, "approved": approved, "approval_source": approval_source}
        else:
            updates["pending_approval"] = build_bound_approval(
                state, action=action, preview=preview, approved=approved,
                approval_source=approval_source,
            )
        updates["pending_approval"].pop("receipt", None)
        if approved and not is_legacy_backend(state):
            updates["pending_approval"]["receipt"] = confirmation

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