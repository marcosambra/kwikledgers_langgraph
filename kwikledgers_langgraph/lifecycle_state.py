from __future__ import annotations

from typing import Any

from kwikledgers_langgraph.state import AgentState


class LifecycleState(AgentState, total=False):
    request: dict[str, Any]
    context: dict[str, Any]
    results: dict[str, Any]
    run_id: str
    outcome: str
    outcome_reason: str
    direct_pr_url: str