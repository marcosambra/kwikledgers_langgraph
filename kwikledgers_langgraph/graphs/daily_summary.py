from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import DailySummaryPorts
from kwikledgers_langgraph.fastmcp_adapters import technical_arguments_for_capability
from kwikledgers_langgraph.fastmcp_execution_capability import issue_runtime_execution_capability
from kwikledgers_langgraph.guardrails import OperationKind, allow_without_approval
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_error, append_tracking_event


def build_daily_summary_graph(ports: DailySummaryPorts) -> Any:
    graph = StateGraph(AgentState)

    def _notification_failed(result: Any) -> bool:
        if isinstance(result, dict):
            status = str(result.get("status") or "").strip().lower()
            return status in {"error", "failed", "send_failed"}
        if isinstance(result, str):
            normalized = result.strip().lower()
            return normalized.startswith("erro ao ") or normalized.startswith("error ")
        return False

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
        if _is_fastmcp_backend(state):
            arguments = technical_arguments_for_capability(
                "sync_daily_tracking", {"summary": summary, "source": source},
            )
            result = ports.tracking.sync_daily_tracking(
                summary,
                source,
                execution_capability=issue_runtime_execution_capability(
                    state,
                    action="daily_summary.sync_tracking",
                    capability_name="sync_daily_tracking",
                    arguments=arguments,
                ),
            )
        else:
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
        try:
            if _is_fastmcp_backend(state):
                arguments = technical_arguments_for_capability("send_notification", {"summary": summary})
                result = ports.notifications.send_daily_summary_notification(
                    summary,
                    execution_capability=issue_runtime_execution_capability(
                        state,
                        action="daily_summary.notify_user",
                        capability_name="send_notification",
                        arguments=arguments,
                    ),
                )
            else:
                result = ports.notifications.send_daily_summary_notification(summary)
        except Exception as error:
            result = {"status": "failed", "message": str(error)}
        event = append_tracking_event(
            state,
            category="notification",
            message="Notificacao do resumo diario emitida pelo runtime LangGraph.",
            payload={"result": result if isinstance(result, dict) else {"value": str(result)}},
        )

        if _notification_failed(result):
            error_message = str(result if isinstance(result, str) else result.get("message") or result)
            error = append_error(state, error_message)
            failed = record_runtime_event(
                state,
                node="notify_user",
                status="failed",
                message="Falha ao enviar notificacao do resumo diario.",
            )
            return {**guardrail, **started, **event, **error, **failed}

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


def _is_fastmcp_backend(state: AgentState) -> bool:
    return str(dict(state.get("observability", {})).get("backend_name") or "").strip().lower() == "fastmcp"