from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class DailySummaryProvider(Protocol):
    def fetch_daily_summary(self) -> dict[str, Any]:
        """Return the normalized daily summary payload."""


class TrackingSyncPort(Protocol):
    def sync_daily_tracking(self, summary: dict[str, Any], source: str) -> Any:
        """Persist local tracking side effects for a summary payload."""


class NotificationPort(Protocol):
    def send_daily_summary_notification(self, summary: dict[str, Any]) -> Any:
        """Emit a user-facing notification for the summary result."""


class IdentityResolverPort(Protocol):
    def resolve_current_user_identity(self) -> dict[str, Any]:
        """Resolve the minimal current user identity context for the runtime."""


class StoryPullRequestPort(Protocol):
    def preview_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return a normalized preview payload for a story draft pull request."""

    def create_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute the story draft pull request creation."""


@dataclass(frozen=True)
class DailySummaryPorts:
    provider: DailySummaryProvider
    tracking: TrackingSyncPort
    identity: IdentityResolverPort | None = None
    notifications: NotificationPort | None = None


@dataclass(frozen=True)
class StoryPullRequestPorts:
    pull_requests: StoryPullRequestPort