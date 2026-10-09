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


class StoryBranchAssociationPort(Protocol):
    def preview_story_branch_association(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return a normalized preview payload for a story branch association."""

    def associate_story_branch(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute the story branch association."""


class StoryStatusTransitionPort(Protocol):
    def preview_story_status_transition(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return a normalized preview payload for a story status transition."""

    def update_story_status(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute the story status transition."""


class TaskClosurePort(Protocol):
    def close_task(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute the atomic Azure updates required to close a task."""


class DayEndClosurePort(Protocol):
    def collect_day_context(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return active tasks and today's local activity entries."""

    def execute_day_end_closure(self, request: dict[str, Any]) -> dict[str, Any]:
        """Log hours, close the task and open its continuation task."""


class SprintStoriesProvider(Protocol):
    def fetch_sprint_stories_detailed(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return detailed stories for the active sprint."""


@dataclass(frozen=True)
class DailySummaryPorts:
    provider: DailySummaryProvider
    tracking: TrackingSyncPort
    identity: IdentityResolverPort | None = None
    notifications: NotificationPort | None = None


@dataclass(frozen=True)
class StoryPullRequestPorts:
    pull_requests: StoryPullRequestPort


@dataclass(frozen=True)
class StoryBranchAssociationPorts:
    branches: StoryBranchAssociationPort


@dataclass(frozen=True)
class StoryStatusTransitionPorts:
    statuses: StoryStatusTransitionPort


@dataclass(frozen=True)
class TaskClosurePorts:
    closures: TaskClosurePort


@dataclass(frozen=True)
class DayEndClosurePorts:
    day_end: DayEndClosurePort


@dataclass(frozen=True)
class SprintStoriesPorts:
    stories: SprintStoriesProvider


class LifecycleTrackingPort(Protocol):
    def record(self, event: dict[str, Any], *, key: str) -> None:
        """Durably append a local event, deduplicating by key; raise on failure."""

    def started_at(self, repository: str, task_id: int) -> str:
        """Read the authoritative recorded start time, not caller state."""

    def story_start_complete(self, context: dict[str, Any]) -> bool:
        """Confirm durable approved activation, branch and parent/child links
        completed for this story/task; never trust a caller boolean.
        """


class LifecycleApprovalPort(Protocol):
    def verify(self, pending: dict[str, Any], confirmation: Any) -> bool:
        """Verify authenticated human approval after preview, bound to its digest,
        request ID, run ID and action. Reject stale/replayed/foreign receipts.
        State booleans alone are never evidence of approval.
        """


class LifecycleContextPort(Protocol):
    def resolve(self, request: dict[str, Any]) -> dict[str, Any]:
        """Authorize repository and work items; return authoritative context.
        Include story_id, task_id, child_ids, repository, user_id, project,
        area, title, revision, status. Reject foreign tasks/repositories.
        """

    def snapshot(self, context: dict[str, Any]) -> dict[str, Any]:
        """Read current story revision/status/children for optimistic concurrency."""


class LifecycleBranchPort(Protocol):
    def preview(self, context: dict[str, Any], branch_type: str) -> dict[str, Any]:
        """Resolve the remote stage-pre-prod HEAD and story-owned branch.
        Return name, base, remote_head, reuse, dirty, associated_ids.
        Validate repository allowlist and existing branch ownership/provenance.
        Never stash, switch or create in preview.
        """

    def create_or_reuse(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        """Revalidate HEAD/ownership/clean checkout, then perform exact approved
        operation idempotently. Never run a command supplied by graph state.
        """

    def links(self, context: dict[str, Any]) -> dict[str, Any]:
        """Return story-owned branch name and currently associated work item IDs."""

    def associate(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        """Associate exactly the approved parent/children; return associated_ids."""


class LifecycleWorkItemPort(Protocol):
    def activate(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        """Apply exact assignment, Area and Active with revision precondition."""

    def awaiting_pr(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        """CAS Desenvolvimento to Aguardando PR, only after separate approval."""

    def hours(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        """Apply approved hours without changing status or Remaining Work."""

    def close(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        """Apply approved Closed; preserve omitted/null Remaining Work."""


class LifecyclePullRequestPort(Protocol):
    def preview(self, context: dict[str, Any], branch: str) -> dict[str, Any]:
        """Read/fill repository template and append Mermaid. Return body,
        template_path, source_branch, target_branch, is_draft and title.
        """

    def create_draft(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        """Idempotently create exact approved Draft PR; return url and is_draft."""


class LifecycleTaskPort(Protocol):
    def plan(self, context: dict[str, Any]) -> dict[str, Any]:
        """Register a trusted plan; return plan_id, files, logic, tests, risks.
        plan_id must resolve to an authorized implementation, never state code.
        """

    def implement(self, plan_id: str, *, key: str) -> dict[str, Any]:
        """Execute only the server-owned authorized plan identified by plan_id."""

    def test(self, plan_id: str, *, key: str) -> dict[str, Any]:
        """Run server-owned checks; return passed bool and nonempty evidence."""

    def commit(self, plan_id: str, task_id: int, *, key: str) -> dict[str, Any]:
        """Commit only authorized plan files after green checks; return commit.
        Use KL-{task_id}; reject unrelated staged files and state shell commands.
        """


class LifecycleClockPort(Protocol):
    def now(self) -> str:
        """Return an aware ISO-8601 timestamp."""


@dataclass(frozen=True)
class LifecyclePorts:
    context: LifecycleContextPort
    tracking: LifecycleTrackingPort
    approvals: LifecycleApprovalPort
    clock: LifecycleClockPort
    branches: LifecycleBranchPort | None = None
    work_items: LifecycleWorkItemPort | None = None
    pull_requests: LifecyclePullRequestPort | None = None
    tasks: LifecycleTaskPort | None = None


class TechnicalDebtPreparationPort(Protocol):
    def resolve(self, request: dict[str, Any]) -> dict[str, Any]:
        """Authorize repository and explicit iteration; return normalized request,
        task_acceptance_supported and authoritative current_sprint_number,
        current_year, iteration_root for a next_sprint request. Workflow policy
        stays in LangGraph, not in a FastMCP handler.
        """

    def find_similar(self, repository_name: str, title: str, context: str) -> dict[str, Any]:
        """Return exact_match bool and similar_items list, without Azure writes."""

    def save_repository_context(self, repository: str, similarity: dict[str, Any], *, key: str) -> str:
        """Persist in AI_Tracking/Repo_Work_Item_Context/technical_debts; return path."""

    def save_preview_cache(self, repository: str, preview: dict[str, Any], *, key: str) -> str:
        """Persist full preview in technical_debt_previews/<repo>.json."""


@dataclass(frozen=True)
class TechnicalDebtPreparationPorts:
    preparation: TechnicalDebtPreparationPort
    tracking: LifecycleTrackingPort


class ProcessSnapshotPort(Protocol):
    def fetch_snapshot(self) -> dict[str, Any]:
        """Read normalized items, including children/all work item types/all task
        statuses, plus aware sprint_end and PR created_at/reviewed/author fields.
        """


@dataclass(frozen=True)
class ProcessSnapshotPorts:
    provider: ProcessSnapshotPort
    tracking: LifecycleTrackingPort
    clock: LifecycleClockPort