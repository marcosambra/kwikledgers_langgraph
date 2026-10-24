from __future__ import annotations

import argparse
from typing import Any

from kwikledgers_langgraph.backend_factory import build_story_pr_publish_ports
from kwikledgers_langgraph.runtime import build_story_pr_publish_runtime
from kwikledgers_langgraph.story_pr_cli_common import add_common_arguments, finish, run_flow

ACTION = "story_pr_publish.execute"


def _build_request(arguments: argparse.Namespace) -> dict[str, Any]:
    return {"story_id": arguments.story_id, "repository_name": arguments.repository_name,
            "pull_request_id": arguments.pull_request_id}


def run_story_pr_publish(arguments: argparse.Namespace) -> dict[str, Any]:
    return run_flow(
        arguments, flow_name="story_pr_publish", request_key="publish_request", preview_key="publish_preview",
        action=ACTION, build_request=_build_request,
        build_runtime=lambda backend: build_story_pr_publish_runtime(build_story_pr_publish_ports(backend_name=backend)),
    )


def _summarize(result: dict[str, Any]) -> list[str]:
    outcome = dict(result.get("publish_result", {}))
    verification = dict(outcome.get("verification", {}))
    lines = [f"PR: {verification['url']}"] if verification.get("url") else []
    lines += [f"Pendente: {step}" for step in verification.get("pending_after_publish", [])]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publica (tira de Draft) a PR ativa de uma história, com preview, aprovação e verificação.",
    )
    add_common_arguments(parser, "story-pr-publish-cli")
    arguments = parser.parse_args(argv)
    return finish(parser, arguments, lambda: run_story_pr_publish(arguments), result_key="publish_result",
                  label="Publicação da PR", summarize=_summarize)


if __name__ == "__main__":
    raise SystemExit(main())
