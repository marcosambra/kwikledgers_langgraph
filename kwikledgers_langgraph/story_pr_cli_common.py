from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from kwikledgers_langgraph.backend_factory import resolve_backend_name
from kwikledgers_langgraph.guardrails import load_resume_confirmation
from kwikledgers_langgraph.observability import (
    append_runtime_observability_artifacts,
    build_observability_context,
)
from kwikledgers_langgraph.state import AgentState
from kwikledgers_langgraph.state_persistence import StateFileError, load_state_file, write_state_file


class StoryPrCliError(RuntimeError):
    """Raised when CLI input cannot be mapped to a valid state."""


def add_common_arguments(parser: argparse.ArgumentParser, default_source: str) -> None:
    parser.add_argument("--story-id", type=int)
    parser.add_argument("--repository-name", help="Nome do repositório no Azure DevOps.")
    parser.add_argument("--pull-request-id", type=int, default=None,
                        help="Obrigatório se houver mais de uma PR candidata no repositório.")
    parser.add_argument("--source", default=default_source)
    parser.add_argument("--backend", default=None)
    parser.add_argument("--resume-state-file", default=None)
    parser.add_argument("--approval-request-id", default=None)
    parser.add_argument("--approval-source", default="human_cli_confirmation")
    parser.add_argument("--save-state-file", default=None)
    parser.add_argument("--print-state", action="store_true")
    parser.add_argument("--disable-observability-sync", action="store_true")


def validate_arguments(arguments: argparse.Namespace) -> None:
    if arguments.resume_state_file:
        if not arguments.approval_request_id:
            raise StoryPrCliError("Informe '--approval-request-id' ao retomar.")
        return
    if arguments.approval_request_id:
        raise StoryPrCliError("Informe aprovação somente com '--resume-state-file', após a prévia.")
    missing = [option for attribute, option in (("story_id", "--story-id"), ("repository_name", "--repository-name"))
               if getattr(arguments, attribute, None) in (None, "")]
    if missing:
        raise StoryPrCliError("Argumentos incompletos: " + ", ".join(missing))


def build_resume_state(
    arguments: argparse.Namespace, *, request_key: str, preview_key: str, action: str,
) -> AgentState:
    try:
        state = load_state_file(arguments.resume_state_file)
    except StateFileError as error:
        raise StoryPrCliError(str(error)) from error
    request = dict(state.get(request_key, {}))
    preview = dict(state.get(preview_key, {}))
    pending = dict(state.get("pending_approval", {}))
    if not request or not preview:
        raise StoryPrCliError("O estado salvo não contém request e preview para retomar.")
    if pending.get("action") != action or not pending.get("request_id"):
        raise StoryPrCliError(f"O estado salvo não contém aprovação pendente válida para '{action}'.")
    if dict(pending.get("preview", {})) != preview:
        raise StoryPrCliError("O preview pendente não corresponde ao preview exibido.")
    if arguments.approval_request_id != pending["request_id"]:
        raise StoryPrCliError("O 'approval_request_id' não corresponde ao request_id pendente.")
    if not dict(state.get("observability", {})).get("active_run_id"):
        raise StoryPrCliError("O estado salvo não contém 'observability.active_run_id'.")
    backend_name = resolve_backend_name(getattr(arguments, "backend", None))
    resumed: AgentState = dict(state)
    resumed.pop("approval_simulation_requested", None)
    try:
        resumed["approval_confirmation"] = load_resume_confirmation(
            state, backend_name=backend_name, receipt_file=None, resume_state_file=arguments.resume_state_file,
            approval_request_id=arguments.approval_request_id, approval_source=arguments.approval_source,
        )
    except ValueError as error:
        raise StoryPrCliError(str(error)) from error
    return resumed


def run_flow(
    arguments: argparse.Namespace, *, flow_name: str, request_key: str, preview_key: str, action: str,
    build_request: Callable[[argparse.Namespace], dict[str, Any]], build_runtime: Callable[[str], Any],
) -> dict[str, Any]:
    validate_arguments(arguments)
    backend_name = resolve_backend_name(getattr(arguments, "backend", None))
    if arguments.resume_state_file:
        state = build_resume_state(arguments, request_key=request_key, preview_key=preview_key, action=action)
    else:
        state = {
            "source": arguments.source, "tracking_events": [], request_key: build_request(arguments),
            "observability": build_observability_context(
                flow_name=flow_name, source=arguments.source, backend_name=backend_name),
        }
    result = build_runtime(backend_name).invoke(state)
    if not isinstance(result, dict):
        raise RuntimeError(f"O runtime {flow_name} retornou um estado final inválido.")
    return result


def finish(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace, runner: Callable[[], dict[str, Any]],
    *, result_key: str, label: str, summarize: Callable[[dict[str, Any]], list[str]],
) -> int:
    try:
        result = runner()
    except StoryPrCliError as error:
        parser.error(str(error))
    if not arguments.disable_observability_sync:
        try:
            append_runtime_observability_artifacts(result)
        except RuntimeError as error:
            print(f"Aviso: falha ao sincronizar observabilidade externa: {error}")
    outcome = dict(result.get(result_key, {}))
    print(f"{label} executado com status '{outcome.get('status', 'completed')}'.")
    for line in summarize(result):
        print(line)
    for error in result.get("errors", []):
        print(f"Erro: {error}")
    if arguments.save_state_file:
        write_state_file(result, arguments.save_state_file)
    if arguments.print_state:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0
