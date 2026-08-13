from __future__ import annotations

from typing import Any

from kwikledgers_langgraph.adapters import (
    DailySummaryPorts,
    StoryBranchAssociationPorts,
    StoryPullRequestPorts,
    StoryStatusTransitionPorts,
    TaskClosurePorts,
)


MUTABLE_RUNTIME_BUILDERS: dict[str, str] = {
    "story_pull_request": "build_story_pull_request_runtime",
    "story_branch_association": "build_story_branch_association_runtime",
    "story_status_transition": "build_story_status_transition_runtime",
    "task_closure": "build_task_closure_runtime",
}


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


def build_story_branch_association_runtime(ports: StoryBranchAssociationPorts) -> Any:
    try:
        from kwikledgers_langgraph.graphs.story_branch_association import build_story_branch_association_graph
    except ModuleNotFoundError as error:
        if error.name == "langgraph":
            raise RuntimeError(
                "A dependencia 'langgraph' ainda nao esta instalada neste ambiente. "
                "Instale as dependencias do projeto 'kwikledgers_langgraph' para compilar o runtime."
            ) from error
        raise

    return build_story_branch_association_graph(ports)


def build_story_status_transition_runtime(ports: StoryStatusTransitionPorts) -> Any:
    try:
        from kwikledgers_langgraph.graphs.story_status_transition import build_story_status_transition_graph
    except ModuleNotFoundError as error:
        if error.name == "langgraph":
            raise RuntimeError(
                "A dependencia 'langgraph' ainda nao esta instalada neste ambiente. "
                "Instale as dependencias do projeto 'kwikledgers_langgraph' para compilar o runtime."
            ) from error
        raise

    return build_story_status_transition_graph(ports)


def build_task_closure_runtime(ports: TaskClosurePorts) -> Any:
    try:
        from kwikledgers_langgraph.graphs.task_closure import build_task_closure_graph
    except ModuleNotFoundError as error:
        if error.name == "langgraph":
            raise RuntimeError(
                "A dependencia 'langgraph' ainda nao esta instalada neste ambiente. "
                "Instale as dependencias do projeto 'kwikledgers_langgraph' para compilar o runtime."
            ) from error
        raise

    return build_task_closure_graph(ports)


def list_mutable_runtime_builders() -> dict[str, str]:
    return dict(MUTABLE_RUNTIME_BUILDERS)