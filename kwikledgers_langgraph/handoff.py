from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from kwikledgers_fastmcp.domains.local_tracking.handoffs import HandoffStore
from kwikledgers_fastmcp.domains.local_tracking.request_metrics import RequestMetricsService, _identifier, _text

_MARKER = re.compile(r"\[kwikledgers-handoff:([A-Za-z0-9_-]{1,4096})\]")
_TRANSITIONS = {
    "pending": {"accepted", "failed"},
    "accepted": {"running", "blocked", "failed"},
    "running": {"blocked", "failed", "completed"},
    "blocked": {"running", "failed"},
    "failed": set(),
    "completed": set(),
}


def create_handoff(tracking_root: Path, *, story_id: int, task_ids: list[int], sprint_path: str = "") -> dict[str, Any]:
    if type(story_id) is not int or story_id <= 0 or any(type(task) is not int or task <= 0 for task in task_ids):
        raise ValueError("História e tasks devem ter IDs inteiros positivos.")
    handoff_id = str(uuid4())
    store = HandoffStore(tracking_root)
    record = store.create({
        "handoff_id": handoff_id, "request_id": handoff_id,
        "story_id": story_id, "task_ids": sorted(set(task_ids)), "sprint_path": sprint_path,
        "mode": "local-only", "status": "pending", "session_id": "",
        "repository": None, "branch": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    token = base64.urlsafe_b64encode(json.dumps({
        "handoff_id": handoff_id, "tracking_root": str(store.tracking.tracking_dir),
    }).encode()).decode().rstrip("=")
    return {
        "schema_version": 1, "handoff_id": handoff_id,
        "marker": f"[kwikledgers-handoff:{token}]",
        "path": str(store.path(handoff_id)), "status": record["status"],
    }


def parse_handoff_marker(prompt: Any, *, workspace_root: Path, default_tracking_root: Path) -> tuple[Path, str] | None:
    if not isinstance(prompt, str):
        return None
    matches = _MARKER.findall(prompt)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("A solicitação deve conter exatamente um handoff.")
    token = matches[0]
    descriptor = json.loads(base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True))
    if not isinstance(descriptor, dict) or set(descriptor) != {"handoff_id", "tracking_root"}:
        raise ValueError("Marcador de handoff inválido.")
    root = Path(descriptor["tracking_root"]).resolve()
    if root != default_tracking_root.resolve() and not root.is_relative_to(workspace_root.resolve()):
        raise ValueError("A raiz do handoff está fora do workspace autorizado.")
    store = HandoffStore(root)
    record = store.read(descriptor["handoff_id"])
    if record is None or record.get("mode") != "local-only" or record.get("status") != "pending":
        raise ValueError("O handoff não está disponível para aceite.")
    return root, record["handoff_id"]


def transition_handoff(
    tracking_root: Path, handoff_id: str, status: str, *, session_id: str = "",
    evidence: str = "", tests_passed: bool = False,
) -> dict[str, Any]:
    _identifier(session_id, "session_id", optional=status == "failed")
    if status not in _TRANSITIONS:
        raise ValueError("Estado de handoff inválido.")
    safe_evidence = _text(evidence, 160)

    def transform(record: dict[str, Any]) -> dict[str, Any]:
        if record.get("session_id") and record["session_id"] != session_id:
            raise ValueError("O handoff pertence a outra sessão.")
        if record["status"] == status:
            return record
        if status not in _TRANSITIONS[record["status"]]:
            raise ValueError("Transição de handoff não permitida.")
        if status == "completed" and (not tests_passed or not safe_evidence.strip()):
            raise ValueError("Conclusão exige testes aprovados e evidência explícita.")
        return {
            **record, "status": status, "session_id": session_id,
            "evidence": safe_evidence, "tests_passed": tests_passed,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    record, changed = HandoffStore(tracking_root).update(handoff_id, transform)
    if changed:
        labels = {"accepted": "Aceite confirmado", "running": "Em execução", "blocked": "Bloqueada",
                  "failed": "Falhou", "completed": "Conclusão informada"}
        metrics = RequestMetricsService(tracking_root=tracking_root)
        if not session_id:
            metrics.append_event(
                request_id=record["request_id"], source="langgraph", kind="request_started",
                label="Envio de continuação local", status="running",
            )
        metrics.append_event(
            request_id=record["request_id"], session_id=session_id, source="langgraph", kind="step",
            label=f"Continuação: {labels[status]}",
            status="completed" if status in {"accepted", "completed"} else "running" if status == "running" else status,
            details={"operation": "handoff", "outcome": status, "evidence": safe_evidence},
        )
        if not session_id:
            metrics.append_event(
                request_id=record["request_id"], source="langgraph", kind="request_finished",
                label="Envio de continuação falhou", status="failed",
            )
    return record
