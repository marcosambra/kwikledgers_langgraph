from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def build_task_closure_preview(request: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    task_id = int(request["task_id"])
    start_time = _parse_timestamp(request["start_time"])
    close_time = _parse_timestamp(request.get("close_time")) or now or datetime.now(timezone.utc)
    elapsed_seconds = (close_time - start_time).total_seconds()
    closure_allowed = elapsed_seconds >= 0
    computed_hours = math.ceil(elapsed_seconds / 3600) if closure_allowed else 0
    completed_work_hours = request.get("completed_work_hours")
    messages = [] if closure_allowed else ["O horario de fechamento nao pode ser anterior ao horario de inicio."]
    if str(request.get("work_kind") or "").strip().lower() == "fix":
        origin = request.get("origin_task_id")
        if origin is not None and int(origin) == task_id:
            messages.append("Correcao de problema exige task propria; nao lance as horas na task de origem.")
        if not str(request.get("fix_summary") or "").strip():
            messages.append("Correcao de problema exige fix_summary descrevendo o problema corrigido.")

    return {
        "task_id": task_id,
        "start_time": start_time.isoformat(),
        "close_time": close_time.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "computed_hours": computed_hours,
        "completed_work_hours": float(completed_work_hours) if completed_work_hours is not None else float(computed_hours),
        "remaining_work_hours": request.get("remaining_work_hours"),
        "target_status": "Closed",
        "comment": request.get("comment"),
        "closure_allowed": not messages,
        "validation_messages": messages,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def build_verified_task_closure_preview(request: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    preview = build_task_closure_preview(request, now=now)
    messages = list(preview["validation_messages"])
    kind = request.get("work_kind") or "implementation"
    if kind not in ("implementation", "fix"):
        messages.append("work_kind deve ser implementation ou fix.")
    if kind == "fix":
        origin = request.get("origin_task_id")
        if type(origin) is not int or origin <= 0 or origin == preview["task_id"]:
            messages.append("Correção exige origin_task_id positivo e uma Task própria diferente da origem.")
        if not isinstance(request.get("fix_summary"), str) or not request["fix_summary"].strip():
            messages.append("Correção exige fix_summary descrevendo o problema corrigido.")
    hours = request.get("completed_work_hours")
    if hours is not None and (
        type(hours) not in (int, float) or not math.isfinite(hours) or hours != preview["computed_hours"]
    ):
        messages.append("completed_work_hours deve corresponder ao ceil do intervalo registrado.")
    return {**preview, "completed_work_hours": float(preview["computed_hours"]),
            "work_kind": kind, "fix_summary": request.get("fix_summary"),
            "origin_task_id": request.get("origin_task_id"),
            "closure_allowed": not messages, "validation_messages": messages}