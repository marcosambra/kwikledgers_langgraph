from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
import math
from typing import Any

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.fastmcp_client import CapabilityClient
from kwikledgers_langgraph.graphs.lifecycle_engine import LifecycleStep, build_lifecycle_graph
from kwikledgers_langgraph.lifecycle_adapters import (
    LifecycleGateway, active_state, require_approved_preview,
)


@dataclass(frozen=True)
class AtomicSchema:
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()


READ_SCHEMAS = {
    "get_active_user": AtomicSchema(),
    "get_my_work_items": AtomicSchema(),
    "get_my_blocked_items": AtomicSchema(),
    "get_my_daily_summary": AtomicSchema(),
    "get_user_stories": AtomicSchema(("email",)),
    "get_sprint_stories": AtomicSchema(),
    "get_sprint_stories_detailed": AtomicSchema(),
    "get_story_details": AtomicSchema(("story_id",)),
    "get_sprint_work_report": AtomicSchema((), ("sprint_path", "email")),
    "get_open_prs": AtomicSchema(("email",)),
    "find_similar_technical_debts": AtomicSchema(("repository_name", "title"), ("description", "limit")),
    "read_tracking_snapshot": AtomicSchema(),
    "get_upcoming_deadlines": AtomicSchema((), ("days",)),
}
BRANCH_SCHEMA = AtomicSchema(("story_id", "repository_name", "source_branch"),
                             ("related_work_item_ids", "include_child_work_items"))
PREVIEW_SCHEMAS = {
    "preview_story_branch_association": BRANCH_SCHEMA,
    "preview_story_pull_request": AtomicSchema(
        BRANCH_SCHEMA.required + ("what_was_changed", "affected_processes", "expected_impacts",
                                  "important_points", "mermaid_diagram"),
        BRANCH_SCHEMA.optional + ("target_branch", "title", "tests_updated", "tests_unchanged",
                                  "new_library_name", "env_changes", "generated_migration_or_seed",
                                  "new_queue_or_command")),
}
MUTATION_SCHEMAS = {
    "add_story_comment": AtomicSchema(("story_id", "comment")),
    "move_work_item_to_next_sprint": AtomicSchema(("work_item_id",), ("comment",)),
    "update_work_item_content": AtomicSchema(("work_item_id",), ("description", "acceptance_criteria", "comment")),
    "create_calendar_event": AtomicSchema(("title", "start", "end"), ("description", "reminder_minutes")),
    "schedule_reminder": AtomicSchema(("title", "remind_at", "message")),
    "send_notification": AtomicSchema(("title", "message"), ("urgency",)),
    "update_task_control": AtomicSchema(("summary",)),
    "record_ai_metrics": AtomicSchema(("entry_title", "summary", "actions_taken"),
                                      ("remaining_story_points", "blocked_items")),
    "save_technical_debt_preview_cache": AtomicSchema(("repository_name", "title"),
        ("description", "acceptance_criteria", "target_iteration_path", "story_points",
         "implementation_status", "implementation_notes", "child_tasks")),
}
LIFECYCLE_ROUTES = {
    "associate_story_branch": "story_start",
    "create_story_pull_request": "story_completion",
    "create_work_item": "story creation or technical_debt_execute with a signed prepared preview",
    "update_work_item_effort": "task_cycle (recorded start, ceil hours and independent closure receipt)",
    "update_story_status": "canonical story status lifecycle; story_completion for Aguardando PR",
    "sync_daily_tracking": "daily_summary",
    "append_daily_action_log": "lifecycle_tracking",
}
ATOMIC_ACTION_CAPABILITIES = {f"atomic_{name}": name for name in MUTATION_SCHEMAS}
CAPABILITY_CATALOG = {
    **{name: "read" for name in READ_SCHEMAS},
    **{name: "preview" for name in PREVIEW_SCHEMAS},
    **{name: "approved_atomic" for name in MUTATION_SCHEMAS},
    **{name: "lifecycle_required" for name in LIFECYCLE_ROUTES},
}
REVISION_TARGETS = {"add_story_comment": "story_id", "move_work_item_to_next_sprint": "work_item_id",
                    "update_work_item_content": "work_item_id"}
TEXT_FIELDS = frozenset({
    "email", "repository_name", "title", "description", "acceptance_criteria", "comment",
    "source_branch", "target_branch", "what_was_changed", "affected_processes", "expected_impacts",
    "important_points", "mermaid_diagram", "new_library_name", "env_changes",
    "generated_migration_or_seed", "new_queue_or_command", "start", "end", "remind_at",
    "message", "urgency", "summary", "entry_title", "target_iteration_path",
    "implementation_status", "implementation_notes", "sprint_path",
})
BOOLEAN_FIELDS = frozenset({"include_child_work_items", "tests_updated", "tests_unchanged"})
INTEGER_FIELDS = frozenset({"story_id", "work_item_id", "days", "limit", "reminder_minutes", "blocked_items"})
FIBONACCI = frozenset({1, 2, 3, 5, 8, 13, 21})


class LifecycleRequired(ValueError):
    pass


def _positive_id(value: Any) -> bool:
    return type(value) is int and value > 0


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _aware_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Calendar timestamps must be timezone-aware")
    return parsed


def _child_tasks(value: Any) -> None:
    allowed = {"title", "description", "acceptance_criteria", "developer_suggestion", "remaining_work_hours"}
    if not isinstance(value, list):
        raise ValueError("child_tasks must be an array")
    for item in value:
        if (not isinstance(item, dict) or set(item) - allowed or "title" not in item
                or not isinstance(item["title"], str) or not item["title"].strip()):
            raise ValueError("Strict technical debt child schema required")
        for field, data in item.items():
            if field == "remaining_work_hours":
                if not _number(data):
                    raise ValueError("Invalid child estimate")
            elif not isinstance(data, str):
                raise ValueError("Child content must be text")


def validate_arguments(name: str, arguments: dict[str, Any], *, technical: bool = False) -> dict[str, Any]:
    if name in LIFECYCLE_ROUTES:
        raise LifecycleRequired(f"{name} requires {LIFECYCLE_ROUTES[name]}")
    schemas = {**READ_SCHEMAS, **PREVIEW_SCHEMAS, **MUTATION_SCHEMAS}
    if name not in schemas:
        raise ValueError("Unknown operational capability")
    schema = schemas[name]
    if not isinstance(arguments, dict) or any(not isinstance(field, str) for field in arguments):
        raise ValueError("Arguments must be a JSON object")
    allowed = set(schema.required + schema.optional)
    if technical and name in REVISION_TARGETS:
        allowed.add("expected_revision")
    if set(arguments) - allowed or set(schema.required) - set(arguments):
        raise ValueError("Unexpected or missing atomic schema fields")
    for field, value in arguments.items():
        if field in TEXT_FIELDS:
            if not isinstance(value, str):
                raise ValueError(f"{field} must be text")
            if field in schema.required and not value.strip():
                raise ValueError(f"{field} must not be empty")
        elif field in BOOLEAN_FIELDS:
            if type(value) is not bool:
                raise ValueError(f"{field} must be boolean")
        elif field in INTEGER_FIELDS or field == "expected_revision":
            minimum = 0 if field in {"reminder_minutes", "blocked_items"} else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{field} must be an integer >= {minimum}")
        elif field in {"story_points", "remaining_story_points"}:
            if not _number(value):
                raise ValueError(f"{field} must be finite and nonnegative")
            if field == "story_points" and value not in FIBONACCI:
                raise ValueError("Technical debt points require Fibonacci sizing")
        elif field == "related_work_item_ids":
            if (not isinstance(value, list) or not all(_positive_id(item) for item in value)
                    or len(set(value)) != len(value)):
                raise ValueError("Unique positive related work item IDs required")
        elif field == "actions_taken":
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise ValueError("actions_taken must be an array of nonempty text")
        elif field == "child_tasks":
            _child_tasks(value)
        else:
            raise ValueError("Unvalidated atomic field")
    if name == "update_work_item_content" and not any(
            isinstance(arguments.get(field), str) and arguments[field].strip()
            for field in ("description", "acceptance_criteria", "comment")):
        raise ValueError("A nonempty content edit is required")
    if name == "send_notification" and arguments.get("urgency", "normal") not in {"normal", "high"}:
        raise ValueError("Invalid notification urgency")
    if name == "update_task_control":
        summary = json.loads(arguments["summary"])
        if not isinstance(summary, dict):
            raise ValueError("Task control summary must encode a JSON object")
        json.dumps(summary, allow_nan=False)
    if name == "create_calendar_event":
        if _aware_time(arguments["end"]) <= _aware_time(arguments["start"]):
            raise ValueError("Calendar end must follow start")
    if name == "schedule_reminder":
        _aware_time(arguments["remind_at"])
    json.dumps(arguments, allow_nan=False)
    return deepcopy(arguments)


def read_capability(client: CapabilityClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name not in READ_SCHEMAS and name not in PREVIEW_SCHEMAS:
        if name in LIFECYCLE_ROUTES:
            raise LifecycleRequired(f"{name} requires {LIFECYCLE_ROUTES[name]}")
        raise ValueError("Only read and preview capabilities are allowed")
    result = client.call(name, validate_arguments(name, arguments))
    if not isinstance(result, dict):
        raise ValueError("Structured native result required")
    return deepcopy(result)


def _details(client: CapabilityClient, identifier: int) -> dict[str, Any]:
    result = client.call("get_story_details", {"story_id": identifier})
    identity = result.get("id", result.get("story_id")) if isinstance(result, dict) else None
    if not _positive_id(identity) or identity != identifier:
        raise ValueError("Authoritative work item identity required")
    revision = result.get("revision", result.get("rev"))
    if not _positive_id(revision):
        raise ValueError("Positive native revision required")
    fields = result.get("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("Structured work item fields required")
    return {**result, "revision": revision,
            "work_item_type": result.get("work_item_type", fields.get("System.WorkItemType"))}


def _content_policy(name: str, details: dict[str, Any]) -> None:
    if name != "update_work_item_content":
        return
    kind = details.get("work_item_type")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("Native work item type required for content edits")
    if kind.casefold().replace("_", " ").replace("-", " ") not in {"task", "bug", "user story", "story"}:
        raise LifecycleRequired("Work item content requires its specific lifecycle (including technical debt)")


class AtomicLifecycleGateway(LifecycleGateway):
    def execute(self, action: str, name: str, arguments: dict[str, Any], key: str) -> dict[str, Any]:
        if ATOMIC_ACTION_CAPABILITIES.get(action) != name:
            raise ValueError("Capability not allowed for this atomic lifecycle action")
        require_approved_preview(action, arguments)
        payload = validate_arguments(name, arguments, technical=True)
        if self.execution_issuer is None:
            raise RuntimeError("MAIN must provide issue_lifecycle_execution_capability for atomic actions")

        def invoke() -> dict[str, Any]:
            if name in REVISION_TARGETS:
                identifier = payload[REVISION_TARGETS[name]]
                fresh = _details(self.client, identifier)
                _content_policy(name, fresh)
                if fresh["revision"] != payload.get("expected_revision"):
                    raise ValueError("Native revision changed; obtain a new preview and receipt")
            state = active_state(action)
            token = self.execution_issuer(state, action=action, capability_name=name, arguments=deepcopy(payload))
            if not isinstance(token, str) or not token.strip():
                raise ValueError("Execution capability missing")
            return self.client.call(name, {**payload, "execution_capability": token})

        return self.journal.once(key, payload, invoke)


def build_atomic_runtime(name: str, ports: LifecyclePorts, *, checkpointer: Any,
                         arguments: dict[str, Any], gateway: LifecycleGateway | None = None) -> Any:
    if name in LIFECYCLE_ROUTES:
        raise LifecycleRequired(f"{name} requires {LIFECYCLE_ROUTES[name]}")
    if name not in MUTATION_SCHEMAS:
        raise ValueError("Approved atomic mutation required")
    supplied = validate_arguments(name, arguments)
    source = gateway if gateway is not None else getattr(ports.work_items, "gateway", None)
    if not isinstance(source, LifecycleGateway):
        raise ValueError("Operational lifecycle gateway required")
    atomic = AtomicLifecycleGateway(source.client, source.journal, source.execution_issuer, source.runtime_issuer)
    action = f"atomic_{name}"

    def resolve(request: dict[str, Any]) -> dict[str, Any]:
        if "repository_name" in supplied:
            if ports.branches is None:
                raise ValueError("Operator-authorized repository required")
            ports.branches.path(supplied["repository_name"])
        return {"atomic_capability": name, "atomic_arguments": deepcopy(supplied)}

    def prepare(state: dict[str, Any]) -> dict[str, Any]:
        context = state["context"]
        if context.get("atomic_capability") != name or context.get("atomic_arguments") != supplied:
            raise ValueError("Atomic checkpoint belongs to different arguments or capability")
        payload = deepcopy(supplied)
        if name in REVISION_TARGETS:
            details = _details(atomic.client, payload[REVISION_TARGETS[name]])
            _content_policy(name, details)
            payload["expected_revision"] = details["revision"]
        return validate_arguments(name, payload, technical=True)

    def execute(state: dict[str, Any], preview: dict[str, Any], key: str) -> dict[str, Any]:
        context = state["context"]
        if context.get("atomic_capability") != name or context.get("atomic_arguments") != supplied:
            raise ValueError("Atomic checkpoint context changed")
        expected = {field: value for field, value in preview.items() if field != "expected_revision"}
        if expected != supplied:
            raise ValueError("Atomic preview differs from requested technical payload")
        return atomic.execute(action, name, preview, key)

    return build_lifecycle_graph(ports, (LifecycleStep(action, prepare, execute),),
        name=action, checkpointer=checkpointer, context_resolver=resolve,
        required_context=("atomic_capability",))