from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from kwikledgers_langgraph.adapters import (
    DailySummaryPorts,
    StoryBranchAssociationPorts,
    StoryPullRequestPorts,
    StoryStatusTransitionPorts,
)
from kwikledgers_langgraph.legacy_adapters import (
    EnvironmentIdentityResolverAdapter,
    LegacyDailySummaryProvider,
    LegacyNotificationAdapter,
    LegacyStoryBranchAssociationAdapter,
    LegacyStoryPullRequestAdapter,
    LegacyStoryStatusTransitionAdapter,
    LegacyTrackingSyncAdapter,
    NoOpNotificationAdapter,
)


LEGACY_BACKEND = "legacy"
FASTMCP_BACKEND = "fastmcp"
SUPPORTED_BACKENDS = (LEGACY_BACKEND, FASTMCP_BACKEND)
BACKEND_ENV_VAR = "KWIKLEDGERS_LANGGRAPH_BACKEND"
FASTMCP_SRC_ENV_VAR = "KWIKLEDGERS_FASTMCP_SRC_PATH"


class BackendSelectionError(RuntimeError):
    """Raised when a backend is invalid or not yet available for a capability."""


def _ensure_fastmcp_import_path() -> None:
    explicit_fastmcp_src_path = os.environ.get(FASTMCP_SRC_ENV_VAR)
    if explicit_fastmcp_src_path:
        candidate = Path(explicit_fastmcp_src_path).expanduser().resolve()
        if candidate.is_dir():
            fastmcp_src_path = str(candidate)
            if fastmcp_src_path not in sys.path:
                sys.path.insert(0, fastmcp_src_path)
            return
        raise BackendSelectionError(
            "A variavel KWIKLEDGERS_FASTMCP_SRC_PATH foi definida, mas nao aponta para um diretorio valido."
        )

    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "kwikledgers_fastmcp" / "src"
        if candidate.is_dir():
            fastmcp_src_path = str(candidate)
            if fastmcp_src_path not in sys.path:
                sys.path.insert(0, fastmcp_src_path)
            return


def _build_fastmcp_daily_summary_provider() -> object:
    module = _load_fastmcp_server_module()
    return module.build_daily_summary_provider()


def _build_fastmcp_tracking_sync_adapter() -> object:
    module = _load_fastmcp_server_module()
    return module.build_tracking_sync_adapter()


def _build_fastmcp_notification_adapter() -> object:
    module = _load_fastmcp_server_module()
    return module.build_notification_adapter()


def _load_fastmcp_server_module() -> object:
    try:
        module = importlib.import_module("kwikledgers_fastmcp.server")
    except ModuleNotFoundError:
        _ensure_fastmcp_import_path()
        try:
            module = importlib.import_module("kwikledgers_fastmcp.server")
        except ModuleNotFoundError as error:
            raise BackendSelectionError(
                "O backend 'fastmcp' para 'daily_summary' exige o pacote kwikledgers_fastmcp instalado ou acessivel em src/."
            ) from error

    return module


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
        notifications = _build_fastmcp_notification_adapter() if notifications_enabled else NoOpNotificationAdapter()
        return DailySummaryPorts(
            identity=EnvironmentIdentityResolverAdapter(),
            provider=_build_fastmcp_daily_summary_provider(),
            tracking=_build_fastmcp_tracking_sync_adapter(),
            notifications=notifications,
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