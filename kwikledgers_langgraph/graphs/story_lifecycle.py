from __future__ import annotations

from datetime import datetime
import re
from typing import Any
from urllib.parse import urlparse

from langgraph.types import interrupt

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.graphs.lifecycle_engine import LifecycleStep, build_lifecycle_graph
from kwikledgers_langgraph.lifecycle_state import LifecycleState


def aware_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return result


def required_ids(context: dict[str, Any]) -> list[int]:
    children = context["child_ids"]
    identifiers = [context["story_id"], *children]
    if any(type(identifier) is not int or identifier <= 0 for identifier in identifiers):
        raise ValueError("Invalid authorized work item IDs")
    if context["task_id"] not in children:
        raise ValueError("Task is not a child of this story")
    return list(dict.fromkeys(identifiers))


def _require_ports(ports: LifecyclePorts, *names: str) -> None:
    if any(getattr(ports, name) is None for name in names):
        raise ValueError(f"Required lifecycle ports: {', '.join(names)}")


def _check_revision(ports: LifecyclePorts, state: LifecycleState, preview: dict[str, Any]) -> None:
    current = ports.context.snapshot(state["context"])
    if current.get("revision") != preview["expected_revision"]:
        raise ValueError("Work item changed after preview")


def _validate_branch(preview: dict[str, Any], context: dict[str, Any], branch_type: str | None = None) -> None:
    pattern = rf'(feature|hotfix)/KL-{context["story_id"]}-[a-z0-9]+(?:-[a-z0-9]+)*'
    if not re.fullmatch(pattern, preview.get("name", "")):
        raise ValueError("Invalid story-owned branch name")
    if branch_type and not preview["name"].startswith(f"{branch_type}/"):
        raise ValueError("Branch type differs from human selection")
    if preview.get("base") != "stage-pre-prod" or not preview.get("remote_head"):
        raise ValueError("Branch must use remote stage-pre-prod HEAD")
    if preview.get("dirty") is not False:
        raise ValueError("Dirty checkout requires separately approved handling; no automatic stash")
    if preview.get("repository") != context["repository"] or preview.get("story_id") != context["story_id"]:
        raise ValueError("Branch ownership mismatch")


def _verify_links(ports: LifecyclePorts, context: dict[str, Any], branch: str) -> None:
    snapshot = ports.context.snapshot(context)
    if set(snapshot.get("child_ids", [])) != set(context["child_ids"]):
        raise ValueError("Story hierarchy changed; refresh authorized context")
    links = ports.branches.links(context)
    if links.get("name") != branch or not set(required_ids(context)) <= set(links.get("associated_ids", [])):
        raise ValueError("Parent/child branch associations incomplete")


def _association_step(ports: LifecyclePorts, *, starting: bool) -> LifecycleStep:
    def prepare(state: LifecycleState) -> dict[str, Any]:
        context = state["context"]
        snapshot = ports.context.snapshot(context)
        if set(snapshot.get("child_ids", [])) != set(context["child_ids"]):
            raise ValueError("Story hierarchy changed before branch association")
        links = ports.branches.links(context)
        branch = state["results"]["branch"]["name"] if starting else links.get("name", "")
        if not re.fullmatch(rf'(feature|hotfix)/KL-{context["story_id"]}-[a-z0-9]+(?:-[a-z0-9]+)*', branch):
            raise ValueError("Missing story-owned branch")
        if links.get("name") != branch:
            raise ValueError("Association preview differs from story-owned branch")
        preview = {"repository": context["repository"], "story_id": context["story_id"],
                   "name": branch, "associated_ids": required_ids(context)}
        revisions = links.get("expected_revisions")
        if (not isinstance(revisions, dict) or set(revisions) != {str(item) for item in preview["associated_ids"]}
                or any(type(value) is not int or value <= 0 for value in revisions.values())):
            raise ValueError("Association requires revisions for every parent/child")
        preview["expected_revisions"] = revisions
        return preview

    def execute(state: LifecycleState, preview: dict[str, Any], key: str) -> dict[str, Any]:
        if prepare(state) != preview:
            raise ValueError("Story branch/hierarchy changed after association preview")
        result = ports.branches.associate(preview, key=key)
        _verify_links(ports, state["context"], preview["name"])
        return {**result, "name": preview["name"]}

    return LifecycleStep("branch_links", prepare, execute)


def build_story_start_graph(ports: LifecyclePorts, *, checkpointer: Any) -> Any:
    _require_ports(ports, "branches", "work_items")

    def activation(state: LifecycleState) -> dict[str, Any]:
        context = state["context"]
        required_ids(context)
        if not context.get("project") or not context.get("area") or not context.get("user_id"):
            raise ValueError("AZURE_PROJECT_AREA/project/user not configured")
        snapshot = ports.context.snapshot(context)
        return {"task_id": context["task_id"], "assigned_to": context["user_id"],
                "area_path": f'{context["project"]}\\{context["area"]}', "status": "Active",
                "expected_revision": snapshot["revision"]}

    def activate(state: LifecycleState, preview: dict[str, Any], key: str) -> dict[str, Any]:
        _check_revision(ports, state, preview)
        return ports.work_items.activate(preview, key=key)

    def select_type(state: LifecycleState) -> dict[str, Any]:
        selection = interrupt({"action": "select_branch_type", "story_id": state["context"]["story_id"],
                               "options": ["feature", "hotfix"]})
        if selection not in ("feature", "hotfix"):
            raise ValueError("Select feature or hotfix")
        return {"branch_type": selection}

    def branch_preview(state: LifecycleState) -> dict[str, Any]:
        branch_type = state["results"]["branch_type"]["branch_type"]
        preview = ports.branches.preview(state["context"], branch_type)
        _validate_branch(preview, state["context"], branch_type)
        return preview

    def branch(state: LifecycleState, preview: dict[str, Any], key: str) -> dict[str, Any]:
        branch_type = state["results"]["branch_type"]["branch_type"]
        current = ports.branches.preview(state["context"], branch_type)
        if current != preview:
            raise ValueError("Remote HEAD/checkout/branch changed after preview")
        result = ports.branches.create_or_reuse(preview, key=key)
        if result.get("name") != preview["name"]:
            raise ValueError("Created branch differs from approved preview")
        return result

    return build_lifecycle_graph(ports, (
        LifecycleStep("activate", activation, activate),
        LifecycleStep("branch_type", select_type),
        LifecycleStep("branch", branch_preview, branch),
        _association_step(ports, starting=True),
    ), name="story_start", checkpointer=checkpointer)


def build_story_completion_graph(ports: LifecyclePorts, *, checkpointer: Any) -> Any:
    _require_ports(ports, "branches", "work_items", "pull_requests")

    def pr_preview(state: LifecycleState) -> dict[str, Any]:
        context = state["context"]
        branch = state["results"]["branch_links"]["name"]
        _verify_links(ports, context, branch)
        snapshot = ports.context.snapshot(context)
        if snapshot.get("status") != "Desenvolvimento":
            raise ValueError("Completion requires Desenvolvimento")
        for field in ("implementation_complete", "tests_passed", "base_up_to_date"):
            if snapshot.get(field) is not True:
                raise ValueError(f"PR prerequisite missing: {field}")
        preview = ports.pull_requests.preview(context, branch)
        if (preview.get("source_branch") != branch or preview.get("target_branch") != "stage-pre-prod"
                or preview.get("is_draft") is not True):
            raise ValueError("PR must be Draft to stage-pre-prod")
        if not preview.get("template_path", "").endswith("pull_request_template.md"):
            raise ValueError("Repository PR template required")
        if not re.search(r"```mermaid\s+.+?```\s*$", preview.get("body", ""), re.DOTALL):
            raise ValueError("PR body must end with Mermaid diagram")
        return {**preview, "expected_revision": snapshot["revision"],
                "related_work_item_ids": required_ids(context)}

    def draft(state: LifecycleState, preview: dict[str, Any], key: str) -> dict[str, Any]:
        if pr_preview(state) != preview:
            raise ValueError("PR context/template changed after preview")
        result = ports.pull_requests.create_draft(preview, key=key)
        url = urlparse(result.get("url", ""))
        if result.get("is_draft") is not True or url.scheme != "https" or not url.netloc:
            raise ValueError("Draft PR direct URL required")
        return result

    def status_preview(state: LifecycleState) -> dict[str, Any]:
        snapshot = ports.context.snapshot(state["context"])
        if snapshot.get("status") != "Desenvolvimento":
            raise ValueError("Only Desenvolvimento -> Aguardando PR is allowed")
        return {"story_id": state["context"]["story_id"], "from_status": "Desenvolvimento",
                "target_status": "Aguardando PR", "expected_revision": snapshot["revision"],
                "pr_url": state["results"]["draft_pr"]["url"]}

    def awaiting(state: LifecycleState, preview: dict[str, Any], key: str) -> dict[str, Any]:
        if status_preview(state) != preview:
            raise ValueError("Story changed after status preview")
        return ports.work_items.awaiting_pr(preview, key=key)

    return build_lifecycle_graph(ports, (
        _association_step(ports, starting=False),
        LifecycleStep("draft_pr", pr_preview, draft),
        LifecycleStep("awaiting_pr", status_preview, awaiting),
    ), name="story_completion", checkpointer=checkpointer)