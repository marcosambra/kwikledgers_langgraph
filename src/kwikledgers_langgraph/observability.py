from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kwikledgers_langgraph.state import AgentState, merge_observability


_SENSITIVE_PAYLOAD_KEY_PARTS = (
    "authorization",
    "body",
    "description",
    "email",
    "html",
    "markdown",
    "message",
    "password",
    "pat",
    "secret",
    "token",
    "uri",
    "url",
)
_MAX_EXTERNAL_STRING_LENGTH = 240
_MAX_EXTERNAL_COLLECTION_ITEMS = 20


def build_observability_context(*, flow_name: str, source: str, backend_name: str | None = None) -> dict[str, Any]:
    correlation_id = str(uuid.uuid4())
    active_run_id = str(uuid.uuid4())
    context = {
        "correlation_id": correlation_id,
        "active_run_id": active_run_id,
        "flow_name": flow_name,
        "source": source,
        "decisions": [],
        "timeline": [
            {
                "node": flow_name,
                "status": "initialized",
                "message": "Fluxo preparado para execução.",
                "payload": {"source": source},
            }
        ],
    }
    if backend_name:
        context["backend_name"] = backend_name
        context["timeline"][0]["payload"]["backend_name"] = backend_name
    return context


def _sanitize_external_text(value: Any) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= _MAX_EXTERNAL_STRING_LENGTH:
        return text
    return text[: _MAX_EXTERNAL_STRING_LENGTH - 3] + "..."


def _is_sensitive_payload_key(key: str) -> bool:
    normalized = key.strip().lower()
    return any(part in normalized for part in _SENSITIVE_PAYLOAD_KEY_PARTS)


def sanitize_external_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_external_text(value)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for index, (key, nested_value) in enumerate(value.items()):
            if index >= _MAX_EXTERNAL_COLLECTION_ITEMS:
                sanitized["truncated_items"] = len(value) - _MAX_EXTERNAL_COLLECTION_ITEMS
                break
            normalized_key = str(key)
            if _is_sensitive_payload_key(normalized_key):
                sanitized[normalized_key] = "[redacted]"
                continue
            sanitized[normalized_key] = sanitize_external_payload(nested_value, depth=depth + 1)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        sanitized_items = [
            sanitize_external_payload(item, depth=depth + 1)
            for item in items[:_MAX_EXTERNAL_COLLECTION_ITEMS]
        ]
        if len(items) > _MAX_EXTERNAL_COLLECTION_ITEMS:
            sanitized_items.append({"truncated_items": len(items) - _MAX_EXTERNAL_COLLECTION_ITEMS})
        return sanitized_items
    return _sanitize_external_text(value)


def record_runtime_event(
    state: AgentState,
    *,
    node: str,
    status: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observability = dict(state.get("observability", {}))
    decisions = list(observability.get("decisions", []))
    timeline = list(observability.get("timeline", []))
    timeline.append(
        {
            "node": node,
            "status": status,
            "message": message,
            "payload": payload or {},
        }
    )
    decisions.append(f"runtime.{node}.{status}")
    observability["decisions"] = decisions
    observability["timeline"] = timeline
    return merge_observability(state, observability)


def build_runtime_usage_metrics_entry(state: AgentState) -> dict[str, Any]:
    observability = dict(state.get("observability", {}))
    timeline = list(observability.get("timeline", []))
    decisions = list(observability.get("decisions", []))
    tracking_events = list(state.get("tracking_events", []))
    errors = list(state.get("errors", []))

    final_event = dict(timeline[-1]) if timeline else {}
    flow_name = str(observability.get("flow_name") or "unknown_flow")
    final_status = str(final_event.get("status") or "completed")
    title = f"runtime:{flow_name}:{final_status}"
    summary = _sanitize_external_text(
        final_event.get("message") or f"Fluxo {flow_name} finalizado com status {final_status}."
    )

    actions = [
        f"source={state.get('source') or observability.get('source') or 'unknown'}",
        f"backend={observability.get('backend_name') or 'unknown'}",
        f"run_id={observability.get('active_run_id') or 'unknown'}",
        f"correlation_id={observability.get('correlation_id') or 'unknown'}",
    ]
    for event in timeline[-5:]:
        actions.append(f"node={event.get('node')} status={event.get('status')}")
    for decision in decisions[-5:]:
        actions.append(f"decision={decision}")
    for tracking_event in tracking_events[-3:]:
        actions.append(
            f"tracking={tracking_event.get('category')}:{_sanitize_external_text(tracking_event.get('message') or '')}"
        )
    for error in errors[:3]:
        actions.append(f"error={_sanitize_external_text(error)}")

    return {
        "title": title,
        "summary": summary,
        "remaining_story_points": 0,
        "blocked_items": len(errors),
        "actions": actions,
    }


def build_runtime_event_stream_records(state: AgentState) -> list[dict[str, Any]]:
    observability = dict(state.get("observability", {}))
    timeline = list(observability.get("timeline", []))
    decisions = list(observability.get("decisions", []))
    tracking_events = list(state.get("tracking_events", []))
    errors = list(state.get("errors", []))
    summary = dict(state.get("summary", {}))
    counts = dict(summary.get("counts", {}))
    final_event = dict(timeline[-1]) if timeline else {}

    common = {
        "runtime": "kwikledgers_langgraph",
        "flow_name": str(observability.get("flow_name") or "unknown_flow"),
        "source": str(state.get("source") or observability.get("source") or "unknown"),
        "backend_name": str(observability.get("backend_name") or "unknown"),
        "correlation_id": str(observability.get("correlation_id") or "unknown"),
        "active_run_id": str(observability.get("active_run_id") or "unknown"),
    }

    records: list[dict[str, Any]] = [
        {
            **common,
            "record_type": "run_summary",
            "final_node": final_event.get("node"),
            "final_status": final_event.get("status") or "completed",
            "message": final_event.get("message") or "",
            "counts": counts,
            "timeline_event_count": len(timeline),
            "decision_count": len(decisions),
            "tracking_event_count": len(tracking_events),
            "error_count": len(errors),
        }
    ]

    for event_index, event in enumerate(timeline):
        records.append(
            {
                **common,
                "record_type": "timeline_event",
                "event_index": event_index,
                "node": event.get("node"),
                "status": event.get("status"),
                "message": _sanitize_external_text(event.get("message") or ""),
                "payload": sanitize_external_payload(dict(event.get("payload", {}))),
            }
        )

    for decision_index, decision in enumerate(decisions):
        records.append(
            {
                **common,
                "record_type": "decision",
                "decision_index": decision_index,
                "decision": decision,
            }
        )

    for tracking_index, tracking_event in enumerate(tracking_events):
        records.append(
            {
                **common,
                "record_type": "tracking_event",
                "tracking_index": tracking_index,
                "category": tracking_event.get("category"),
                "message": _sanitize_external_text(tracking_event.get("message") or ""),
                "payload": sanitize_external_payload(dict(tracking_event.get("payload", {}))),
            }
        )

    for error_index, error in enumerate(errors):
        records.append(
            {
                **common,
                "record_type": "error",
                "error_index": error_index,
                "message": _sanitize_external_text(error),
            }
        )

    return records


def resolve_ai_tracking_root(explicit_root: str | Path | None = None) -> Path:
    if explicit_root is not None:
        candidate = Path(explicit_root).expanduser().resolve()
        if candidate.is_dir():
            return candidate
        raise RuntimeError("O tracking root informado para observabilidade nao existe.")

    explicit_env_root = os.environ.get("KWIKLEDGERS_AI_TRACKING_ROOT")
    if explicit_env_root:
        candidate = Path(explicit_env_root).expanduser().resolve()
        if candidate.is_dir():
            return candidate
        raise RuntimeError("A variavel KWIKLEDGERS_AI_TRACKING_ROOT nao aponta para um diretorio valido.")

    explicit_legacy_root = os.environ.get("KWIKLEDGERS_LEGACY_ROOT")
    if explicit_legacy_root:
        candidate = Path(explicit_legacy_root).expanduser().resolve() / "AI_Tracking"
        if candidate.is_dir():
            return candidate

    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "AI_Tracking"
        if candidate.is_dir():
            return candidate

    raise RuntimeError("Nao foi possivel localizar o diretorio AI_Tracking para observabilidade externa.")


def append_runtime_usage_metrics(state: AgentState, *, tracking_root: str | Path | None = None) -> Path:
    root = resolve_ai_tracking_root(tracking_root)
    metrics_dir = root / "Metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = metrics_dir / "ai-usage-log.md"
    if not metrics_path.exists():
        metrics_path.write_text(
            "# AI Usage Metrics Log\n\n"
            "Registro operacional das interacoes com IA relacionadas ao sprint atual.\n\n",
            encoding="utf-8",
        )

    entry = build_runtime_usage_metrics_entry(state)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        f"## {timestamp} - {entry['title']}",
        "",
        f"Resumo: {entry['summary']}",
        f"Story points restantes: {entry['remaining_story_points']}",
        f"Itens bloqueados: {entry['blocked_items']}",
        "Acoes executadas:",
    ]
    for action in entry["actions"]:
        lines.append(f"- {action}")
    lines.append("")

    with metrics_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    return metrics_path


def append_runtime_event_stream(state: AgentState, *, tracking_root: str | Path | None = None) -> Path:
    root = resolve_ai_tracking_root(tracking_root)
    metrics_dir = root / "Metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    events_path = metrics_dir / "langgraph-runtime-events.jsonl"
    emitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with events_path.open("a", encoding="utf-8") as handle:
        for record in build_runtime_event_stream_records(state):
            handle.write(json.dumps({"emitted_at": emitted_at, **record}, ensure_ascii=False) + "\n")

    return events_path


def append_runtime_observability_artifacts(
    state: AgentState,
    *,
    tracking_root: str | Path | None = None,
) -> dict[str, Path]:
    usage_metrics_path = append_runtime_usage_metrics(state, tracking_root=tracking_root)
    event_stream_path = append_runtime_event_stream(state, tracking_root=tracking_root)
    return {
        "usage_metrics_path": usage_metrics_path,
        "event_stream_path": event_stream_path,
    }