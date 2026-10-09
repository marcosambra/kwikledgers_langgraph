from __future__ import annotations

import math
import re
from datetime import datetime, time, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_CUTOFF_TIME = "18:00"
CONTINUATION_SUFFIX = " (continuação)"
# Entradas geradas por automação não contam como atividade humana do dia.
IGNORED_ACTIVITY_SOURCE_PREFIXES = ("task-scheduler", "scheduler", "day-end-closure")

_ENTRY_HEADER = re.compile(r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z - (.*)$")


def parse_daily_log_entries(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        header = _ENTRY_HEADER.match(line)
        if header:
            timestamp = datetime.strptime(header.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            current = {"timestamp": timestamp.isoformat(), "title": header.group(2), "source": "", "details": ""}
            entries.append(current)
        elif current is not None and not current["source"] and line.startswith("Fonte:"):
            current["source"] = line[len("Fonte:"):].strip()
        elif current is not None:
            current["details"] += line + "\n"
    return entries


def build_day_end_preview(
    request: dict[str, Any],
    context: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    zone = _resolve_timezone(request.get("timezone"))
    current = (_parse_timestamp(request.get("now")) or now or datetime.now(timezone.utc)).astimezone(zone)
    cutoff_time = str(request.get("cutoff_time") or DEFAULT_CUTOFF_TIME)
    cutoff = datetime.combine(current.date(), _parse_cutoff(cutoff_time), tzinfo=zone)
    window_end = current if request.get("trigger") == "completed" else min(current, cutoff)

    preview: dict[str, Any] = {
        "trigger": request.get("trigger", "manual"),
        "cutoff_time": cutoff_time,
        "window_end": window_end.isoformat(),
        "dry_run": bool(request.get("dry_run", False)),
        "status": "skipped",
        "skip_reason": None,
        "task": None,
        "continuation": None,
    }

    if request.get("trigger") == "scheduled" and current < cutoff:
        return {**preview, "skip_reason": "before_cutoff"}

    activity = _human_activity_timestamps(context.get("activity_entries", []), current.date(), zone)
    if not activity:
        return {**preview, "skip_reason": "no_activity"}

    tasks = list(context.get("active_tasks", []))
    if request.get("task_id"):
        tasks = [task for task in tasks if int(task["id"]) == int(request["task_id"])]
    if not tasks:
        return {**preview, "skip_reason": "no_active_task"}
    if len(tasks) > 1:
        return {**preview, "skip_reason": "ambiguous_active_tasks", "task_ids": [task.get("id") for task in tasks]}

    task = tasks[0]
    task_pattern = re.compile(rf"(?:\bKL-|\btask\s+|\bchild\s+){int(task['id'])}\b", re.IGNORECASE)
    task_entries = [
        entry for entry in context.get("activity_entries", [])
        if entry.get("source") != "task-completed"
        and task_pattern.search(str(entry.get("title", "")) + "\n" + str(entry.get("details", "")))
    ]
    activity = _human_activity_timestamps(task_entries, current.date(), zone)
    if not activity:
        return {**preview, "skip_reason": "no_task_activity"}
    activated_at = _parse_timestamp(task.get("activated_at"))
    if activated_at and not any(moment >= activated_at.astimezone(zone) for moment in activity):
        return {**preview, "skip_reason": "no_task_activity"}
    window_start = max(activity[0], activated_at.astimezone(zone)) if activated_at else activity[0]
    elapsed_seconds = (window_end - window_start).total_seconds()
    if elapsed_seconds <= 0:
        return {**preview, "skip_reason": "no_elapsed_time", "window_start": window_start.isoformat()}

    computed_hours = math.ceil(elapsed_seconds / 3600)
    previous_hours = float(task.get("completed_work_hours") or 0)
    if not math.isfinite(previous_hours) or previous_hours < 0:
        raise ValueError("CompletedWork deve ser finito e nao negativo.")
    continuation = None
    if request.get("trigger") != "completed" and request.get("create_continuation", True):
        if not task.get("parent_id"):
            return {**preview, "skip_reason": "missing_parent"}
        continuation = {
            "title": _continuation_title(str(task.get("title") or "")),
            "parent_id": task.get("parent_id"),
            "area_path": task.get("area_path"),
            "iteration_path": task.get("iteration_path"),
            "assigned_to": task.get("assigned_to"),
        }

    return {
        **preview,
        "status": "ready",
        "task": {"id": int(task["id"]), "title": task.get("title")},
        "expected_revision": task.get("revision"),
        "assigned_to": task.get("assigned_to"),
        "window_start": window_start.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "computed_hours": computed_hours,
        "completed_work_hours": previous_hours + computed_hours,
        "remaining_work_hours": None,
        "target_status": "Closed",
        "continuation": continuation,
        "comment": f"Fechamento automático de fim de dia ({preview['trigger']}): {computed_hours}h até {window_end:%H:%M}.",
    }


def _continuation_title(title: str) -> str:
    base = title.removesuffix(CONTINUATION_SUFFIX).rstrip()
    return base + CONTINUATION_SUFFIX


def _human_activity_timestamps(entries: list[dict[str, Any]], local_date: Any, zone: tzinfo) -> list[datetime]:
    timestamps: list[datetime] = []
    for entry in entries:
        source = str(entry.get("source") or "").strip().lower()
        title = str(entry.get("title") or "").casefold()
        if not source or source.startswith(IGNORED_ACTIVITY_SOURCE_PREFIXES) or "resumo" in title:
            continue
        moment = _parse_timestamp(entry.get("timestamp"))
        if moment is not None and moment.astimezone(zone).date() == local_date:
            timestamps.append(moment.astimezone(zone))
    return sorted(timestamps)


def _resolve_timezone(name: Any) -> tzinfo:
    if name:
        return ZoneInfo(str(name))
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_cutoff(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
