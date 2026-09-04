from __future__ import annotations

import argparse
import json
from typing import Any

from kwikledgers_langgraph.backend_factory import build_daily_summary_ports, resolve_backend_name
from kwikledgers_langgraph.observability import (
    append_runtime_observability_artifacts,
    build_observability_context,
)
from kwikledgers_langgraph.runtime import build_daily_summary_runtime
from kwikledgers_langgraph.state import AgentState


def _build_initial_state(source: str, notifications_enabled: bool, backend_name: str) -> AgentState:
    return {
        "source": source,
        "notification_enabled": notifications_enabled,
        "tracking_events": [],
        "observability": build_observability_context(
            flow_name="daily_summary",
            source=source,
            backend_name=backend_name,
        ),
    }


def run_daily_summary(*, source: str, notifications_enabled: bool, backend_name: str | None = None) -> dict[str, Any]:
    resolved_backend = resolve_backend_name(backend_name)
    runtime = build_daily_summary_runtime(
        build_daily_summary_ports(
            backend_name=resolved_backend,
            notifications_enabled=notifications_enabled,
        )
    )
    state = _build_initial_state(source, notifications_enabled, resolved_backend)
    result = runtime.invoke(state)
    if not isinstance(result, dict):
        raise RuntimeError("O runtime LangGraph retornou um estado final invalido.")
    return result


def _record_runtime_observability(result: dict[str, Any]) -> None:
    append_runtime_observability_artifacts(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Executa o runtime LangGraph do resumo diario da KwikLedgers.")
    parser.add_argument("--source", default="langgraph-cli", help="Origem registrada no estado e no tracking.")
    parser.add_argument(
        "--backend",
        default=None,
        help="Backend de integracao do runtime. O padrao atual e 'legacy'.",
    )
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
    parser.add_argument(
        "--disable-observability-sync",
        action="store_true",
        help="Nao grava a observabilidade resumida do runtime em AI_Tracking/Metrics/ai-usage-log.md.",
    )
    args = parser.parse_args(argv)

    result = run_daily_summary(
        source=args.source,
        notifications_enabled=not args.disable_notifications,
        backend_name=args.backend,
    )

    summary = result.get("summary", {})
    counts = summary.get("counts", {})
    if not args.disable_observability_sync:
        try:
            _record_runtime_observability(result)
        except RuntimeError as error:
            print(f"Aviso: falha ao sincronizar observabilidade externa: {error}")
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