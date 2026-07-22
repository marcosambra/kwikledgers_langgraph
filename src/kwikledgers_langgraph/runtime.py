from __future__ import annotations

from typing import Any

from kwikledgers_langgraph.adapters import DailySummaryPorts, StoryPullRequestPorts


def build_daily_summary_runtime(ports: DailySummaryPorts) -> Any:
    try:
        from kwikledgers_langgraph.graphs.daily_summary import build_daily_summary_graph
    except ModuleNotFoundError as error:
        if error.name == "langgraph":
            raise RuntimeError(
                "A dependencia 'langgraph' ainda nao esta instalada neste ambiente. "
                "Instale as dependencias do projeto 'kwikledgers_langgraph' para compilar o runtime."
            ) from error
        raise

    return build_daily_summary_graph(ports)


def build_story_pull_request_runtime(ports: StoryPullRequestPorts) -> Any:
    try:
        from kwikledgers_langgraph.graphs.story_pull_request import build_story_pull_request_graph
    except ModuleNotFoundError as error:
        if error.name == "langgraph":
            raise RuntimeError(
                "A dependencia 'langgraph' ainda nao esta instalada neste ambiente. "
                "Instale as dependencias do projeto 'kwikledgers_langgraph' para compilar o runtime."
            ) from error
        raise

    return build_story_pull_request_graph(ports)