from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from kwikledgers_langgraph.state import AgentState


EXECUTION_CAPABILITY_KEY_ENV_VAR = "KWIKLEDGERS_FASTMCP_EXECUTION_KEY"


class ExecutionCapabilityError(RuntimeError):
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

    issued_at = now or datetime.now(timezone.utc)
    payload = {
        "capability": capability_name,
        "payload_digest": _payload_digest(arguments),
        "run_id": observability.get("active_run_id"),
        "request_id": pending_approval.get("request_id"),
        "expires_at": (issued_at + timedelta(minutes=5)).isoformat(),
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    signature = hmac.new(signing_key.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"{encoded_payload}.{signature}"


def _payload_digest(arguments: dict[str, Any]) -> str:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()