from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from kwikledgers_langgraph.state import AgentState


EXECUTION_CAPABILITY_KEY_ENV_VAR = "KWIKLEDGERS_FASTMCP_EXECUTION_KEY"
EXECUTION_CAPABILITY_LEDGER_ENV_VAR = "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER"
DEFAULT_EXECUTION_CAPABILITY_LEDGER = (
    Path(__file__).resolve().parents[2] / "AI_Tracking/Audit/execution-capabilities.sqlite3"
)
LOCAL_CAPABILITY_SUFFIX = "local-v1"
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
    if pending_approval.get("action") != action or pending_approval.get("approved") is not True:
        raise ExecutionCapabilityError("O LangGraph não possui aprovação válida para emitir a capability de execução.")
    if pending_approval.get("run_id") != observability.get("active_run_id"):
        raise ExecutionCapabilityError("A aprovação não pertence à execução ativa do LangGraph.")
    from kwikledgers_langgraph.guardrails import AUTONOMOUS_ACTIONS, verify_signed_approval

    expiration = None
    if action not in AUTONOMOUS_ACTIONS:
        if not verify_signed_approval(pending_approval, pending_approval.get("receipt")):
            raise ExecutionCapabilityError("A execução exige recibo assinado vinculado à prévia atual.")
        claims = pending_approval["receipt"]["claims"]
        confirmed_at = datetime.fromisoformat(claims["issued_at"])
        expiration = datetime.fromisoformat(claims["expires_at"])
        issued_at = now if now is not None else datetime.now(timezone.utc)
        if (not isinstance(issued_at, datetime) or issued_at.utcoffset() is None
                or issued_at < confirmed_at or issued_at >= expiration):
            raise ExecutionCapabilityError("A emissão deve ocorrer dentro da validade do recibo assinado.")

    return _sign_capability(
        capability_name=capability_name,
        arguments=arguments,
        run_id=observability.get("active_run_id"),
        request_id=pending_approval.get("request_id"),
        action=action,
        now=now,
        expires_at=expiration,
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
        pending = trusted["pending_approval"]
        if (trusted.get("run_id") != binding["run_id"]
                or trusted.get("observability", {}).get("active_run_id") != binding["run_id"]
                or pending.get("approved") is not True or pending.get("action") != action
                or any(pending.get(field) != binding[field] for field in ("run_id", "request_id"))):
            raise ValueError("Lifecycle approval must match the active action, run and request")
        validate_lifecycle_execution(trusted, action=action, capability_name=capability_name, arguments=arguments)
        current_time = datetime.now(timezone.utc)
        issued_at = now if now is not None else current_time
        if not isinstance(issued_at, datetime) or issued_at.utcoffset() is None or issued_at > current_time:
            raise ValueError("Lifecycle capability issuance requires a valid current time")
        receipt_claims = pending["receipt"]["claims"]
        confirmed_at = datetime.fromisoformat(receipt_claims["issued_at"])
        receipt_expires = datetime.fromisoformat(receipt_claims["expires_at"])
        if (confirmed_at.utcoffset() is None or receipt_expires.utcoffset() is None
                or issued_at < confirmed_at or issued_at >= receipt_expires):
            raise ValueError("Capability issuance must be inside the signed approval validity")
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ExecutionCapabilityError("Authenticated lifecycle approval required for execution capability.") from error

    return _sign_capability(
        capability_name=capability_name,
        arguments=arguments,
        run_id=trusted["run_id"],
        request_id=binding["request_id"],
        action=action,
        now=issued_at,
        expires_at=receipt_expires,
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
        action=action,
        now=now,
    )


def _sign_capability(
    *,
    capability_name: str,
    arguments: dict[str, Any],
    run_id: str,
    request_id: str,
    action: str,
    now: datetime | None,
    expires_at: datetime | None = None,
) -> str:
    if any(not isinstance(value, str) or not value.strip()
           for value in (capability_name, run_id, request_id, action)):
        raise ExecutionCapabilityError("A capability exige ação, operação, run e request válidos.")
    if not isinstance(arguments, dict):
        raise ExecutionCapabilityError("Os argumentos da capability devem ser um objeto JSON.")
    try:
        payload_digest = _payload_digest(arguments)
    except (TypeError, ValueError, RecursionError) as error:
        raise ExecutionCapabilityError("Os argumentos da capability devem ser JSON válido.") from error

    issued_at = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(issued_at, datetime) or issued_at.utcoffset() is None:
        raise ExecutionCapabilityError("A emissão da capability exige uma data com fuso.")
    expiration = issued_at + timedelta(minutes=5)
    if expires_at is not None:
        expiration = min(expiration, expires_at)
    if expiration <= issued_at:
        raise ExecutionCapabilityError("A capability exige um prazo de validade positivo.")
    payload = {
        "capability": capability_name,
        "payload_digest": payload_digest,
        "run_id": run_id,
        "request_id": request_id,
        "nonce": uuid4().hex,
        "issued_at": issued_at.isoformat(),
        "expires_at": expiration.isoformat(),
    }
    payload.update({"version": 1, "action": action})
    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    if len(encoded_payload) + 65 > 16384:
        raise ExecutionCapabilityError("A capability de execução excede o tamanho permitido.")
    token = f"{encoded_payload}.{LOCAL_CAPABILITY_SUFFIX}"
    _register_local_capability(token, payload["nonce"], expiration)
    return token


def _register_local_capability(token: str, nonce: str, expires_at: datetime) -> None:
    configured_path = os.environ.get(EXECUTION_CAPABILITY_LEDGER_ENV_VAR)
    if configured_path is not None and (not configured_path.strip() or configured_path == ":memory:"):
        raise ExecutionCapabilityError("O ledger de capabilities deve ser persistente.")
    path = Path(configured_path) if configured_path is not None else DEFAULT_EXECUTION_CAPABILITY_LEDGER
    try:
        _prepare_capability_ledger(path)
        with closing(sqlite3.connect(str(path), timeout=5)) as connection:
            connection.execute("PRAGMA synchronous = FULL")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS local_execution_capabilities "
                    "(token_digest TEXT PRIMARY KEY, nonce_digest TEXT UNIQUE NOT NULL, expires_at REAL NOT NULL)"
                )
                connection.execute("DELETE FROM local_execution_capabilities WHERE expires_at <= ?",
                                   (datetime.now(timezone.utc).timestamp(),))
                connection.execute(
                    "INSERT INTO local_execution_capabilities VALUES (?, ?, ?)",
                    (hashlib.sha256(token.encode("ascii")).hexdigest(),
                     hashlib.sha256(nonce.encode("ascii")).hexdigest(), expires_at.timestamp()),
                )
    except (OSError, sqlite3.Error, ValueError) as error:
        raise ExecutionCapabilityError("Não foi possível registrar a capability local com segurança.") from error


def _prepare_capability_ledger(path: Path, *, create: bool = True) -> None:
    parent = path.absolute().parent
    owner = os.getuid()
    for directory in (*reversed(parent.parents), parent):
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        metadata = directory.lstat()
        trusted_sticky = directory != parent and metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in (0, owner)
                or (directory == parent and metadata.st_uid != owner)
                or (metadata.st_mode & 0o022 and not trusted_sticky)):
            raise ValueError("Capability ledger requires owner-controlled local directories")
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            metadata = path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        _validate_capability_ledger_file(metadata)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            raise
    else:
        _validate_capability_ledger_file(metadata)
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    handle = os.open(path, flags, 0o600)
    try:
        _validate_capability_ledger_file(os.fstat(handle))
        os.fchmod(handle, 0o600)
    finally:
        os.close(handle)


def _validate_capability_ledger_file(metadata: os.stat_result) -> None:
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022 or metadata.st_nlink != 1):
        raise ValueError("Capability ledger requires an owner-controlled local file")


def _payload_digest(arguments: dict[str, Any]) -> str:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()