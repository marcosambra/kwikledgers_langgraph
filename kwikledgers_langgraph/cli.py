from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from kwikledgers_fastmcp.shared.operational_context import OperationalContext
from kwikledgers_langgraph.fastmcp_client import FastMCPClient

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


def run_daily_summary(
    *, source: str, notifications_enabled: bool, backend_name: str | None = None,
    sprint_path: str | None = None, story_id: int | None = None,
) -> dict[str, Any]:
    resolved_backend = resolve_backend_name(backend_name)
    context = OperationalContext.from_environment() or OperationalContext(
        str(uuid4()), run_id=str(uuid4()), span_id=str(uuid4()),
    )

    async def execute() -> dict[str, Any]:
        client = FastMCPClient(context=context)
        async with client.session():
            runtime = build_daily_summary_runtime(build_daily_summary_ports(
                backend_name=resolved_backend, notifications_enabled=notifications_enabled,
                client=client, sprint_path=sprint_path, story_id=story_id,
            ))
            state = _build_initial_state(source, notifications_enabled, resolved_backend)
            state["observability"].update({
                "request_id": context.request_id,
                "active_run_id": context.run_id or state["observability"]["active_run_id"],
            })
            result = await asyncio.to_thread(runtime.invoke, state)
        if not isinstance(result, dict):
            raise RuntimeError("O runtime LangGraph retornou um estado final invalido.")
        return result

    return asyncio.run(execute())


def _record_runtime_observability(result: dict[str, Any]) -> None:
    append_runtime_observability_artifacts(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Executa o runtime LangGraph do resumo diario da KwikLedgers.")
    parser.add_argument("--source", default="langgraph-cli", help="Origem registrada no estado e no tracking.")
    parser.add_argument("--sprint-path", help="Caminho exato da sprint selecionada.")
    parser.add_argument("--story-id", type=int, help="História identificada pela branch ativa.")
    parser.add_argument("--json", action="store_true", help="Retorna somente o envelope operacional JSON versão 1.")
    parser.add_argument("--standalone-request", action="store_true", help="Esta CLI é dona do ciclo de métricas da solicitação.")
    parser.add_argument(
        "--backend",
        default=None,
        help="Backend de integracao do runtime. Apenas 'fastmcp' e suportado.",
    )
    parser.add_argument(
        "--disable-notifications",
        action="store_true",
        help="Nao envia notificacao pelo adapter Windows configurado.",
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

    try:
        result = run_daily_summary(
            source=args.source, notifications_enabled=not args.disable_notifications,
            backend_name=args.backend, sprint_path=args.sprint_path, story_id=args.story_id,
        )
    except Exception as error:
        if not args.json:
            raise
        print(json.dumps({
            "schema_version": 1, "operation": "daily_summary", "status": "failed",
            "result": None, "error": {"code": type(error).__name__},
        }))
        print(f"Falha no resumo operacional: {type(error).__name__}. Consulte o runtime.", file=sys.stderr)
        return 1

    summary = result.get("summary", {})
    counts = summary.get("counts", {})
    if args.standalone_request:
        result.setdefault("observability", {})["owns_request"] = True
    if not args.disable_observability_sync:
        try:
            _record_runtime_observability(result)
        except RuntimeError as error:
            print(f"Aviso: falha ao sincronizar observabilidade externa: {error}", file=sys.stderr)
            if args.json:
                result["metrics_error"] = type(error).__name__
    failed = bool(result.get("errors")) or not isinstance(result.get("summary"), dict)
    if args.json:
        context = OperationalContext.from_environment()
        print(json.dumps({
            "schema_version": 1, "operation": "daily_summary",
            "context": asdict(context) if context else {
                "request_id": result.get("observability", {}).get("request_id", ""),
                "run_id": result.get("observability", {}).get("active_run_id", ""),
            },
            "status": "failed" if failed else "completed",
            "result": result,
        }, ensure_ascii=False))
        return 1 if failed else 0
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