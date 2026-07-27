from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from kwikledgers_langgraph.adapters import StoryPullRequestPorts
from kwikledgers_langgraph.guardrails import build_approval_confirmation
from kwikledgers_langgraph.legacy_adapters import LegacyStoryPullRequestAdapter
from kwikledgers_langgraph.observability import build_observability_context
from kwikledgers_langgraph.runtime import build_story_pull_request_runtime
from kwikledgers_langgraph.state import AgentState, StoryPullRequestRequest


class StoryPullRequestCliError(RuntimeError):
    """Raised when CLI input cannot be mapped to a valid story PR flow state."""


def _required_start_arguments() -> tuple[tuple[str, str], ...]:
    return (
        ("story_id", "--story-id"),
        ("repository_name", "--repository-name"),
        ("source_branch", "--source-branch"),
        ("what_was_changed", "--what-was-changed"),
        ("affected_processes", "--affected-processes"),
        ("expected_impacts", "--expected-impacts"),
        ("important_points", "--important-points"),
        ("mermaid_diagram", "--mermaid-diagram"),
    )


def _build_request(arguments: argparse.Namespace) -> StoryPullRequestRequest:
    return {
        "story_id": arguments.story_id,
        "repository_name": arguments.repository_name,
        "source_branch": arguments.source_branch,
        "what_was_changed": arguments.what_was_changed,
        "affected_processes": arguments.affected_processes,
        "expected_impacts": arguments.expected_impacts,
        "important_points": arguments.important_points,
        "mermaid_diagram": arguments.mermaid_diagram,
        "related_work_item_ids": arguments.related_work_item_ids or [],
        "include_child_work_items": not arguments.exclude_child_work_items,
        "title": arguments.title,
        "tests_updated": arguments.tests_updated,
        "tests_unchanged": arguments.tests_unchanged,
        "new_library_name": arguments.new_library_name,
        "env_changes": arguments.env_changes,
        "generated_migration_or_seed": arguments.generated_migration_or_seed,
        "new_queue_or_command": arguments.new_queue_or_command,
    }


def _build_initial_state(arguments: argparse.Namespace) -> AgentState:
    state: AgentState = {
        "source": arguments.source,
        "tracking_events": [],
        "pull_request_request": _build_request(arguments),
        "observability": build_observability_context(
            flow_name="story_pull_request",
            source=arguments.source,
        ),
    }
    if arguments.approve_create:
        state["approval_simulation_requested"] = True
    return state


def _load_state_file(state_file_path: str) -> AgentState:
    try:
        with Path(state_file_path).open("r", encoding="utf-8") as state_file:
            payload = json.load(state_file)
    except FileNotFoundError as error:
        raise StoryPullRequestCliError(f"O arquivo de estado '{state_file_path}' nao foi encontrado.") from error
    except json.JSONDecodeError as error:
        raise StoryPullRequestCliError(f"O arquivo de estado '{state_file_path}' nao contem JSON valido.") from error

    if not isinstance(payload, dict):
        raise StoryPullRequestCliError("O arquivo de estado precisa conter um objeto JSON no nivel raiz.")
    return cast(AgentState, payload)


def _build_resume_state(arguments: argparse.Namespace) -> AgentState:
    state = _load_state_file(arguments.resume_state_file)
    pull_request_request = dict(state.get("pull_request_request", {}))
    preview = dict(state.get("pull_request_preview", {}))
    pending_approval = dict(state.get("pending_approval", {}))
    observability = dict(state.get("observability", {}))
    expected_request_id = pending_approval.get("request_id")

    if not pull_request_request:
        raise StoryPullRequestCliError("O estado salvo nao contem 'pull_request_request' para retomar o fluxo.")
    if not preview:
        raise StoryPullRequestCliError("O estado salvo nao contem 'pull_request_preview' para comprovar o preview anterior.")
    if pending_approval.get("action") != "story_pull_request.create":
        raise StoryPullRequestCliError("O estado salvo nao contem uma aprovacao pendente valida para 'story_pull_request.create'.")
    if not expected_request_id:
        raise StoryPullRequestCliError("O estado salvo nao contem 'pending_approval.request_id' para vincular a aprovacao.")
    if dict(pending_approval.get("preview", {})) != preview:
        raise StoryPullRequestCliError("O estado salvo esta inconsistente: o preview pendente nao corresponde ao preview exibido.")
    if arguments.approval_request_id != expected_request_id:
        raise StoryPullRequestCliError("O 'approval_request_id' informado nao corresponde ao request_id pendente do estado salvo.")
    if not observability.get("active_run_id"):
        raise StoryPullRequestCliError("O estado salvo nao contem 'observability.active_run_id' para retomar a aprovacao real.")

    resumed_state: AgentState = dict(state)
    resumed_state.pop("approval_simulation_requested", None)
    resumed_state["approval_confirmation"] = build_approval_confirmation(
        request_id=expected_request_id,
        approved=True,
        approval_source=arguments.approval_source,
    )
    return resumed_state


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if arguments.resume_state_file:
        if arguments.approve_create:
            raise StoryPullRequestCliError("A flag '--approve-create' nao pode ser usada junto com '--resume-state-file'.")
        if not arguments.approval_request_id:
            raise StoryPullRequestCliError("Informe '--approval-request-id' ao usar '--resume-state-file'.")
        return

    missing_arguments = [
        option_name
        for attribute_name, option_name in _required_start_arguments()
        if getattr(arguments, attribute_name, None) in (None, "")
    ]
    if missing_arguments:
        raise StoryPullRequestCliError(
            "Os argumentos de preview inicial estao incompletos: " + ", ".join(missing_arguments)
        )


def _build_runtime_state(arguments: argparse.Namespace) -> AgentState:
    _validate_arguments(arguments)
    if arguments.resume_state_file:
        return _build_resume_state(arguments)
    return _build_initial_state(arguments)


def _write_state_file(state: dict[str, Any], state_file_path: str) -> None:
    output_path = Path(state_file_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2, ensure_ascii=False)
        state_file.write("\n")


def run_story_pull_request(arguments: argparse.Namespace) -> dict[str, Any]:
    runtime = build_story_pull_request_runtime(
        StoryPullRequestPorts(pull_requests=LegacyStoryPullRequestAdapter())
    )
    result = runtime.invoke(_build_runtime_state(arguments))
    if not isinstance(result, dict):
        raise RuntimeError("O runtime de PR da story retornou um estado final invalido.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Executa o fluxo LangGraph de preview/criacao de PR da story. "
            "Use o modo inicial para gerar o preview e salvar o estado; use "
            "'--resume-state-file' com '--approval-request-id' para retomar a criacao real."
        ),
    )
    parser.add_argument("--story-id", type=int, help="ID da story pai.")
    parser.add_argument("--repository-name", help="Nome do repositório no Azure DevOps.")
    parser.add_argument("--source-branch", help="Branch de origem da story.")
    parser.add_argument("--what-was-changed", help="Resumo formal do que foi alterado.")
    parser.add_argument("--affected-processes", help="Processos afetados pela mudança.")
    parser.add_argument("--expected-impacts", help="Impactos esperados da alteração.")
    parser.add_argument("--important-points", help="Pontos importantes para revisão.")
    parser.add_argument("--mermaid-diagram", help="Diagrama Mermaid que será anexado ao preview.")
    parser.add_argument("--source", default="story-pr-cli", help="Origem registrada no estado do runtime.")
    parser.add_argument("--title", default=None, help="Título explícito da PR, se necessário.")
    parser.add_argument(
        "--related-work-item-ids",
        type=int,
        nargs="*",
        default=None,
        help="IDs extras para associar à PR além da story e children padrão.",
    )
    parser.add_argument(
        "--exclude-child-work-items",
        action="store_true",
        help="Não inclui child work items automaticamente no preview.",
    )
    parser.add_argument("--tests-updated", action="store_true", help="Marca que os testes foram atualizados.")
    parser.add_argument("--tests-unchanged", action="store_true", help="Marca que os testes permaneceram inalterados.")
    parser.add_argument("--new-library-name", default=None, help="Nova biblioteca adicionada, se houver.")
    parser.add_argument("--env-changes", default=None, help="Mudanças de ambiente necessárias, se houver.")
    parser.add_argument(
        "--generated-migration-or-seed",
        default=None,
        help="Migração ou seed gerada pela mudança, se houver.",
    )
    parser.add_argument(
        "--new-queue-or-command",
        default=None,
        help="Nova fila ou comando operacional criado, se houver.",
    )
    parser.add_argument(
        "--approve-create",
        action="store_true",
        help="Simula aprovação local após o preview para testes e validação manual do runtime.",
    )
    parser.add_argument(
        "--resume-state-file",
        default=None,
        help="Arquivo JSON do estado final salvo apos o preview para retomar a aprovacao real.",
    )
    parser.add_argument(
        "--approval-request-id",
        default=None,
        help="Request ID explicitamente aprovado por um humano para o estado retomado.",
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
    args = parser.parse_args(argv)

    try:
        _validate_arguments(args)
    except StoryPullRequestCliError as error:
        parser.error(str(error))

    try:
        result = run_story_pull_request(args)
    except StoryPullRequestCliError as error:
        parser.error(str(error))

    final_status = result.get("pull_request_result", {}).get("status", "completed")
    print(f"Fluxo de PR da story executado com status '{final_status}'.")
    if args.save_state_file:
        _write_state_file(result, args.save_state_file)
    if args.print_state:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())