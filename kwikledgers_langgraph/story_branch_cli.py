from __future__ import annotations

import argparse
import json
from typing import Any

from kwikledgers_langgraph.backend_factory import build_story_branch_association_ports, resolve_backend_name
from kwikledgers_langgraph.guardrails import load_resume_confirmation
from kwikledgers_langgraph.observability import append_request_metrics, append_runtime_usage_metrics, build_observability_context
from kwikledgers_langgraph.runtime import build_story_branch_association_runtime
from kwikledgers_langgraph.state import AgentState, StoryBranchAssociationRequest
from kwikledgers_langgraph.state_persistence import (
    StateFileError,
    load_state_file,
    write_state_file,
)


class StoryBranchCliError(RuntimeError):
    """Raised when CLI input cannot be mapped to a valid story branch flow state."""


def _required_start_arguments() -> tuple[tuple[str, str], ...]:
    return (
        ("story_id", "--story-id"),
        ("repository_name", "--repository-name"),
        ("source_branch", "--source-branch"),
    )


def _build_request(arguments: argparse.Namespace) -> StoryBranchAssociationRequest:
    return {
        "story_id": arguments.story_id,
        "repository_name": arguments.repository_name,
        "source_branch": arguments.source_branch,
        "related_work_item_ids": arguments.related_work_item_ids or [],
        "include_child_work_items": not arguments.exclude_child_work_items,
    }


def _build_initial_state(arguments: argparse.Namespace) -> AgentState:
    backend_name = resolve_backend_name(getattr(arguments, "backend", None))
    state: AgentState = {
        "source": arguments.source,
        "tracking_events": [],
        "branch_association_request": _build_request(arguments),
        "observability": build_observability_context(
            flow_name="story_branch_association",
            source=arguments.source,
            backend_name=backend_name,
        ),
    }
    return state


def _load_state_file(state_file_path: str) -> AgentState:
    try:
        return load_state_file(state_file_path)
    except StateFileError as error:
        raise StoryBranchCliError(str(error)) from error


def _build_resume_state(arguments: argparse.Namespace) -> AgentState:
    state = _load_state_file(arguments.resume_state_file)
    branch_association_request = dict(state.get("branch_association_request", {}))
    preview = dict(state.get("branch_association_preview", {}))
    pending_approval = dict(state.get("pending_approval", {}))
    observability = dict(state.get("observability", {}))
    expected_request_id = pending_approval.get("request_id")

    if not branch_association_request:
        raise StoryBranchCliError("O estado salvo nao contem 'branch_association_request' para retomar o fluxo.")
    if not preview:
        raise StoryBranchCliError("O estado salvo nao contem 'branch_association_preview' para comprovar o preview anterior.")
    if pending_approval.get("action") != "story_branch_association.execute":
        raise StoryBranchCliError(
            "O estado salvo nao contem uma aprovacao pendente valida para 'story_branch_association.execute'."
        )
    if not expected_request_id:
        raise StoryBranchCliError(
            "O estado salvo nao contem 'pending_approval.request_id' para vincular a aprovacao."
        )
    if dict(pending_approval.get("preview", {})) != preview:
        raise StoryBranchCliError(
            "O estado salvo esta inconsistente: o preview pendente nao corresponde ao preview exibido."
        )
    if getattr(arguments, "approval_request_id", None) is not None and arguments.approval_request_id != expected_request_id:
        raise StoryBranchCliError(
            "O 'approval_request_id' informado nao corresponde ao request_id pendente do estado salvo."
        )
    if not observability.get("active_run_id"):
        raise StoryBranchCliError(
            "O estado salvo nao contem 'observability.active_run_id' para retomar a aprovacao real."
        )
    resumed_backend = str(observability.get("backend_name") or "").strip().lower()
    requested_backend = resolve_backend_name(getattr(arguments, "backend", None))
    if resumed_backend and requested_backend != resumed_backend:
        raise StoryBranchCliError(
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
        raise StoryBranchCliError(str(error)) from error
    return resumed_state


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if getattr(arguments, "receipt", None) is not None:
        raise StoryBranchCliError("Recibos foram removidos. Use '--approval-request-id'.")
    if arguments.resume_state_file:
        if not getattr(arguments, "receipt", None) and not getattr(arguments, "approval_request_id", None):
            raise StoryBranchCliError("Informe '--approval-request-id' ao retomar.")
        return
    if getattr(arguments, "receipt", None) or getattr(arguments, "approval_request_id", None):
        raise StoryBranchCliError("Informe aprovação somente com '--resume-state-file', após a prévia.")

    missing_arguments = [
        option_name
        for attribute_name, option_name in _required_start_arguments()
        if getattr(arguments, attribute_name, None) in (None, "")
    ]
    if missing_arguments:
        raise StoryBranchCliError(
            "Os argumentos de preview inicial estao incompletos: " + ", ".join(missing_arguments)
        )


def _build_runtime_state(arguments: argparse.Namespace) -> AgentState:
    _validate_arguments(arguments)
    if arguments.resume_state_file:
        return _build_resume_state(arguments)
    return _build_initial_state(arguments)


def _write_state_file(state: dict[str, Any], state_file_path: str) -> None:
    write_state_file(state, state_file_path)


def run_story_branch_association(arguments: argparse.Namespace) -> dict[str, Any]:
    backend_name = resolve_backend_name(getattr(arguments, "backend", None))
    state = _build_runtime_state(arguments)
    runtime = build_story_branch_association_runtime(
        build_story_branch_association_ports(backend_name=backend_name)
    )
    result = runtime.invoke(state)
    if not isinstance(result, dict):
        raise RuntimeError("O runtime de associacao de branch da story retornou um estado final invalido.")
    return result


def _record_runtime_observability(result: dict[str, Any]) -> None:
    append_runtime_usage_metrics(result)
    append_request_metrics(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Executa o fluxo LangGraph de preview/associacao de branch da story. "
            "Use o modo inicial para gerar o preview e salvar o estado; use "
            "'--resume-state-file' com '--approval-request-id' para confirmar a associação real."
        ),
    )
    parser.add_argument("--story-id", type=int, help="ID da story pai.")
    parser.add_argument("--repository-name", help="Nome do repositório no Azure DevOps.")
    parser.add_argument("--source-branch", help="Branch de origem da story.")
    parser.add_argument("--source", default="story-branch-cli", help="Origem registrada no estado do runtime.")
    parser.add_argument(
        "--backend",
        default=None,
        help="Backend de integração do runtime. O padrão é 'fastmcp'.",
    )
    parser.add_argument(
        "--related-work-item-ids",
        type=int,
        nargs="*",
        default=None,
        help="IDs extras para associar à branch além da story e children padrão.",
    )
    parser.add_argument(
        "--exclude-child-work-items",
        action="store_true",
        help="Não inclui child work items automaticamente no preview.",
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
    except StoryBranchCliError as error:
        parser.error(str(error))

    try:
        result = run_story_branch_association(args)
    except StoryBranchCliError as error:
        parser.error(str(error))

    final_status = result.get("branch_association_result", {}).get("status", "completed")
    if not args.disable_observability_sync:
        try:
            _record_runtime_observability(result)
        except RuntimeError as error:
            print(f"Aviso: falha ao sincronizar observabilidade externa: {error}")
    print(f"Fluxo de associacao de branch da story executado com status '{final_status}'.")
    if args.save_state_file:
        _write_state_file(result, args.save_state_file)
    if args.print_state:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())