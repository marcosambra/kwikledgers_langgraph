from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import SprintStoriesPorts
from kwikledgers_langgraph.guardrails import OperationKind, allow_without_approval
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_error, append_tracking_event


def build_sprint_stories_graph(ports: SprintStoriesPorts) -> Any:
    graph = StateGraph(AgentState)

    def fetch_sprint_stories(state: AgentState) -> dict[str, Any]:
        guardrail = allow_without_approval(
            state,
            action="sprint_stories.fetch_detailed",
            operation_kind=OperationKind.READ_ONLY,
        )
        started = record_runtime_event(
            state,
            node="fetch_sprint_stories",
            status="started",
            message="Buscando historias detalhadas do sprint atual.",
        )
        request = dict(state.get("sprint_stories_request", {}))
        try:
            result = ports.stories.fetch_sprint_stories_detailed(request)
        except Exception as error:
            error_updates = append_error(state, f"Falha ao buscar historias detalhadas do sprint: {error}")
            failed = record_runtime_event(
                state,
                node="fetch_sprint_stories",
                status="failed",
                message="Falha ao buscar historias detalhadas do sprint.",
                payload={"error": str(error)},
            )
            return {
                **guardrail,
                **started,
                **error_updates,
                **failed,
                "sprint_stories_result": {"status": "fetch_failed"},
            }

        event = append_tracking_event(
            state,
            category="sprint_stories",
            message="Historias detalhadas do sprint carregadas pelo runtime LangGraph.",
            payload={"counts": result.get("counts", {})},
        )
        completed = record_runtime_event(
            state,
            node="fetch_sprint_stories",
            status="completed",
            message="Historias detalhadas do sprint carregadas com sucesso.",
            payload={"counts": result.get("counts", {})},
        )
        return {**guardrail, **started, **event, **completed, "sprint_stories_result": result}

    graph.add_node("fetch_sprint_stories", fetch_sprint_stories)
    graph.add_edge(START, "fetch_sprint_stories")
    graph.add_edge("fetch_sprint_stories", END)

    return graph.compile()
