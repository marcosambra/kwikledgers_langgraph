from __future__ import annotations

import argparse
import json
from typing import Any

from kwikledgers_langgraph.backend_factory import build_task_closure_ports, resolve_backend_name
from kwikledgers_langgraph.guardrails import load_resume_confirmation
from kwikledgers_langgraph.observability import (
    append_runtime_observability_artifacts,
    build_observability_context,
)
from kwikledgers_langgraph.runtime import build_task_closure_runtime
from kwikledgers_langgraph.state import AgentState, TaskClosureRequest
from kwikledgers_langgraph.state_persistence import (
    StateFileError,
    load_state_file,
    write_state_file,
)


class TaskClosureCliError(RuntimeError):
    """Raised when CLI input cannot be mapped to a valid task closure flow state."""


def _required_start_arguments() -> tuple[tuple[str, str], ...]:
    return (
        ("task_id", "--task-id"),
        ("start_time", "--start-time"),
    )


def _build_request(arguments: argparse.Namespace) -> TaskClosureRequest:
    request: TaskClosureRequest = {
        "task_id": arguments.task_id,
        "start_time": arguments.start_time,
    }
    if arguments.close_time:
        request["close_time"] = arguments.close_time
    if arguments.completed_work_hours is not None:
        request["completed_work_hours"] = arguments.completed_work_hours
    if arguments.remaining_work_hours is not None:
        request["remaining_work_hours"] = arguments.remaining_work_hours
    if arguments.comment:
        request["comment"] = arguments.comment
    return request


def _build_initial_state(arguments: argparse.Namespace) -> AgentState:
    backend_name = resolve_backend_name(getattr(arguments, "backend", None))
    state: AgentState = {
        "source": arguments.source,
        "tracking_events": [],
        "task_closure_request": _build_request(arguments),
        "observability": build_observability_context(
            flow_name="task_closure",
            source=arguments.source,
            backend_name=backend_name,
        ),
    }
    return state


def _load_state_file(state_file_path: str) -> AgentState:
    try:
        return load_state_file(state_file_path)
    except StateFileError as error:
        raise TaskClosureCliError(str(error)) from error


def _build_resume_state(arguments: argparse.Namespace) -> AgentState:
    state = _load_state_file(arguments.resume_state_file)
    request = dict(state.get("task_closure_request", {}))
    preview = dict(state.get("task_closure_preview", {}))
    pending_approval = dict(state.get("pending_approval", {}))
    observability = dict(state.get("observability", {}))
    expected_request_id = pending_approval.get("request_id")

    if not request:
        raise TaskClosureCliError("O estado salvo nao contem 'task_closure_request' para retomar o fluxo.")
    if not preview:
        raise TaskClosureCliError("O estado salvo nao contem 'task_closure_preview' para comprovar o preview anterior.")
    if pending_approval.get("action") != "task_closure.execute":
        raise TaskClosureCliError(
            "O estado salvo nao contem uma aprovacao pendente valida para 'task_closure.execute'."
        )
    if not expected_request_id:
        raise TaskClosureCliError(
            "O estado salvo nao contem 'pending_approval.request_id' para vincular a aprovacao."
        )
    if dict(pending_approval.get("preview", {})) != preview:
        raise TaskClosureCliError(
            "O estado salvo esta inconsistente: o preview pendente nao corresponde ao preview exibido."
        )
    if getattr(arguments, "approval_request_id", None) is not None and arguments.approval_request_id != expected_request_id:
        raise TaskClosureCliError(
            "O 'approval_request_id' informado nao corresponde ao request_id pendente do estado salvo."
        )
    if not observability.get("active_run_id"):
        raise TaskClosureCliError(
            "O estado salvo nao contem 'observability.active_run_id' para retomar a aprovacao real."
        )
    resumed_backend = str(observability.get("backend_name") or "").strip().lower()
    requested_backend = resolve_backend_name(getattr(arguments, "backend", None))
    if resumed_backend and requested_backend != resumed_backend:
        raise TaskClosureCliError(
            "O backend solicitado para a retomada nao corresponde ao backend registrado no preview salvo."
        )

    resumed_state: AgentState = dict(state)
    resumed_state.pop("approval_simulation_requested", None)
    try:
        resumed_state["approval_confirmation"] = load_resume_confirmation(
            state, backend_name=requested_backend,
            receipt_file=getattr(arguments, "receipt", None),
            resume_state_file=arguments.resume_state_file,
            approval_request_id=getattr(arguments, "approval_request_id", None),
            approval_source=arguments.approval_source,
        )
    except ValueError as error:
        raise TaskClosureCliError(str(error)) from error
    return resumed_state


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if getattr(arguments, "receipt", None) is not None:
        raise TaskClosureCliError("Recibos foram removidos. Use '--approval-request-id'.")
    if arguments.resume_state_file:
        if not getattr(arguments, "receipt", None) and not getattr(arguments, "approval_request_id", None):
            raise TaskClosureCliError("Informe '--approval-request-id' ao retomar.")
        return
    if getattr(arguments, "receipt", None) or getattr(arguments, "approval_request_id", None):
        raise TaskClosureCliError("Informe aprovação somente com '--resume-state-file', após a prévia.")

    missing_arguments = [
        option_name
        for attribute_name, option_name in _required_start_arguments()
        if getattr(arguments, attribute_name, None) in (None, "")
    ]
    if missing_arguments:
        raise TaskClosureCliError(
            "Os argumentos de preview inicial estao incompletos: " + ", ".join(missing_arguments)
        )


def _build_runtime_state(arguments: argparse.Namespace) -> AgentState:
    _validate_arguments(arguments)
    if arguments.resume_state_file:
        return _build_resume_state(arguments)
    return _build_initial_state(arguments)


def _write_state_file(state: dict[str, Any], state_file_path: str) -> None:
    write_state_file(state, state_file_path)


def run_task_closure(arguments: argparse.Namespace) -> dict[str, Any]:
    backend_name = resolve_backend_name(getattr(arguments, "backend", None))
    state = _build_runtime_state(arguments)
    runtime = build_task_closure_runtime(build_task_closure_ports(backend_name=backend_name))
    result = runtime.invoke(state)
    if not isinstance(result, dict):
        raise RuntimeError("O runtime de fechamento de task retornou um estado final invalido.")
    return result


def _record_runtime_observability(result: dict[str, Any]) -> None:
    append_runtime_observability_artifacts(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Executa o fluxo LangGraph de preview/fechamento de task (Regra Absoluta de Fechamento de Historia). "
            "Use o modo inicial para gerar o preview com as horas calculadas via ceil() e salvar o estado; use "
            "'--resume-state-file' com '--approval-request-id' para confirmar o fechamento real."
        ),
    )
    parser.add_argument("--task-id", type=int, help="ID da task ou work item a ser fechado.")
    parser.add_argument("--start-time", help="Horario de inicio registrado, em ISO 8601.")
    parser.add_argument(
        "--close-time",
        default=None,
        help="Horario de fechamento em ISO 8601. Padrao: agora (UTC).",
    )
    parser.add_argument(
        "--completed-work-hours",
        type=float,
        default=None,
        help="Sobrescreve o calculo automatico de horas trabalhadas (ceil do tempo decorrido).",
    )
    parser.add_argument(
        "--remaining-work-hours",
        type=float,
        default=None,
        help="Horas restantes a lancar. Se omitido, o campo permanece vazio (nao forca 0).",
    )
    parser.add_argument("--comment", default=None, help="Comentario opcional registrado no work item.")
    parser.add_argument("--source", default="task-closure-cli", help="Origem registrada no estado do runtime.")
    parser.add_argument(
        "--backend",
        default=None,
        help="Backend de integração do runtime. O padrão é 'fastmcp'.",
    )
    parser.add_argument(
        "--resume-state-file",
        default=None,
        help="Arquivo JSON do estado final salvo apos o preview para retomar a aprovacao real.",
    )
    parser.add_argument(
        "--approval-request-id",
        default=None,
        help="ID da prévia salva que está sendo aprovada explicitamente nesta retomada.",
    )
    parser.add_argument(
        "--approval-source",
        default="human_cli_confirmation",
        help="Origem registrada para a confirmacao humana ao retomar um estado salvo.",
    )
    parser.add_argument(
        "--save-state-file",
        default=None,
        help="Grava o estado final completo em JSON para auditoria ou retomada posterior.",
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
        _validate_arguments(args)
    except TaskClosureCliError as error:
        parser.error(str(error))

    try:
        result = run_task_closure(args)
    except TaskClosureCliError as error:
        parser.error(str(error))

    final_status = result.get("task_closure_result", {}).get("status", "completed")
    if not args.disable_observability_sync:
        try:
            _record_runtime_observability(result)
        except RuntimeError as error:
            print(f"Aviso: falha ao sincronizar observabilidade externa: {error}")
    print(f"Fluxo de fechamento de task executado com status '{final_status}'.")
    if args.save_state_file:
        _write_state_file(result, args.save_state_file)
    if args.print_state:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
