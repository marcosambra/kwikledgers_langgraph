from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from kwikledgers_langgraph.state import AgentState


EXECUTION_CAPABILITY_KEY_ENV_VAR = "KWIKLEDGERS_FASTMCP_EXECUTION_KEY"
INTERNAL_RUNTIME_CAPABILITIES = frozenset({
    "sync_daily_tracking",
    "update_task_control",
    "append_daily_action_log",
    "record_ai_metrics",
    "send_notification",
    "create_calendar_event",
    "schedule_reminder",
})


class ExecutionCapabilityError(RuntimeError, ValueError):
    """Raised when LangGraph cannot issue a FastMCP execute capability."""


def issue_execution_capability(
    state: AgentState,
    *,
    action: str,
    capability_name: str,
    arguments: dict[str, Any],
    now: datetime | None = None,
) -> str:
    pending_approval = dict(state.get("pending_approval", {}))
    observability = dict(state.get("observability", {}))
    if pending_approval.get("action") != action or not pending_approval.get("approved"):
        raise ExecutionCapabilityError("O LangGraph não possui aprovação válida para emitir a capability de execução.")
    if pending_approval.get("run_id") != observability.get("active_run_id"):
        raise ExecutionCapabilityError("A aprovação não pertence à execução ativa do LangGraph.")

    signing_key = os.environ.get(EXECUTION_CAPABILITY_KEY_ENV_VAR, "").strip()
    if not signing_key:
        raise ExecutionCapabilityError("A chave de capability de execução do FastMCP não está configurada.")

    return _sign_capability(
        capability_name=capability_name,
        arguments=arguments,
        run_id=str(observability.get("active_run_id") or ""),
        request_id=str(pending_approval.get("request_id") or ""),
        now=now,
    )


def issue_lifecycle_execution_capability(
    state: dict[str, Any],
    *,
    action: str,
    capability_name: str,
    arguments: dict[str, Any],
    now: datetime | None = None,
) -> str:
    from kwikledgers_langgraph.lifecycle_adapters import (
        active_state, canonical, validate_lifecycle_execution,
    )

    try:
        trusted = active_state(action)
        binding = trusted["verified_lifecycle_binding"]
        if canonical(state) != canonical(trusted):
            raise ValueError("Lifecycle issuer requires the authenticated active state")
        if any(not isinstance(binding.get(field), str) or not binding[field].strip()
               for field in ("run_id", "request_id")):
            raise ValueError("Lifecycle issuer requires nonblank run and request identifiers")
        validate_lifecycle_execution(trusted, action=action, capability_name=capability_name, arguments=arguments)
        current_time = datetime.now(timezone.utc)
        issued_at = now if now is not None else current_time
        claims = trusted["pending_approval"]["receipt"]["claims"]
        receipt_issued_at = datetime.fromisoformat(claims["issued_at"])
        receipt_expires_at = datetime.fromisoformat(claims["expires_at"])
        if (not isinstance(issued_at, datetime) or issued_at.utcoffset() is None
                or receipt_issued_at.utcoffset() is None or receipt_expires_at.utcoffset() is None
                or not receipt_issued_at <= issued_at < receipt_expires_at
                or issued_at > current_time):
            raise ValueError("Lifecycle receipt is not valid at capability issuance")
    except (KeyError, TypeError, ValueError) as error:
        raise ExecutionCapabilityError("Authenticated lifecycle approval required for execution capability.") from error

    return _sign_capability(
        capability_name=capability_name,
        arguments=arguments,
        run_id=trusted["run_id"],
        request_id=binding["request_id"],
        now=issued_at,
        expires_at=receipt_expires_at,
    )


def issue_runtime_execution_capability(
    state: AgentState,
    *,
    action: str,
    capability_name: str,
    arguments: dict[str, Any],
    now: datetime | None = None,
) -> str:
    """Authorize non-Azure runtime effects bound to the active LangGraph run."""
    if capability_name not in INTERNAL_RUNTIME_CAPABILITIES:
        raise ExecutionCapabilityError("A capability interna solicitada não é permitida.")
    observability = dict(state.get("observability", {}))
    run_id = str(observability.get("active_run_id") or "").strip()
    if not run_id or not str(action).strip():
        raise ExecutionCapabilityError("O efeito interno exige uma execução ativa do LangGraph.")
    return _sign_capability(
        capability_name=capability_name,
        arguments=arguments,
        run_id=run_id,
        request_id=f"runtime:{action}",
        now=now,
    )


def _sign_capability(
    *,
    capability_name: str,
    arguments: dict[str, Any],
    run_id: str,
    request_id: str,
    now: datetime | None,
    expires_at: datetime | None = None,
) -> str:
    signing_key = os.environ.get(EXECUTION_CAPABILITY_KEY_ENV_VAR, "").strip()
    if not signing_key:
        raise ExecutionCapabilityError("A chave de capability de execução do FastMCP não está configurada.")

    issued_at = now or datetime.now(timezone.utc)
    expiration = issued_at + timedelta(minutes=5)
    if expires_at is not None:
        expiration = min(expiration, expires_at)
    payload = {
        "capability": capability_name,
        "payload_digest": _payload_digest(arguments),
        "run_id": run_id,
        "request_id": request_id,
        "nonce": uuid4().hex,
        "issued_at": issued_at.isoformat(),
        "expires_at": expiration.isoformat(),
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    signature = hmac.new(signing_key.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"{encoded_payload}.{signature}"


def _payload_digest(arguments: dict[str, Any]) -> str:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()