from __future__ import annotations

import argparse
import fcntl
import json
import os
from typing import cast

from kwikledgers_langgraph.backend_factory import build_day_end_closure_ports, resolve_backend_name
from kwikledgers_langgraph.fastmcp_adapters import FastMCPNotificationAdapter
from kwikledgers_langgraph.fastmcp_execution_capability import issue_runtime_execution_capability
from kwikledgers_langgraph.observability import (
    append_runtime_observability_artifacts,
    build_observability_context,
    resolve_ai_tracking_root,
)
from kwikledgers_langgraph.runtime import build_day_end_closure_runtime
from kwikledgers_langgraph.state import AgentState, DayEndClosureRequest, ObservabilityContext
from kwikledgers_langgraph.state_persistence import write_state_file


def _build_initial_state(arguments: argparse.Namespace, backend_name: str) -> AgentState:
    request: DayEndClosureRequest = {
        "trigger": arguments.trigger,
        "cutoff_time": arguments.cutoff_time,
        "dry_run": arguments.dry_run,
        "create_continuation": not arguments.no_continuation,
    }
    if arguments.timezone:
        request["timezone"] = arguments.timezone
    if getattr(arguments, "task_id", None):
        request["task_id"] = arguments.task_id
    if getattr(arguments, "completed_at", None):
        request["now"] = arguments.completed_at
    return {
        "source": f"day-end-closure-{arguments.trigger}",
        "tracking_events": [],
        "day_end_closure_request": request,
        "observability": cast(ObservabilityContext, build_observability_context(
            flow_name="day_end_closure",
            source=f"day-end-closure-{arguments.trigger}",
            backend_name=backend_name,
        )),
    }


def run_day_end_closure(arguments: argparse.Namespace) -> AgentState:
    backend_name = resolve_backend_name(arguments.backend)
    runtime = build_day_end_closure_runtime(build_day_end_closure_ports(backend_name=backend_name))
    state = _build_initial_state(arguments, backend_name)
    lock_dir = resolve_ai_tracking_root() / "Task_Control"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "day-end-closure.lock").open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**state, "day_end_closure_result": {"status": "skipped", "reason": "already_running", "changed": False}}
        result = runtime.invoke(state)
    if not isinstance(result, dict):
        raise RuntimeError("O runtime de fechamento de fim de dia retornou um estado final invalido.")
    return cast(AgentState, result)


def _notification_text(result: AgentState) -> tuple[str, str, str]:
    outcome = dict(result.get("day_end_closure_result", {}))
    errors = result.get("errors") or []
    if errors:
        return "Fechamento de fim de dia falhou", "; ".join(errors)[:240], "high"
    status = outcome.get("status")
    if status in ("closed", "closed_continuation_failed"):
        urgency = "high" if status == "closed_continuation_failed" else "normal"
        message = (
            f"Task {outcome.get('task_id')} fechada com {outcome.get('completed_work_hours')}h. "
            f"Continuacao: {outcome.get('continuation_task_id', 'nao criada')}."
        )
        return "Horas lancadas no Azure DevOps", message, urgency
    return "Fechamento de fim de dia", f"Nada alterado ({status}: {outcome.get('reason')}).", "normal"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Lança as horas da task ativa, fecha-a e cria a continuação no Azure DevOps. "
            "Com --dry-run gera apenas a prévia, sem escrita."
        ),
    )
    parser.add_argument("--trigger", choices=("manual", "scheduled", "completed"), default="manual")
    parser.add_argument("--task-id", type=int, help="Task especifica; obrigatoria para o hook completed.")
    parser.add_argument("--completed-at", help="Horario registrado da conclusao, em ISO 8601, somente para completed.")
    parser.add_argument("--cutoff-time", default="18:00", help="Horario de corte local (HH:MM).")
    parser.add_argument("--timezone", default=None, help="Fuso IANA. Padrao: fuso local.")
    parser.add_argument("--dry-run", action="store_true", help="Gera o preview sem escrever no Azure.")
    parser.add_argument("--no-continuation", action="store_true", help="Nao cria a task de continuacao.")
    parser.add_argument("--backend", default=None, help="Backend de integração do runtime. Apenas 'fastmcp' é suportado.")
    parser.add_argument("--notify", action="store_true", help="Envia notificacao com o resultado.")
    parser.add_argument("--save-state-file", default=None, help="Grava o estado final em JSON.")
    parser.add_argument("--print-state", action="store_true", help="Imprime o estado final completo em JSON.")
    parser.add_argument("--disable-observability-sync", action="store_true")
    args = parser.parse_args(argv)
    if args.trigger == "completed" and (not args.task_id or args.task_id <= 0):
        parser.error("O hook completed exige --task-id positivo.")
    if args.completed_at and args.trigger != "completed":
        parser.error("--completed-at exige --trigger completed.")

    if args.notify and os.environ.get("KWIKLEDGERS_ENV_FILE"):
        from dotenv import load_dotenv

        load_dotenv(os.environ["KWIKLEDGERS_ENV_FILE"])
    result = run_day_end_closure(args)
    outcome = dict(result.get("day_end_closure_result", {}))
    if not args.disable_observability_sync:
        try:
            append_runtime_observability_artifacts(result)
        except RuntimeError as error:
            print(f"Aviso: falha ao sincronizar observabilidade externa: {error}")
    print(f"Fechamento de fim de dia executado com status '{outcome.get('status', 'completed')}'.")
    if args.notify and not (outcome.get("status") == "skipped" and outcome.get("reason") in {"no_activity", "no_task_activity", "no_active_task"}):
        try:
            notification = FastMCPNotificationAdapter(execution_capability_provider=lambda name, payload:
                issue_runtime_execution_capability(result, action="day_end_notification",
                    capability_name=name, arguments=payload))
            notification.send_notification(*_notification_text(result))
        except Exception as error:
            print(f"Aviso: falha ao enviar notificacao: {error}")
    if args.save_state_file:
        write_state_file(dict(result), args.save_state_file)
    if args.print_state:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if result.get("errors") or outcome.get("status") in {"execute_failed", "closed_continuation_failed", "preview_failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
