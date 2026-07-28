from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kwikledgers_langgraph.state import AgentState, append_decision, merge_observability


def build_observability_context(*, flow_name: str, source: str) -> dict[str, Any]:
    correlation_id = str(uuid.uuid4())
    active_run_id = str(uuid.uuid4())
    return {
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


def record_runtime_event(
    state: AgentState,
    *,
    node: str,
    status: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observability = dict(state.get("observability", {}))
    timeline = list(observability.get("timeline", []))
    timeline.append(
        {
            "node": node,
            "status": status,
            "message": message,
            "payload": payload or {},
        }
    )
    observability["timeline"] = timeline
    decision = append_decision(state, f"runtime.{node}.{status}")
    return {**decision, **merge_observability(state, observability)}


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
    summary = str(final_event.get("message") or f"Fluxo {flow_name} finalizado com status {final_status}.")

    actions = [
        f"source={state.get('source') or observability.get('source') or 'unknown'}",
        f"run_id={observability.get('active_run_id') or 'unknown'}",
        f"correlation_id={observability.get('correlation_id') or 'unknown'}",
    ]
    for event in timeline[-5:]:
        actions.append(f"node={event.get('node')} status={event.get('status')}")
    for decision in decisions[-5:]:
        actions.append(f"decision={decision}")
    for tracking_event in tracking_events[-3:]:
        actions.append(
            f"tracking={tracking_event.get('category')}:{tracking_event.get('message')}"
        )
    for error in errors[:3]:
        actions.append(f"error={error}")

    return {
        "title": title,
        "summary": summary,
        "remaining_story_points": 0,
        "blocked_items": len(errors),
        "actions": actions,
    }


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