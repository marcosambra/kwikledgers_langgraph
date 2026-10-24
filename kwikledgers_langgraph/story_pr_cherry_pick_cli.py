from __future__ import annotations

import argparse
from typing import Any

from kwikledgers_langgraph.backend_factory import build_story_pr_cherry_pick_ports
from kwikledgers_langgraph.runtime import build_story_pr_cherry_pick_runtime
from kwikledgers_langgraph.story_pr_cli_common import add_common_arguments, finish, run_flow

ACTION = "story_pr_cherry_pick.execute"


def _build_request(arguments: argparse.Namespace) -> dict[str, Any]:
    request = {
        "story_id": arguments.story_id, "repository_name": arguments.repository_name,
        "pull_request_id": arguments.pull_request_id, "target_branch": arguments.target_branch,
        "source_target_branch": arguments.source_target_branch,
    }
    if arguments.repository_path:
        request["repository_path"] = arguments.repository_path
    return request


def run_story_pr_cherry_pick(arguments: argparse.Namespace) -> dict[str, Any]:
    return run_flow(
        arguments, flow_name="story_pr_cherry_pick", request_key="cherry_pick_request",
        preview_key="cherry_pick_preview", action=ACTION, build_request=_build_request,
        build_runtime=lambda backend: build_story_pr_cherry_pick_runtime(
            build_story_pr_cherry_pick_ports(backend_name=backend)),
    )


def _summarize(result: dict[str, Any]) -> list[str]:
    preview = dict(result.get("cherry_pick_preview", {}))
    outcome = dict(result.get("cherry_pick_result", {}))
    lines: list[str] = []
    if preview:
        lines.append(f"PR {preview.get('pull_request_id')} -> {preview.get('target_branch')} "
                     f"(branch {preview.get('generated_branch')}, ação: {preview.get('action')})")
        lines += [f"- {step}" for step in preview.get("steps", [])]
    report = dict(outcome.get("report", {}))
    if report.get("worktree_path"):
        lines.append(f"Conflito: worktree preservado em {report['worktree_path']} "
                     f"(commit {report.get('stuck_commit')}): {', '.join(report.get('conflicting_files', []))}")
    if report.get("resume"):
        lines.append(str(report["resume"]))
    verification = dict(outcome.get("verification", {}))
    labels = {"pull_request_origem": "PR de origem", "branch_cherry_pick": "Branch gerada",
              "criar_pr_para_alvo": "Abrir PR para o alvo"}
    lines += [f"{labels.get(name, name)}: {url}" for name, url in
              {**report.get("links", {}), **verification.get("links", {})}.items()]
    lines += [f"Pendente: {step}" for step in verification.get("pending_after_cherry_pick", [])]
    lines += [f"Pendente: {step}" for step in report.get("pending_after_cherry_pick", [])]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Cherry-pick de uma PR ativa da história para outra branch (padrão stage-homolog), sem merge: "
            "tenta a API do Azure e, em conflito, usa um worktree dedicado preservado para resolução."
        ),
    )
    add_common_arguments(parser, "story-pr-cherry-pick-cli")
    parser.add_argument("--target-branch", default="stage-homolog")
    parser.add_argument("--source-target-branch", default="stage-pre-prod",
                        help="Alvo das PRs candidatas da história.")
    parser.add_argument("--repository-path", default=None, help="Caminho local. Padrão: projects/<nome>.")
    arguments = parser.parse_args(argv)
    return finish(parser, arguments, lambda: run_story_pr_cherry_pick(arguments), result_key="cherry_pick_result",
                  label="Cherry-pick", summarize=_summarize)


if __name__ == "__main__":
    raise SystemExit(main())
