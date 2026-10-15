from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kwikledgers_langgraph.fastmcp_adapters import ExecutionCapabilityProvider
    from kwikledgers_langgraph.fastmcp_client import CapabilityClient

from kwikledgers_langgraph.adapters import (
    DailySummaryPorts,
    DayEndClosurePorts,
    SprintStoriesPorts,
    StoryBranchAssociationPorts,
    StoryPullRequestPorts,
    StoryStatusTransitionPorts,
    TaskClosurePorts,
)
from kwikledgers_langgraph.runtime_adapters import (
    EnvironmentIdentityResolverAdapter,
    NoOpNotificationAdapter,
)


FASTMCP_BACKEND = "fastmcp"
SUPPORTED_BACKENDS = (FASTMCP_BACKEND,)
BACKEND_ENV_VAR = "KWIKLEDGERS_LANGGRAPH_BACKEND"


class BackendSelectionError(RuntimeError):
    """Raised when a backend is invalid or not yet available for a capability."""


def build_day_end_closure_ports(
    *, backend_name: str, client: CapabilityClient | None = None,
) -> DayEndClosurePorts:
    if backend_name == FASTMCP_BACKEND:
        from kwikledgers_langgraph.fastmcp_adapters import FastMCPDayEndClosureAdapter

        return DayEndClosurePorts(day_end=FastMCPDayEndClosureAdapter(client))
    raise BackendSelectionError("Backend indisponivel para a capability 'day_end_closure'.")


def resolve_backend_name(explicit_backend: str | None = None) -> str:
    candidate = explicit_backend or os.environ.get(BACKEND_ENV_VAR, FASTMCP_BACKEND)
    backend_name = str(candidate).strip().lower()
    if backend_name not in SUPPORTED_BACKENDS:
        raise BackendSelectionError(
            "Backend invalido para o runtime LangGraph: "
            f"'{backend_name}'. Use um dos valores suportados: {', '.join(SUPPORTED_BACKENDS)}."
        )
    return backend_name


def build_daily_summary_ports(
    *, backend_name: str, notifications_enabled: bool, client: CapabilityClient | None = None,
    execution_capability_provider: ExecutionCapabilityProvider | None = None,
) -> DailySummaryPorts:
    if backend_name == FASTMCP_BACKEND:
        from kwikledgers_langgraph.fastmcp_adapters import (
            FastMCPDailySummaryProvider, FastMCPNotificationAdapter, FastMCPTrackingSyncAdapter,
        )
        from kwikledgers_langgraph.fastmcp_client import FastMCPClient

        capability_client = client if client is not None else FastMCPClient()
        return DailySummaryPorts(
            identity=EnvironmentIdentityResolverAdapter(),
            provider=FastMCPDailySummaryProvider(capability_client),
            tracking=FastMCPTrackingSyncAdapter(capability_client, execution_capability_provider),
            notifications=(FastMCPNotificationAdapter(capability_client, execution_capability_provider)
                           if notifications_enabled else NoOpNotificationAdapter()),
        )

    raise BackendSelectionError("Backend invalido para a capability 'daily_summary'.")


def build_story_pull_request_ports(
    *, backend_name: str, client: CapabilityClient | None = None,
    execution_capability_provider: ExecutionCapabilityProvider | None = None,
) -> StoryPullRequestPorts:
    if backend_name == FASTMCP_BACKEND:
        from kwikledgers_langgraph.fastmcp_adapters import FastMCPStoryPullRequestAdapter

        return StoryPullRequestPorts(pull_requests=FastMCPStoryPullRequestAdapter(client, execution_capability_provider))

    raise BackendSelectionError(
        "Backend invalido para a capability 'story_pull_request'."
    )


def build_story_branch_association_ports(
    *, backend_name: str, client: CapabilityClient | None = None,
    execution_capability_provider: ExecutionCapabilityProvider | None = None,
) -> StoryBranchAssociationPorts:
    if backend_name == FASTMCP_BACKEND:
        from kwikledgers_langgraph.fastmcp_adapters import FastMCPStoryBranchAssociationAdapter

        return StoryBranchAssociationPorts(branches=FastMCPStoryBranchAssociationAdapter(client, execution_capability_provider))

    raise BackendSelectionError(
        "Backend invalido para a capability 'story_branch_association'."
    )


def build_story_status_transition_ports(
    *, backend_name: str, client: CapabilityClient | None = None,
    execution_capability_provider: ExecutionCapabilityProvider | None = None,
) -> StoryStatusTransitionPorts:
    if backend_name == FASTMCP_BACKEND:
        from kwikledgers_langgraph.fastmcp_adapters import FastMCPStoryStatusTransitionAdapter

        return StoryStatusTransitionPorts(statuses=FastMCPStoryStatusTransitionAdapter(client, execution_capability_provider))

    raise BackendSelectionError(
        "Backend invalido para a capability 'story_status_transition'."
    )


def build_task_closure_ports(
    *, backend_name: str, client: CapabilityClient | None = None,
    execution_capability_provider: ExecutionCapabilityProvider | None = None,
) -> TaskClosurePorts:
    if backend_name == FASTMCP_BACKEND:
        from kwikledgers_langgraph.fastmcp_adapters import FastMCPTaskClosureAdapter

        return TaskClosurePorts(closures=FastMCPTaskClosureAdapter(client, execution_capability_provider))

    raise BackendSelectionError(
        "Backend invalido para a capability 'task_closure'."
    )


def build_sprint_stories_ports(*, backend_name: str, client: CapabilityClient | None = None) -> SprintStoriesPorts:
    if backend_name == FASTMCP_BACKEND:
        from kwikledgers_langgraph.fastmcp_adapters import FastMCPSprintStoriesProvider

        return SprintStoriesPorts(stories=FastMCPSprintStoriesProvider(client))

    raise BackendSelectionError(
        "Backend invalido para a capability 'sprint_stories'."
    )