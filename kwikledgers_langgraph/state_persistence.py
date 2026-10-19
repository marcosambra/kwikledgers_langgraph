from __future__ import annotations

import json
import argparse
from hashlib import sha256
import os
import tempfile
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
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_path.parent,
                                         delete=False) as state_file:
            temporary = Path(state_file.name)
            json.dump(state, state_file, indent=2, ensure_ascii=False, allow_nan=False)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def inspect_approval(state_file_path: str) -> dict[str, Any]:
    state = load_state_file(state_file_path)
    if state.get("errors"):
        raise StateFileError("O fluxo falhou. Consulte o resultado salvo antes de repetir a operação.")
    if state.get("observability", {}).get("backend_name") != "fastmcp":
        raise StateFileError("Backend de aprovação inválido.")
    pending = state.get("pending_approval", {})
    if pending:
        if not isinstance(pending, dict) or not isinstance(pending.get("preview"), dict):
            raise StateFileError("Prévia inválida.")
        digest = sha256(json.dumps(pending["preview"], sort_keys=True, allow_nan=False).encode()).hexdigest()
        if (pending.get("preview_digest") != digest
                or not state.get("observability", {}).get("active_run_id")
                or pending.get("run_id") != state["observability"]["active_run_id"]
                or any(not isinstance(pending.get(field), str) or not pending[field].strip()
                       for field in ("action", "request_id", "previewed_at"))
                or type(pending.get("approved")) is not bool
                or "receipt" in pending):
            raise StateFileError("A prévia não corresponde à execução atual. Gere uma nova prévia.")
    return {"version": 1, "pending": pending,
            "result": state.get("task_closure_result", state.get("pull_request_result", {}))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspecionar uma prévia local sem executar o workflow.")
    parser.add_argument("--inspect", required=True, metavar="STATE_FILE")
    args = parser.parse_args(argv)
    print(json.dumps(inspect_approval(args.inspect), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())