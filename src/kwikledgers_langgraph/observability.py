from __future__ import annotations

import uuid
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