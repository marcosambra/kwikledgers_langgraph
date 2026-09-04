from __future__ import annotations

import os

from kwikledgers_langgraph.adapters import (
    DailySummaryPorts,
    SprintStoriesPorts,
    StoryBranchAssociationPorts,
    StoryPullRequestPorts,
    StoryStatusTransitionPorts,
    TaskClosurePorts,
)
from kwikledgers_langgraph.legacy_adapters import (
    EnvironmentIdentityResolverAdapter,
    LegacyDailySummaryProvider,
    LegacyNotificationAdapter,
    LegacySprintStoriesProvider,
    LegacyStoryBranchAssociationAdapter,
    LegacyStoryPullRequestAdapter,
    LegacyStoryStatusTransitionAdapter,
    LegacyTaskClosureAdapter,
    LegacyTrackingSyncAdapter,
    NoOpNotificationAdapter,
)


LEGACY_BACKEND = "legacy"
FASTMCP_BACKEND = "fastmcp"
SUPPORTED_BACKENDS = (LEGACY_BACKEND, FASTMCP_BACKEND)
BACKEND_ENV_VAR = "KWIKLEDGERS_LANGGRAPH_BACKEND"


class BackendSelectionError(RuntimeError):
    """Raised when a backend is invalid or not yet available for a capability."""


def resolve_backend_name(explicit_backend: str | None = None) -> str:
    candidate = explicit_backend or os.environ.get(BACKEND_ENV_VAR, LEGACY_BACKEND)
    backend_name = str(candidate).strip().lower()
    if backend_name not in SUPPORTED_BACKENDS:
        raise BackendSelectionError(
            "Backend invalido para o runtime LangGraph: "
            f"'{backend_name}'. Use um dos valores suportados: {', '.join(SUPPORTED_BACKENDS)}."
        )
    return backend_name


def build_daily_summary_ports(*, backend_name: str, notifications_enabled: bool) -> DailySummaryPorts:
    if backend_name == LEGACY_BACKEND:
        notifications = LegacyNotificationAdapter() if notifications_enabled else NoOpNotificationAdapter()
        return DailySummaryPorts(
            identity=EnvironmentIdentityResolverAdapter(),
            provider=LegacyDailySummaryProvider(),
            tracking=LegacyTrackingSyncAdapter(),
            notifications=notifications,
        )

    if backend_name == FASTMCP_BACKEND:
        raise BackendSelectionError(
            "O backend 'fastmcp' foi pausado por decisao atual da arquitetura. "
            "Mantenha o runtime em 'legacy' e concentre a evolucao no LangGraph."
        )

    raise BackendSelectionError("Backend invalido para a capability 'daily_summary'.")


def build_story_pull_request_ports(*, backend_name: str) -> StoryPullRequestPorts:
    if backend_name == LEGACY_BACKEND:
        return StoryPullRequestPorts(pull_requests=LegacyStoryPullRequestAdapter())

    raise BackendSelectionError(
        "O backend 'fastmcp' ainda nao esta disponivel para a capability 'story_pull_request'."
    )


def build_story_branch_association_ports(*, backend_name: str) -> StoryBranchAssociationPorts:
    if backend_name == LEGACY_BACKEND:
        return StoryBranchAssociationPorts(branches=LegacyStoryBranchAssociationAdapter())

    raise BackendSelectionError(
        "O backend 'fastmcp' ainda nao esta disponivel para a capability 'story_branch_association'."
    )


def build_story_status_transition_ports(*, backend_name: str) -> StoryStatusTransitionPorts:
    if backend_name == LEGACY_BACKEND:
        return StoryStatusTransitionPorts(statuses=LegacyStoryStatusTransitionAdapter())

    raise BackendSelectionError(
        "O backend 'fastmcp' ainda nao esta disponivel para a capability 'story_status_transition'."
    )


def build_task_closure_ports(*, backend_name: str) -> TaskClosurePorts:
    if backend_name == LEGACY_BACKEND:
        return TaskClosurePorts(closures=LegacyTaskClosureAdapter())

    raise BackendSelectionError(
        "O backend 'fastmcp' ainda nao esta disponivel para a capability 'task_closure'."
    )


def build_sprint_stories_ports(*, backend_name: str) -> SprintStoriesPorts:
    if backend_name == LEGACY_BACKEND:
        return SprintStoriesPorts(stories=LegacySprintStoriesProvider())

    raise BackendSelectionError(
        "O backend 'fastmcp' ainda nao esta disponivel para a capability 'sprint_stories'."
    )