from __future__ import annotations

from typing import Any, TypedDict

try:
    from typing import NotRequired
except ImportError:
    from typing_extensions import NotRequired


class UserIdentityContext(TypedDict, total=False):
    display_name: str
    resolved_email: str
    email_source: str


class StoryContext(TypedDict, total=False):
    story_id: int
    child_id: int
    repository: str
    branch_name: str
    status: str


class StoryPullRequestRequest(TypedDict, total=False):
    story_id: int
    repository_name: str
    source_branch: str
    what_was_changed: str
    affected_processes: str
    expected_impacts: str
    important_points: str
    mermaid_diagram: str
    related_work_item_ids: list[int]
    include_child_work_items: bool
    title: str
    tests_updated: bool
    tests_unchanged: bool
    new_library_name: str
    env_changes: str
    generated_migration_or_seed: str
    new_queue_or_command: str


class StoryBranchAssociationRequest(TypedDict, total=False):
    story_id: int
    repository_name: str
    source_branch: str
    related_work_item_ids: list[int]
    include_child_work_items: bool


class StoryStatusTransitionRequest(TypedDict, total=False):
    story_id: int
    target_status: str
    expected_current_status: str
    transition_exception_reason: str


class TaskClosureRequest(TypedDict, total=False):
    task_id: int
    start_time: str
    close_time: str
    completed_work_hours: float
    remaining_work_hours: float
    comment: str
    work_kind: str
    fix_summary: str
    origin_task_id: int


class DayEndClosureRequest(TypedDict, total=False):
    trigger: str
    task_id: int
    cutoff_time: str
    timezone: str
    now: str
    dry_run: bool
    create_continuation: bool


class ApprovalRequest(TypedDict, total=False):
    action: str
    preview: dict[str, Any]
    approved: bool
    run_id: str
    request_id: str
    approval_source: str


class ApprovalConfirmation(TypedDict, total=False):
    request_id: str
    approved: bool
    approval_source: str


class TrackingEvent(TypedDict, total=False):
    category: str
    message: str
    payload: dict[str, Any]


class ObservabilityEvent(TypedDict, total=False):
    node: str
    status: str
    message: str
    payload: dict[str, Any]
    at: str
    duration_ms: float | None


class ObservabilityContext(TypedDict, total=False):
    correlation_id: str
    active_run_id: str
    flow_name: str
    source: str
    started_at: str
    request_id: str
    owns_request: bool
    session_id: str
    workspace_id: str
    backend_name: str
    decisions: list[str]
    timeline: list[ObservabilityEvent]


class AgentState(TypedDict, total=False):
    user_identity: UserIdentityContext
    pull_request_request: StoryPullRequestRequest
    pull_request_preview: dict[str, Any]
    pull_request_result: dict[str, Any]
    branch_association_request: StoryBranchAssociationRequest
    branch_association_preview: dict[str, Any]
    branch_association_result: dict[str, Any]
    story_status_request: StoryStatusTransitionRequest
    story_status_preview: dict[str, Any]
    story_status_result: dict[str, Any]
    task_closure_request: TaskClosureRequest
    task_closure_preview: dict[str, Any]
    task_closure_result: dict[str, Any]
    day_end_closure_request: DayEndClosureRequest
    day_end_closure_context: dict[str, Any]
    day_end_closure_preview: dict[str, Any]
    day_end_closure_result: dict[str, Any]
    sprint_stories_request: dict[str, Any]
    sprint_stories_result: dict[str, Any]
    pending_approval: ApprovalRequest
    approval_confirmation: ApprovalConfirmation
    approval_resume_ready: bool
    tracking_events: list[TrackingEvent]
    observability: ObservabilityContext
    summary: dict[str, Any]
    source: str
    notification_enabled: bool
    approval_simulation_requested: bool
    errors: NotRequired[list[str]]


def append_decision(state: AgentState, decision: str) -> dict[str, Any]:
    observability = dict(state.get("observability", {}))
    decisions = list(observability.get("decisions", []))
    decisions.append(decision)
    observability["decisions"] = decisions
    return {"observability": observability}


def merge_observability(state: AgentState, updates: dict[str, Any]) -> dict[str, Any]:
    observability = dict(state.get("observability", {}))
    observability.update(updates)
    return {"observability": observability}


def append_tracking_event(
    state: AgentState,
    *,
    category: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    events = list(state.get("tracking_events", []))
    events.append(
        {
            "category": category,
            "message": message,
            "payload": payload or {},
        }
    )
    return {"tracking_events": events}


def append_error(state: AgentState, message: str) -> dict[str, Any]:
    errors = list(state.get("errors", []))
    errors.append(message)
    return {"errors": errors}