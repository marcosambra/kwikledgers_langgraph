from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import DailySummaryPorts
from kwikledgers_langgraph.guardrails import OperationKind, allow_without_approval
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_tracking_event


def build_daily_summary_graph(ports: DailySummaryPorts) -> Any:
    graph = StateGraph(AgentState)

    def load_identity_context(state: AgentState) -> dict[str, Any]:
        updates = record_runtime_event(
            state,
            node="load_identity_context",
            status="started",
            message="Iniciando resolução de identidade do estado.",
        )
        identity = state.get("user_identity", {})
        if identity:
            completed = record_runtime_event(
                state,
                node="load_identity_context",
                status="completed",
                message="Identidade já estava disponível no estado.",
            )
            return {**updates, **completed}

        if ports.identity is not None:
            resolved_identity = ports.identity.resolve_current_user_identity()
            if resolved_identity:
                event = append_tracking_event(
                    state,
                    category="runtime",
                    message="Identidade resolvida pelo adapter configurado.",
                    payload={
                        "has_resolved_email": bool(resolved_identity.get("resolved_email")),
                        "has_display_name": bool(resolved_identity.get("display_name")),
                    },
                )
                completed = record_runtime_event(
                    state,
                    node="load_identity_context",
                    status="completed",
                    message="Identidade resolvida com sucesso.",
                    payload={"identity_resolved": True},
                )
                return {
                    **updates,
                    **event,
                    **completed,
                    "user_identity": resolved_identity,
                }

        event = append_tracking_event(
            state,
            category="runtime",
            message="Fluxo iniciado sem identidade resolvida no estado.",
        )
        completed = record_runtime_event(
            state,
            node="load_identity_context",
            status="completed",
            message="Fluxo seguiu sem identidade resolvida.",
        )
        return {**updates, **event, **completed}

    def fetch_summary(state: AgentState) -> dict[str, Any]:
        guardrail = allow_without_approval(
            state,
            action="daily_summary.fetch_summary",
            operation_kind=OperationKind.READ_ONLY,
        )
        started = record_runtime_event(
            state,
            node="fetch_summary",
            status="started",
            message="Executando provider de resumo diário.",
        )
        summary = ports.provider.fetch_daily_summary()
        event = append_tracking_event(
            state,
            category="daily_summary",
            message="Resumo diario carregado pelo provider configurado.",
            payload={"counts": summary.get("counts", {})},
        )
        completed = record_runtime_event(
            state,
            node="fetch_summary",
            status="completed",
            message="Resumo diário carregado com sucesso.",
            payload={"counts": summary.get("counts", {})},
        )
        return {**guardrail, **started, **event, **completed, "summary": summary}

    def sync_tracking(state: AgentState) -> dict[str, Any]:
        guardrail = allow_without_approval(
            state,
            action="daily_summary.sync_tracking",
            operation_kind=OperationKind.LOCAL_TRACKING_WRITE,
        )
        started = record_runtime_event(
            state,
            node="sync_tracking",
            status="started",
            message="Sincronizando tracking local do runtime novo.",
        )
        summary = state.get("summary") or {}
        source = state.get("source", "langgraph-runtime")
        result = ports.tracking.sync_daily_tracking(summary, source)
        event = append_tracking_event(
            state,
            category="tracking",
            message="Tracking local sincronizado pelo runtime LangGraph.",
            payload={"result": result if isinstance(result, dict) else {"value": str(result)}},
        )
        completed = record_runtime_event(
            state,
            node="sync_tracking",
            status="completed",
            message="Tracking local sincronizado com sucesso.",
        )
        return {**guardrail, **started, **event, **completed}

    def notify_user(state: AgentState) -> dict[str, Any]:
        if not state.get("notification_enabled", True) or ports.notifications is None:
            return record_runtime_event(
                state,
                node="notify_user",
                status="skipped",
                message="Notificação desabilitada para esta execução.",
            )

        guardrail = allow_without_approval(
            state,
            action="daily_summary.notify_user",
            operation_kind=OperationKind.NOTIFICATION,
        )
        started = record_runtime_event(
            state,
            node="notify_user",
            status="started",
            message="Enviando notificação do resumo diário.",
        )
        summary = state.get("summary") or {}
        result = ports.notifications.send_daily_summary_notification(summary)
        event = append_tracking_event(
            state,
            category="notification",
            message="Notificacao do resumo diario emitida pelo runtime LangGraph.",
            payload={"result": result if isinstance(result, dict) else {"value": str(result)}},
        )
        completed = record_runtime_event(
            state,
            node="notify_user",
            status="completed",
            message="Notificação do resumo diário concluída.",
        )
        return {**guardrail, **started, **event, **completed}

    graph.add_node("load_identity_context", load_identity_context)
    graph.add_node("fetch_summary", fetch_summary)
    graph.add_node("sync_tracking", sync_tracking)
    graph.add_node("notify_user", notify_user)

    graph.add_edge(START, "load_identity_context")
    graph.add_edge("load_identity_context", "fetch_summary")
    graph.add_edge("fetch_summary", "sync_tracking")
    graph.add_edge("sync_tracking", "notify_user")
    graph.add_edge("notify_user", END)

    return graph.compile()