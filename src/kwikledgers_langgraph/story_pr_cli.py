from __future__ import annotations

import argparse
import json
from typing import Any

from kwikledgers_langgraph.adapters import StoryPullRequestPorts
from kwikledgers_langgraph.legacy_adapters import LegacyStoryPullRequestAdapter
from kwikledgers_langgraph.observability import build_observability_context
from kwikledgers_langgraph.runtime import build_story_pull_request_runtime
from kwikledgers_langgraph.state import AgentState, StoryPullRequestRequest


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


def run_story_pull_request(arguments: argparse.Namespace) -> dict[str, Any]:
    runtime = build_story_pull_request_runtime(
        StoryPullRequestPorts(pull_requests=LegacyStoryPullRequestAdapter())
    )
    result = runtime.invoke(_build_initial_state(arguments))
    if not isinstance(result, dict):
        raise RuntimeError("O runtime de PR da story retornou um estado final invalido.")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Executa o fluxo LangGraph de preview/criação de PR da story.",
    )
    parser.add_argument("--story-id", type=int, required=True, help="ID da story pai.")
    parser.add_argument("--repository-name", required=True, help="Nome do repositório no Azure DevOps.")
    parser.add_argument("--source-branch", required=True, help="Branch de origem da story.")
    parser.add_argument("--what-was-changed", required=True, help="Resumo formal do que foi alterado.")
    parser.add_argument("--affected-processes", required=True, help="Processos afetados pela mudança.")
    parser.add_argument("--expected-impacts", required=True, help="Impactos esperados da alteração.")
    parser.add_argument("--important-points", required=True, help="Pontos importantes para revisão.")
    parser.add_argument("--mermaid-diagram", required=True, help="Diagrama Mermaid que será anexado ao preview.")
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
        "--print-state",
        action="store_true",
        help="Imprime o estado final completo em JSON.",
    )
    args = parser.parse_args()

    result = run_story_pull_request(args)
    final_status = result.get("pull_request_result", {}).get("status", "completed")
    print(f"Fluxo de PR da story executado com status '{final_status}'.")
    if args.print_state:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())