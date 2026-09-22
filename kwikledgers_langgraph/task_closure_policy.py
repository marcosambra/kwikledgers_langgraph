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
        "closure_allowed": closure_allowed,
        "validation_messages": [] if closure_allowed else ["O horario de fechamento nao pode ser anterior ao horario de inicio."],
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)