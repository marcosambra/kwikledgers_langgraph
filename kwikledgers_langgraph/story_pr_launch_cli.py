from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kwikledgers_langgraph.backend_factory import build_story_pr_launch_ports, resolve_backend_name
from kwikledgers_langgraph.guardrails import load_resume_confirmation
from kwikledgers_langgraph.observability import (
    append_runtime_observability_artifacts,
    build_observability_context,
)
from kwikledgers_langgraph.runtime import build_story_pr_launch_runtime
from kwikledgers_langgraph.state import AgentState
from kwikledgers_langgraph.state_persistence import StateFileError, load_state_file, write_state_file

ACTION = "story_pr_launch.create"
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


class StoryPrLaunchCliError(RuntimeError):
    """Raised when CLI input cannot be mapped to a valid PR launch state."""


def _default_repository_path(repository_name: str) -> str:
    return str(WORKSPACE_ROOT / "projects" / repository_name.strip().lower().replace(" ", "_"))


def _build_request(arguments: argparse.Namespace) -> dict[str, Any]:
    return {
        "story_id": arguments.story_id,
        "repository_name": arguments.repository_name,
        "repository_path": arguments.repository_path or _default_repository_path(arguments.repository_name),
        "source_branch": arguments.source_branch,
        "target_branch": arguments.target_branch,
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
        "tests_evidence": arguments.tests_evidence,
        "evidence_files": arguments.evidence_file or [],
        "env_changes": arguments.env_changes,
        "generated_migration_or_seed": arguments.generated_migration_or_seed,
        "new_queue_or_command": arguments.new_queue_or_command,
        "new_library_name": arguments.new_library_name,
    }


def _build_resume_state(arguments: argparse.Namespace) -> AgentState:
    try:
        state = load_state_file(arguments.resume_state_file)
    except StateFileError as error:
        raise StoryPrLaunchCliError(str(error)) from error
    request = dict(state.get("pull_request_request", {}))
    preview = dict(state.get("pull_request_preview", {}))
    pending = dict(state.get("pending_approval", {}))
    observability = dict(state.get("observability", {}))
    if not request or not preview:
        raise StoryPrLaunchCliError("O estado salvo não contém request e preview para retomar.")
    if pending.get("action") != ACTION or not pending.get("request_id"):
        raise StoryPrLaunchCliError(f"O estado salvo não contém aprovação pendente válida para '{ACTION}'.")
    if dict(pending.get("preview", {})) != preview:
        raise StoryPrLaunchCliError("O preview pendente não corresponde ao preview exibido.")
    if arguments.approval_request_id != pending["request_id"]:
        raise StoryPrLaunchCliError("O 'approval_request_id' não corresponde ao request_id pendente.")
    if not observability.get("active_run_id"):
        raise StoryPrLaunchCliError("O estado salvo não contém 'observability.active_run_id'.")
    backend_name = resolve_backend_name(getattr(arguments, "backend", None))
    resumed: AgentState = dict(state)
    resumed.pop("approval_simulation_requested", None)
    try:
        resumed["approval_confirmation"] = load_resume_confirmation(
            state, backend_name=backend_name, receipt_file=None,
            resume_state_file=arguments.resume_state_file,
            approval_request_id=arguments.approval_request_id,
            approval_source=arguments.approval_source,
        )
    except ValueError as error:
        raise StoryPrLaunchCliError(str(error)) from error
    return resumed


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if arguments.resume_state_file:
        if not arguments.approval_request_id:
            raise StoryPrLaunchCliError("Informe '--approval-request-id' ao retomar.")
        return
    if arguments.approval_request_id:
        raise StoryPrLaunchCliError("Informe aprovação somente com '--resume-state-file', após a prévia.")
    required = (("story_id", "--story-id"), ("repository_name", "--repository-name"),
                ("source_branch", "--source-branch"), ("what_was_changed", "--what-was-changed"),
                ("affected_processes", "--affected-processes"), ("expected_impacts", "--expected-impacts"),
                ("important_points", "--important-points"), ("mermaid_diagram", "--mermaid-diagram"))
    missing = [option for attribute, option in required if getattr(arguments, attribute, None) in (None, "")]
    if missing:
        raise StoryPrLaunchCliError("Argumentos do preview incompletos: " + ", ".join(missing))


def run_story_pr_launch(arguments: argparse.Namespace) -> dict[str, Any]:
    _validate_arguments(arguments)
    backend_name = resolve_backend_name(getattr(arguments, "backend", None))
    if arguments.resume_state_file:
        state = _build_resume_state(arguments)
    else:
        state = {
            "source": arguments.source,
            "tracking_events": [],
            "pull_request_request": _build_request(arguments),
            "observability": build_observability_context(
                flow_name="story_pr_launch", source=arguments.source, backend_name=backend_name,
            ),
        }
    runtime = build_story_pr_launch_runtime(build_story_pr_launch_ports(backend_name=backend_name))
    result = runtime.invoke(state)
    if not isinstance(result, dict):
        raise RuntimeError("O runtime de lançamento da PR retornou um estado final inválido.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Lança a PR Draft de uma branch já publicada: checklist determinístico (branch publicada, commits, "
            "arquivos proibidos, template, testes, print), preview, aprovação, criação e verificação final."
        ),
    )
    parser.add_argument("--story-id", type=int)
    parser.add_argument("--repository-name", help="Nome do repositório no Azure DevOps.")
    parser.add_argument("--repository-path", default=None, help="Caminho local do repositório. Padrão: projects/<nome>.")
    parser.add_argument("--source-branch")
    parser.add_argument("--target-branch", default="stage-pre-prod",
                        help="Alvo da PR. Use stage-homolog para a PR de uma branch <branch-de-prod>-on-stage-homolog")
    parser.add_argument("--what-was-changed")
    parser.add_argument("--affected-processes")
    parser.add_argument("--expected-impacts")
    parser.add_argument("--important-points")
    parser.add_argument("--mermaid-diagram")
    parser.add_argument("--title", default=None)
    parser.add_argument("--related-work-item-ids", type=int, nargs="*", default=None)
    parser.add_argument("--exclude-child-work-items", action="store_true")
    parser.add_argument("--tests-updated", action="store_true")
    parser.add_argument("--tests-unchanged", action="store_true")
    parser.add_argument("--tests-evidence", default=None, help="Comando e resultado dos testes executados.")
    parser.add_argument("--evidence-file", action="append", help="Print de evidência; obrigatório em frontend.")
    parser.add_argument("--new-library-name", default=None)
    parser.add_argument("--env-changes", default=None)
    parser.add_argument("--generated-migration-or-seed", default=None)
    parser.add_argument("--new-queue-or-command", default=None)
    parser.add_argument("--source", default="story-pr-launch-cli")
    parser.add_argument("--backend", default=None)
    parser.add_argument("--resume-state-file", default=None)
    parser.add_argument("--approval-request-id", default=None)
    parser.add_argument("--approval-source", default="human_cli_confirmation")
    parser.add_argument("--save-state-file", default=None)
    parser.add_argument("--print-state", action="store_true")
    parser.add_argument("--disable-observability-sync", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = run_story_pr_launch(args)
    except StoryPrLaunchCliError as error:
        parser.error(str(error))

    outcome = dict(result.get("pull_request_result", {}))
    if not args.disable_observability_sync:
        try:
            append_runtime_observability_artifacts(result)
        except RuntimeError as error:
            print(f"Aviso: falha ao sincronizar observabilidade externa: {error}")
    print(f"Lançamento da PR executado com status '{outcome.get('status', 'completed')}'.")
    for item in dict(result.get("pull_request_preflight", {})).get("checks", []):
        if not item["ok"]:
            print(f"- [{item['severity']}] {item['id']}: {item['detail']}")
    verification = dict(outcome.get("verification", {}))
    if verification:
        print(f"PR: {verification.get('url')}")
        for step in verification.get("pending_after_pr", []):
            print(f"Pendente: {step}")
    for error in result.get("errors", []):
        print(f"Erro: {error}")
    if args.save_state_file:
        write_state_file(result, args.save_state_file)
    if args.print_state:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
