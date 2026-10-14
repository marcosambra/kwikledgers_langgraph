from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from kwikledgers_langgraph.state import AgentState


STATE_SIGNING_KEY_ENV_VAR = "KWIKLEDGERS_LANGGRAPH_STATE_SIGNING_KEY"
STATE_SIGNATURE_ALGORITHM = "hmac-sha256"
MUTABLE_STATE_KEYS = (
    "pull_request_request",
    "branch_association_request",
    "story_status_request",
    "task_closure_request",
)


class StateFileError(RuntimeError):
    """Raised when a saved runtime state file cannot be trusted or parsed."""


def _get_state_signing_key() -> str | None:
    return None


def _canonical_state_json(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _build_state_signature(state: dict[str, Any], *, signing_key: str) -> str:
    raise StateFileError("Assinaturas foram removidas. Gere uma nova prévia.")


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

    if any(field in payload for field in ("state", "signature", "signature_algorithm")):
        raise StateFileError("Estados assinados antigos não são aceitos. Gere uma nova prévia.")

    return cast(AgentState, payload)


def write_state_file(state: dict[str, Any], state_file_path: str) -> None:
    output_path = Path(state_file_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2, ensure_ascii=False, allow_nan=False)
        state_file.write("\n")