from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from kwikledgers_langgraph.day_end_closure_policy import (
    _human_activity_timestamps, _parse_timestamp, _resolve_timezone, parse_daily_log_entries,
)
from kwikledgers_langgraph.fastmcp_client import CapabilityClient, FastMCPClient
from kwikledgers_langgraph.observability import resolve_ai_tracking_root


ExecutionCapabilityProvider = Callable[[str, dict[str, Any]], str]
BRANCH_FIELDS = (
    "story_id", "repository_name", "source_branch", "related_work_item_ids", "include_child_work_items",
)
PR_FIELDS = BRANCH_FIELDS + (
    "target_branch", "title", "what_was_changed", "affected_processes", "expected_impacts",
    "important_points", "tests_updated", "tests_unchanged", "new_library_name", "env_changes",
    "generated_migration_or_seed", "new_queue_or_command", "mermaid_diagram",
)


def _revision(request: dict[str, Any], *preview_keys: str) -> int:
    preview = request.get("preview", {})
    if not isinstance(preview, dict):
        raise ValueError("A structured approved preview is required.")
    revisions = [request.get("expected_revision")]
    revisions.extend(preview.get(key) for key in preview_keys)
    supplied = [value for value in revisions if value is not None]
    if not supplied or any(type(value) is not int or value <= 0 for value in supplied):
        raise ValueError("A positive native expected_revision is required.")
    if len(set(supplied)) != 1:
        raise ValueError("Revision does not match the approved preview.")
    return supplied[0]


def technical_arguments_for_capability(capability_name: str, request: dict[str, Any]) -> dict[str, Any]:
    """Build exactly the token-free payload the issuer must authorize."""
    if capability_name in {"preview_story_branch_association", "associate_story_branch"}:
        fields = BRANCH_FIELDS
    elif capability_name in {"preview_story_pull_request", "create_story_pull_request"}:
        fields = PR_FIELDS
    elif capability_name == "update_story_status":
        return {
            "story_id": int(request["story_id"]),
            "status": str(request["target_status"]),
            "expected_revision": _revision(request, "story_revision", "expected_revision"),
        }
    elif capability_name == "update_work_item_effort":
        preview = request.get("preview", {})
        if not isinstance(preview, dict):
            raise ValueError("Task effort requires a structured approved preview.")
        hours_only = preview.get("hours_only") is True
        if not hours_only and preview.get("closure_allowed") is not True:
            raise ValueError("Task closure requires an allowed approved preview.")
        task_id = int(request["task_id"])
        if preview.get("task_id", task_id) != task_id:
            raise ValueError("Task closure preview belongs to a different task.")
        hours = preview.get("completed_work_hours")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours < 0:
            raise ValueError("Task closure requires valid approved completed_work_hours.")
        arguments: dict[str, Any] = {
            "work_item_id": task_id,
            "completed_work_hours": hours,
            "expected_revision": _revision(request, "task_revision", "expected_revision", "revision"),
        }
        if not hours_only:
            arguments["status"] = "Closed"
        for field in ("remaining_work_hours", "comment"):
            if preview.get(field) is not None:
                arguments[field] = preview[field]
        return arguments
    elif capability_name == "publish_story_pull_request":
        preview = request.get("preview", {})
        if not isinstance(preview, dict) or not preview.get("expected_source_commit"):
            raise ValueError("Publishing requires an approved preview with expected_source_commit.")
        return {
            "repository_name": str(preview["repository_name"]),
            "pull_request_id": int(preview["pull_request_id"]),
            "expected_source_commit": str(preview["expected_source_commit"]),
        }
    elif capability_name == "cherry_pick_story_pull_request":
        preview = request.get("preview", {})
        if not isinstance(preview, dict):
            raise ValueError("Cherry-pick requires a structured approved preview.")
        if preview.get("action") != "azure_api":
            return {}
        return {
            "repository_name": str(preview["repository_name"]),
            "pull_request_id": int(preview["pull_request_id"]),
            "target_branch": str(preview["target_branch"]),
            "generated_branch": str(preview["generated_branch"]),
            "expected_source_commit": str(preview["expected_source_commit"]),
            "wait_seconds": int(preview.get("wait_seconds", 90)),
        }
    elif capability_name == "sync_daily_tracking":
        return {
            "summary": json.dumps(request["summary"], ensure_ascii=True),
            "actions_taken": ["langgraph_runtime", "fastmcp_daily_summary_provider", "fastmcp_local_tracking_adapter"],
            "source": request["source"],
        }
    elif capability_name == "send_notification":
        summary = request["summary"]
        counts = summary.get("counts", {})
        blocked = counts.get("blocked_items", 0)
        return {
            "title": "Resumo diario disponivel",
            "message": (
                "Resumo diario sincronizado pelo runtime LangGraph. "
                f"Itens atribuidos: {counts.get('assigned_items', 0)}. "
                f"Bloqueados: {blocked}. PRs abertas: {counts.get('open_prs', 0)}. "
                f"Story points restantes: {summary.get('remaining_story_points', '-')}"
                "."
            ),
            "urgency": "high" if blocked else "normal",
        }
    else:
        raise ValueError("Unsupported FastMCP argument mapping.")
    arguments = {field: request[field] for field in fields if field in request}
    if capability_name in {"associate_story_branch", "create_story_pull_request"}:
        preview = request.get("preview", {})
        if not isinstance(preview, dict):
            raise ValueError("A structured approved preview is required.")
        revisions = preview.get("expected_revisions") if preview else request.get("expected_revisions")
        if not isinstance(revisions, dict) or not revisions:
            raise ValueError("Approved expected_revisions are required for every branch/PR target.")
        if any(type(value) is not int or value <= 0 for value in revisions.values()):
            raise ValueError("Approved revisions must be positive native integers.")
        if "expected_revisions" in request and request["expected_revisions"] != revisions:
            raise ValueError("Revisions do not match the approved preview.")
        if "expected_revision" in request and request["expected_revision"] != revisions.get(str(request["story_id"]), revisions.get(request["story_id"])):
            raise ValueError("Story revision does not match the approved preview.")
        arguments["expected_revisions"] = dict(revisions)
    return arguments


class _FastMCPAdapter:
    def __init__(
        self, client: CapabilityClient | None = None,
        execution_capability_provider: ExecutionCapabilityProvider | None = None,
    ) -> None:
        self.client = client if client is not None else FastMCPClient()
        self.execution_capability_provider = execution_capability_provider

    def _execute(self, name: str, arguments: dict[str, Any], token: str | None) -> dict[str, Any]:
        if token is None and self.execution_capability_provider is not None:
            token = self.execution_capability_provider(name, dict(arguments))
        if not isinstance(token, str) or not token.strip():
            raise ValueError("A LangGraph-issued execution_capability is required.")
        return self.client.call(name, {**arguments, "execution_capability": token})


class FastMCPDailySummaryProvider(_FastMCPAdapter):
    def __init__(
        self, client: CapabilityClient | None = None, *, sprint_path: str | None = None, story_id: int | None = None,
    ) -> None:
        super().__init__(client)
        if sprint_path is not None and (not isinstance(sprint_path, str) or not sprint_path.strip()):
            raise ValueError("O caminho da sprint selecionada deve ser preenchido.")
        if story_id is not None and (type(story_id) is not int or story_id <= 0):
            raise ValueError("O ID da história deve ser um inteiro positivo.")
        self.arguments = {}
        if sprint_path is not None:
            self.arguments["sprint_path"] = sprint_path
        if story_id is not None:
            self.arguments["story_id"] = story_id

    def fetch_daily_summary(self) -> dict[str, Any]:
        return self.client.call("get_my_daily_summary", dict(self.arguments))


class FastMCPWorkItemProcessMetadataAdapter(_FastMCPAdapter):
    def fetch_work_item_process_metadata(self) -> dict[str, Any]:
        return self.client.call("get_work_item_process_metadata", {})


class FastMCPDayEndClosureAdapter(_FastMCPAdapter):
    def collect_day_context(self, request: dict[str, Any]) -> dict[str, Any]:
        zone = _resolve_timezone(request.get("timezone"))
        current = (_parse_timestamp(request.get("now")) or datetime.now(timezone.utc)).astimezone(zone)
        root = resolve_ai_tracking_root()
        stamps = {
            (current + timedelta(days=offset)).astimezone(timezone.utc).date().isoformat()
            for offset in (-1, 0, 1)
        } | {current.date().isoformat()}
        entries: list[dict[str, str]] = []
        for stamp in sorted(stamps):
            log = root / "Daily_Action_Logs" / f"{stamp}.md"
            if log.is_file():
                entries.extend(parse_daily_log_entries(log.read_text(encoding="utf-8")))
        if not _human_activity_timestamps(entries, current.date(), zone):
            return {"active_tasks": [], "activity_entries": entries}
        for journal in (root / "Task_Control" / "day-end-closures").glob("*.json"):
            if json.loads(journal.read_text(encoding="utf-8")).get("phase") != "completed":
                raise RuntimeError("Fechamento incompleto; reconcilie o Azure antes de gerar outra previa.")

        summary = self.client.call("get_my_work_items", {})
        email = summary.get("user_email")
        items = summary.get("items")
        if not isinstance(email, str) or not email.strip() or not isinstance(items, list):
            raise ValueError("A leitura nativa deve informar usuario e itens atribuidos.")
        active_tasks: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("A leitura nativa retornou um item invalido.")
            if str(item.get("work_item_type", "")).casefold() != "task":
                continue
            if str(item.get("state", "")).strip().casefold() not in {"active", "ativa", "em andamento"}:
                continue
            task_id = item.get("id")
            if type(task_id) is not int or task_id <= 0:
                raise ValueError("A task nativa deve possuir um ID positivo.")
            if request.get("task_id") and task_id != request["task_id"]:
                continue
            details = self.client.call("get_story_details", {"story_id": task_id})
            revision = details.get("revision")
            assigned_email = details.get("assigned_to_email")
            if (
                details.get("id") != task_id
                or type(revision) is not int or revision <= 0
                or str(details.get("work_item_type", "")).casefold() != "task"
                or str(details.get("state", "")).strip().casefold() not in {"active", "ativa", "em andamento"}
                or not isinstance(assigned_email, str)
                or assigned_email.casefold() != email.casefold()
            ):
                raise ValueError("A task mudou ou nao pertence ao usuario da leitura nativa.")
            if _parse_timestamp(details.get("activated_at")) is None:
                raise ValueError("A task ativa deve informar sua data de ativacao.")
            active_tasks.append({
                **{field: details.get(field) for field in (
                    "id", "title", "revision", "parent_id", "area_path", "iteration_path",
                    "activated_at", "completed_work_hours",
                )},
                "assigned_to": assigned_email,
            })
        return {"active_tasks": active_tasks, "activity_entries": entries}

    def execute_day_end_closure(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("O fechamento diario nativo fornece apenas previa; use o lifecycle com aprovacao humana.")


class FastMCPSprintStoriesProvider(_FastMCPAdapter):
    def fetch_sprint_stories_detailed(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.client.call("get_sprint_stories_detailed", {})


class FastMCPTrackingSyncAdapter(_FastMCPAdapter):
    def sync_daily_tracking(
        self, summary: dict[str, Any], source: str, *, execution_capability: str | None = None,
    ) -> dict[str, Any]:
        arguments = technical_arguments_for_capability("sync_daily_tracking", {"summary": summary, "source": source})
        return self._execute("sync_daily_tracking", arguments, execution_capability)


class FastMCPNotificationAdapter(_FastMCPAdapter):
    def send_notification(self, title: str, message: str, urgency: str = "normal") -> dict[str, Any]:
        return self._execute("send_notification", {"title": title, "message": message, "urgency": urgency}, None)

    def send_daily_summary_notification(
        self, summary: dict[str, Any], *, execution_capability: str | None = None,
    ) -> dict[str, Any]:
        arguments = technical_arguments_for_capability("send_notification", {"summary": summary})
        return self._execute("send_notification", arguments, execution_capability)


class FastMCPStoryBranchAssociationAdapter(_FastMCPAdapter):
    def preview_story_branch_association(self, request: dict[str, Any]) -> dict[str, Any]:
        arguments = technical_arguments_for_capability("preview_story_branch_association", request)
        return self.client.call("preview_story_branch_association", arguments)

    def associate_story_branch(self, request: dict[str, Any]) -> dict[str, Any]:
        arguments = technical_arguments_for_capability("associate_story_branch", request)
        return self._execute("associate_story_branch", arguments, request.get("execution_capability"))


class FastMCPStoryPullRequestAdapter(_FastMCPAdapter):
    def preview_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        arguments = technical_arguments_for_capability("preview_story_pull_request", request)
        return {**self.client.call("preview_story_pull_request", arguments),
                "execution_requires_lifecycle": "story_completion"}

    def create_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("PR creation requires lifecycle story_completion with implementation/test evidence.")


class FastMCPStoryPrLaunchAdapter(_FastMCPAdapter):
    """Draft PR for a branch already published; checks run in the story_pr_launch preflight."""

    def preview_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        arguments = technical_arguments_for_capability("preview_story_pull_request", request)
        return self.client.call("preview_story_pull_request", arguments)

    def create_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        arguments = technical_arguments_for_capability("create_story_pull_request", request)
        return self._execute("create_story_pull_request", arguments, request.get("execution_capability"))


def _story_pull_requests(client: CapabilityClient, request: dict[str, Any], status: str) -> list[dict[str, Any]]:
    arguments: dict[str, Any] = {"story_id": int(request["story_id"]), "status": status}
    if request.get("repository_name"):
        arguments["repository_name"] = request["repository_name"]
    if request.get("source_target_branch"):
        arguments["target_branch"] = request["source_target_branch"]
    pull_requests = client.call("list_story_pull_requests", arguments).get("pull_requests")
    return [item for item in pull_requests or [] if isinstance(item, dict)]


def select_story_pull_request(
    pull_requests: list[dict[str, Any]], pull_request_id: int | None, *, description: str,
) -> dict[str, Any]:
    if pull_request_id is not None:
        matches = [item for item in pull_requests if item.get("pull_request_id") == int(pull_request_id)]
    else:
        matches = pull_requests
    if len(matches) != 1:
        raise ValueError(
            f"Esperava exatamente 1 PR {description}, encontrei {len(matches)}; informe --pull-request-id."
        )
    return matches[0]


class FastMCPStoryPrPublishAdapter(_FastMCPAdapter):
    def preview_publish(self, request: dict[str, Any]) -> dict[str, Any]:
        selected = select_story_pull_request(
            _story_pull_requests(self.client, request, "active"), request.get("pull_request_id"),
            description="ativa da história neste repositório",
        )
        if selected.get("is_draft") is not True:
            raise ValueError(f"A PR {selected.get('pull_request_id')} já está publicada.")
        if str(selected.get("merge_status") or "").lower() == "conflicts":
            raise ValueError(f"A PR {selected.get('pull_request_id')} tem conflitos de merge; resolva antes.")
        return {
            "story_id": int(request["story_id"]),
            "repository_name": selected["repository_name"],
            "pull_request_id": selected["pull_request_id"],
            "title": selected.get("title"),
            "source_ref_name": selected.get("source_ref_name"),
            "target_ref_name": selected.get("target_ref_name"),
            "expected_source_commit": selected.get("source_commit_id"),
            "web_url": selected.get("web_url"),
            "merge_status": selected.get("merge_status"),
        }

    def publish_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        arguments = technical_arguments_for_capability("publish_story_pull_request", request)
        return self._execute("publish_story_pull_request", arguments, request.get("execution_capability"))


class FastMCPStoryPrCherryPickAdapter(_FastMCPAdapter):
    def list_candidate_pull_requests(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        return _story_pull_requests(self.client, request, "active")

    def cherry_pick(self, request: dict[str, Any]) -> dict[str, Any]:
        arguments = technical_arguments_for_capability("cherry_pick_story_pull_request", request)
        return self._execute("cherry_pick_story_pull_request", arguments, request.get("execution_capability"))

    def get_cherry_pick(self, repository_name: str, cherry_pick_id: int) -> dict[str, Any]:
        return self.client.call(
            "get_story_cherry_pick", {"repository_name": repository_name, "cherry_pick_id": int(cherry_pick_id)},
        )


class FastMCPStoryStatusTransitionAdapter(_FastMCPAdapter):
    def preview_story_status_transition(self, request: dict[str, Any]) -> dict[str, Any]:
        story_id = int(request["story_id"])
        details = self.client.call("get_story_details", {"story_id": story_id})
        fields = details.get("fields", {})
        if not isinstance(fields, dict):
            raise ValueError("Native work item fields must be structured.")
        revision = details.get("revision", details.get("rev", details.get("story_revision")))
        status = details.get("current_status", details.get("status", details.get("state", fields.get("System.State"))))
        if type(revision) is not int or revision <= 0 or not isinstance(status, str) or not status:
            raise ValueError("Native story details must include a structured status and revision.")
        if details.get("story_id", details.get("id", story_id)) != story_id:
            raise ValueError("Native story details belong to a different story.")
        return {
            "story_id": story_id,
            "title": details.get("title", fields.get("System.Title")),
            "work_item_type": details.get("work_item_type", fields.get("System.WorkItemType")),
            "url": details.get("url"),
            "current_status": status,
            "target_status": request.get("target_status"),
            "story_revision": revision,
        }

    def update_story_status(self, request: dict[str, Any]) -> dict[str, Any]:
        arguments = technical_arguments_for_capability("update_story_status", request)
        result = self._execute("update_story_status", arguments, request.get("execution_capability"))
        return {
            **result,
            "status": "updated",
            "story_id": arguments["story_id"],
            "previous_status": request.get("preview", {}).get("current_status"),
            "current_status": arguments["status"],
            "changed": True,
        }


class FastMCPTaskClosureAdapter(_FastMCPAdapter):
    def preview_task_closure(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = int(request["task_id"])
        details = self.client.call("get_story_details", {"story_id": task_id})
        revision = details.get("revision", details.get("rev", details.get("task_revision")))
        if type(revision) is not int or revision <= 0:
            raise ValueError("Native task details must include a positive revision.")
        if details.get("task_id", details.get("story_id", details.get("id", task_id))) != task_id:
            raise ValueError("Native task details belong to a different task.")
        return {"task_id": task_id, "task_revision": revision, "execution_requires_lifecycle": "task_cycle"}

    def close_task(self, request: dict[str, Any]) -> dict[str, Any]:
        preview = request.get("preview", {})
        try:
            arguments = technical_arguments_for_capability(
                "update_work_item_effort", {**request, "preview": preview},
            )
        except ValueError as error:
            raise RuntimeError(
                "Task closure requires an allowed approved preview."
            ) from error
        return self._execute("update_work_item_effort", arguments, request.get("execution_capability"))