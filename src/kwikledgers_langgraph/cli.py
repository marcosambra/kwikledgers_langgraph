from __future__ import annotations

import argparse
import json
from typing import Any

from kwikledgers_langgraph.adapters import DailySummaryPorts
from kwikledgers_langgraph.legacy_adapters import (
    EnvironmentIdentityResolverAdapter,
    LegacyDailySummaryProvider,
    LegacyNotificationAdapter,
    LegacyTrackingSyncAdapter,
    NoOpNotificationAdapter,
)
from kwikledgers_langgraph.observability import (
    build_langsmith_trace_outputs,
    build_observability_context,
    trace_runtime_execution,
)
from kwikledgers_langgraph.runtime import build_daily_summary_runtime
from kwikledgers_langgraph.state import AgentState


def _build_initial_state(source: str, notifications_enabled: bool) -> AgentState:
    return {
        "source": source,
        "notification_enabled": notifications_enabled,
        "tracking_events": [],
        "observability": build_observability_context(
            flow_name="daily_summary",
            source=source,
        ),
    }


def _build_ports(notifications_enabled: bool) -> DailySummaryPorts:
    notifications = LegacyNotificationAdapter() if notifications_enabled else NoOpNotificationAdapter()
    return DailySummaryPorts(
        identity=EnvironmentIdentityResolverAdapter(),
        provider=LegacyDailySummaryProvider(),
        tracking=LegacyTrackingSyncAdapter(),
        notifications=notifications,
    )


def run_daily_summary(*, source: str, notifications_enabled: bool) -> dict[str, Any]:
    runtime = build_daily_summary_runtime(_build_ports(notifications_enabled))
    state = _build_initial_state(source, notifications_enabled)
    with trace_runtime_execution(state) as traced_run:
        result = runtime.invoke(state)
        if traced_run is not None:
            traced_run.end(outputs=build_langsmith_trace_outputs(result))
    if not isinstance(result, dict):
        raise RuntimeError("O runtime LangGraph retornou um estado final invalido.")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Executa o runtime LangGraph do resumo diario da KwikLedgers.")
    parser.add_argument("--source", default="langgraph-cli", help="Origem registrada no estado e no tracking.")
    parser.add_argument(
        "--disable-notifications",
        action="store_true",
        help="Nao envia notificacao pelo adapter legado de Windows.",
    )
    parser.add_argument(
        "--print-state",
        action="store_true",
        help="Imprime o estado final completo em JSON.",
    )
    args = parser.parse_args()

    result = run_daily_summary(
        source=args.source,
        notifications_enabled=not args.disable_notifications,
    )

    summary = result.get("summary", {})
    counts = summary.get("counts", {})
    print(
        "Runtime LangGraph executado com sucesso "
        f"para {counts.get('assigned_items', 0)} itens atribuídos e "
        f"{counts.get('blocked_items', 0)} bloqueios."
    )
    if args.print_state:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())