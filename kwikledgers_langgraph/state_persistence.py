from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, cast

from kwikledgers_langgraph.state import AgentState


STATE_SIGNING_KEY_ENV_VAR = "KWIKLEDGERS_LANGGRAPH_STATE_SIGNING_KEY"
LOCAL_SIMULATION_ENV_VAR = "KWIKLEDGERS_LANGGRAPH_ALLOW_LOCAL_SIMULATION"
STATE_SIGNATURE_ALGORITHM = "hmac-sha256"
MUTABLE_STATE_KEYS = (
    "pull_request_request",
    "branch_association_request",
    "story_status_request",
    "task_closure_request",
)


class StateFileError(RuntimeError):
    """Raised when a saved runtime state file cannot be trusted or parsed."""


def assert_local_simulation_enabled(requested: bool) -> None:
    if not requested:
        return
    if os.environ.get(LOCAL_SIMULATION_ENV_VAR, "").strip() == "1":
        return
    raise StateFileError(
        "A simulacao local de aprovacao e restrita a testes. "
        f"Defina {LOCAL_SIMULATION_ENV_VAR}=1 somente em ambiente de teste."
    )


def _get_state_signing_key() -> str | None:
    raw_value = os.environ.get(STATE_SIGNING_KEY_ENV_VAR)
    if raw_value is None:
        return None
    normalized = raw_value.strip()
    return normalized or None


def _canonical_state_json(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _build_state_signature(state: dict[str, Any], *, signing_key: str) -> str:
    payload = _canonical_state_json(state).encode("utf-8")
    return hmac.new(signing_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _requires_signature(state: dict[str, Any]) -> bool:
    return bool(state.get("pending_approval")) or any(state.get(key) for key in MUTABLE_STATE_KEYS)


def load_state_file(state_file_path: str) -> AgentState:
    try:
        with Path(state_file_path).open("r", encoding="utf-8") as state_file:
            payload = json.load(state_file)
    except FileNotFoundError as error:
        raise StateFileError(f"O arquivo de estado '{state_file_path}' nao foi encontrado.") from error
    except json.JSONDecodeError as error:
        raise StateFileError(f"O arquivo de estado '{state_file_path}' nao contem JSON valido.") from error

    if not isinstance(payload, dict):
        raise StateFileError("O arquivo de estado precisa conter um objeto JSON no nivel raiz.")

    signing_key = _get_state_signing_key()
    if "state" in payload or "signature" in payload:
        state_payload = payload.get("state")
        signature = payload.get("signature")
        algorithm = payload.get("signature_algorithm")

        if not isinstance(state_payload, dict):
            raise StateFileError("O envelope assinado do arquivo de estado nao contem um objeto valido em 'state'.")
        if algorithm != STATE_SIGNATURE_ALGORITHM:
            raise StateFileError("O arquivo de estado assinado usa um algoritmo de assinatura nao suportado.")
        if not isinstance(signature, str) or not signature.strip():
            raise StateFileError("O arquivo de estado assinado nao contem uma assinatura valida.")
        if not signing_key:
            raise StateFileError(
                "O arquivo de estado esta assinado. Defina KWIKLEDGERS_LANGGRAPH_STATE_SIGNING_KEY para validar a integridade antes da retomada."
            )

        expected_signature = _build_state_signature(state_payload, signing_key=signing_key)
        if not hmac.compare_digest(signature, expected_signature):
            raise StateFileError(
                "A assinatura do arquivo de estado nao confere; o arquivo pode ter sido alterado ou a chave configurada nao corresponde."
            )
        return cast(AgentState, state_payload)

    if signing_key:
        raise StateFileError(
            "KWIKLEDGERS_LANGGRAPH_STATE_SIGNING_KEY esta configurada, mas o arquivo de estado nao esta assinado. Gere um novo preview antes de retomar a mutacao."
        )

    if _requires_signature(payload):
        raise StateFileError(
            "O arquivo de estado de um fluxo mutavel precisa estar assinado. "
            "Defina KWIKLEDGERS_LANGGRAPH_STATE_SIGNING_KEY e gere um novo preview antes de retomar."
        )

    return cast(AgentState, payload)


def write_state_file(state: dict[str, Any], state_file_path: str) -> None:
    output_path = Path(state_file_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    signing_key = _get_state_signing_key()

    if _requires_signature(state) and not signing_key:
        raise StateFileError(
            "Defina KWIKLEDGERS_LANGGRAPH_STATE_SIGNING_KEY para salvar estado de fluxo mutavel."
        )

    payload: dict[str, Any]
    if signing_key:
        payload = {
            "state": state,
            "signature": _build_state_signature(state, signing_key=signing_key),
            "signature_algorithm": STATE_SIGNATURE_ALGORITHM,
        }
    else:
        payload = state

    with output_path.open("w", encoding="utf-8") as state_file:
        json.dump(payload, state_file, indent=2, ensure_ascii=False)
        state_file.write("\n")