from __future__ import annotations

from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import ProcessSnapshotPorts
from kwikledgers_langgraph.graphs.story_lifecycle import aware_time
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.process_policy import analyze_snapshot, coordinate_deadlines
from kwikledgers_langgraph.state import AgentState, append_error, append_tracking_event


def build_process_snapshot_graph(ports: ProcessSnapshotPorts) -> Any:
    graph = StateGraph(AgentState)

    def analyze(state: AgentState) -> dict[str, Any]:
        try:
            snapshot = ports.provider.fetch_snapshot()
            summary = analyze_snapshot(snapshot["items"])
            summary["deadline_reminders"] = coordinate_deadlines(snapshot, aware_time(ports.clock.now()))
            event = {"action": "process_snapshot.analyzed", "blocked_count": len(summary["blocked_ids"]),
                     "returned_count": len(summary["returned_ids"]), "task_count": len(summary["task_ids"])}
            ports.tracking.record(event, key=f"process_snapshot:{uuid4()}")
            return {**append_tracking_event(state, category="lifecycle", message="process_snapshot.analyzed", payload=event),
                    **record_runtime_event(state, node="process_snapshot", status="completed",
                                           message="process_snapshot.analyzed", payload=event),
                    "summary": summary, "errors": []}
        except Exception as error:
            return {**append_error(state, type(error).__name__), "summary": {}}

    graph.add_node("analyze", analyze)
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", END)
    return graph.compile(name="process_snapshot")