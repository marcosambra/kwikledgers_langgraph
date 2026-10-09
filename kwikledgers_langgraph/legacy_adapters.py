from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kwikledgers_langgraph.adapters import (
    DayEndClosurePort,
    IdentityResolverPort,
    NotificationPort,
    SprintStoriesProvider,
    StoryBranchAssociationPort,
    StoryPullRequestPort,
    StoryStatusTransitionPort,
    TaskClosurePort,
)


def _parse_legacy_json_dict(payload: str, *, invalid_message: str, root_message: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError(invalid_message) from error
    if not isinstance(parsed, dict):
        raise RuntimeError(root_message)
    return parsed


def _minimalize_story_branch_association_preview(preview: dict[str, Any]) -> dict[str, Any]:
    parent = dict(preview.get("parent_work_item", {}))
    counts = dict(preview.get("counts", {}))
    return {
        "operation": preview.get("operation"),
        "story_id": preview.get("story_id"),
        "repository_name": preview.get("repository_name"),
        "source_branch": preview.get("source_branch"),
        "source_ref_name": preview.get("source_ref_name"),
        "branch_artifact_uri": preview.get("branch_artifact_uri"),
        "branch_browser_url": preview.get("branch_browser_url"),
        "parent_work_item": {
            "id": parent.get("id"),
            "title": parent.get("title"),
            "state": parent.get("state"),
            "work_item_type": parent.get("work_item_type"),
            "browser_url": parent.get("browser_url"),
        },
        "related_work_item_ids": list(preview.get("related_work_item_ids", [])),
        "include_child_work_items": bool(preview.get("include_child_work_items", True)),
        "counts": {
            "total_work_items": counts.get("total_work_items"),
            "already_linked": counts.get("already_linked"),
            "pending_link": counts.get("pending_link"),
        },
    }


def _minimalize_story_pull_request_preview(preview: dict[str, Any]) -> dict[str, Any]:
    branch_preview = dict(preview.get("branch_association_preview", {}))
    existing_active_pull_request = preview.get("existing_active_pull_request")
    return {
        "operation": preview.get("operation"),
        "story_id": preview.get("story_id"),
        "repository_name": preview.get("repository_name"),
        "source_branch": preview.get("source_branch"),
        "source_ref_name": preview.get("source_ref_name"),
        "target_branch": preview.get("target_branch"),
        "target_ref_name": preview.get("target_ref_name"),
        "is_draft": bool(preview.get("is_draft", True)),
        "title": preview.get("title"),
        "can_open_pull_request": bool(preview.get("can_open_pull_request", False)),
        "branch_association_preview": _minimalize_story_branch_association_preview(branch_preview),
        "existing_active_pull_request": existing_active_pull_request,
        "recommended_story_status_transition": preview.get("recommended_story_status_transition"),
    }


def _resolve_mcp_path() -> Path:
    explicit_mcp_path = os.environ.get("KWIKLEDGERS_MCP_SERVERS_PATH")
    if explicit_mcp_path:
        candidate = Path(explicit_mcp_path).expanduser().resolve()
        if candidate.is_dir():
            return candidate
        raise RuntimeError(
            "A variavel KWIKLEDGERS_MCP_SERVERS_PATH foi definida, mas nao aponta para um diretorio valido."
        )

    explicit_legacy_root = os.environ.get("KWIKLEDGERS_LEGACY_ROOT")
    if explicit_legacy_root:
        candidate = Path(explicit_legacy_root).expanduser().resolve() / "agent" / "mcp_servers"
        if candidate.is_dir():
            return candidate
        raise RuntimeError(
            "A variavel KWIKLEDGERS_LEGACY_ROOT foi definida, mas o caminho 'agent/mcp_servers' nao foi encontrado nela."
        )

    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "agent" / "mcp_servers"
        if candidate.is_dir():
            return candidate

    raise RuntimeError(
        "Nao foi possivel localizar o runtime legado da KwikLedgers. "
        "Defina KWIKLEDGERS_MCP_SERVERS_PATH com o diretorio 'agent/mcp_servers' "
        "ou KWIKLEDGERS_LEGACY_ROOT com o root do workspace legado."
    )


def _ensure_legacy_import_path() -> None:
    mcp_path = str(_resolve_mcp_path())
    if mcp_path not in sys.path:
        sys.path.insert(0, mcp_path)


class LegacyDailySummaryProvider:
    def fetch_daily_summary(self) -> dict[str, Any]:
        _ensure_legacy_import_path()
        from azure_devops import server as azure_server

        payload = azure_server._get_my_daily_summary()
        return _parse_legacy_json_dict(
            payload,
            invalid_message="O provider legado retornou um resumo diario em JSON invalido.",
            root_message="O provider legado retornou um resumo diario invalido.",
        )


class EnvironmentIdentityResolverAdapter(IdentityResolverPort):
    def resolve_current_user_identity(self) -> dict[str, Any]:
        resolved_email = os.environ.get("KWIKLEDGERS_USER_EMAIL")
        display_name = os.environ.get("KWIKLEDGERS_USER_DISPLAY_NAME")

        identity: dict[str, Any] = {}
        if resolved_email:
            identity["resolved_email"] = resolved_email
            identity["email_source"] = "environment"
        if display_name:
            identity["display_name"] = display_name
        return identity


class LegacyTrackingSyncAdapter:
    def sync_daily_tracking(self, summary: dict[str, Any], source: str) -> Any:
        _ensure_legacy_import_path()
        from local_tracking import server as tracking_server

        return tracking_server._sync_daily_tracking(
            json.dumps(summary, ensure_ascii=True),
            [
                "langgraph_runtime",
                "legacy_daily_summary_provider",
                "legacy_local_tracking_adapter",
            ],
            "Resumo diario executado pelo runtime LangGraph.",
            None,
            "Resumo diario executado pelo runtime LangGraph.",
            None,
            source,
        )


class LegacyNotificationAdapter(NotificationPort):
    def send_daily_summary_notification(self, summary: dict[str, Any]) -> Any:
        _ensure_legacy_import_path()
        from windows_calendar import server as windows_server

        counts = summary.get("counts", {})
        blocked_items = counts.get("blocked_items", 0)
        urgency = "high" if blocked_items else "normal"
        remaining_story_points = summary.get("remaining_story_points", "-")
        message = (
            "Resumo diario sincronizado pelo runtime LangGraph. "
            f"Itens atribuidos: {counts.get('assigned_items', 0)}. "
            f"Bloqueados: {blocked_items}. "
            f"PRs abertas: {counts.get('open_prs', 0)}. "
            f"Story points restantes: {remaining_story_points}."
        )
        return windows_server._send_notification(
            "Resumo diario disponivel",
            message,
            urgency,
        )


class NoOpNotificationAdapter(NotificationPort):
    def send_daily_summary_notification(self, summary: dict[str, Any]) -> Any:
        counts = summary.get("counts", {})
        return {
            "status": "skipped",
            "reason": "notifications_disabled",
            "counts": counts,
        }


class LegacyStoryPullRequestAdapter(StoryPullRequestPort):
    def preview_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        _ensure_legacy_import_path()
        from azure_devops import server as azure_server

        payload = azure_server._preview_story_pull_request(**request)
        preview = _parse_legacy_json_dict(
            payload,
            invalid_message="O preview legado de PR retornou JSON invalido.",
            root_message="O preview legado de PR retornou um payload raiz invalido.",
        )
        return _minimalize_story_pull_request_preview(preview)

    def create_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        _ensure_legacy_import_path()
        from azure_devops import server as azure_server

        payload = azure_server._create_story_pull_request(**request)
        return _parse_legacy_json_dict(
            payload,
            invalid_message="A criacao legada de PR retornou JSON invalido.",
            root_message="A criacao legada de PR retornou um payload raiz invalido.",
        )


class LegacyStoryBranchAssociationAdapter(StoryBranchAssociationPort):
    def preview_story_branch_association(self, request: dict[str, Any]) -> dict[str, Any]:
        _ensure_legacy_import_path()
        from azure_devops import server as azure_server

        payload = azure_server._preview_story_branch_association(**request)
        preview = _parse_legacy_json_dict(
            payload,
            invalid_message="O preview legado de associacao de branch retornou JSON invalido.",
            root_message="O preview legado de associacao de branch retornou um payload raiz invalido.",
        )
        return _minimalize_story_branch_association_preview(preview)

    def associate_story_branch(self, request: dict[str, Any]) -> dict[str, Any]:
        _ensure_legacy_import_path()
        from azure_devops import server as azure_server

        payload = azure_server._associate_story_branch(**request)
        return _parse_legacy_json_dict(
            payload,
            invalid_message="A associacao legada de branch retornou JSON invalido.",
            root_message="A associacao legada de branch retornou um payload raiz invalido.",
        )


class LegacyStoryStatusTransitionAdapter(StoryStatusTransitionPort):
    def preview_story_status_transition(self, request: dict[str, Any]) -> dict[str, Any]:
        _ensure_legacy_import_path()
        from azure_devops import server as azure_server

        story_id = int(request["story_id"])
        payload = azure_server._get_story_details(story_id)
        details = self._parse_story_details(payload, story_id=story_id)
        return {
            "story_id": story_id,
            "title": details["title"],
            "work_item_type": details["work_item_type"],
            "url": details["url"],
            "current_status": details["current_status"],
            "story_revision": details["story_revision"],
        }

    def update_story_status(self, request: dict[str, Any]) -> dict[str, Any]:
        preview = dict(request.get("preview", {}))
        story_id = int(request["story_id"])
        target_status = str(request["target_status"])

        latest_preview = self.preview_story_status_transition(
            {
                "story_id": story_id,
                "target_status": target_status,
                "expected_current_status": preview.get("current_status") or request.get("expected_current_status"),
            }
        )
        if preview:
            if preview.get("story_revision") != latest_preview.get("story_revision"):
                raise RuntimeError("A story foi alterada apos o preview original; gere uma nova pre-visualizacao antes de atualizar o status.")
            if preview.get("current_status") != latest_preview.get("current_status"):
                raise RuntimeError("O status atual da story mudou apos o preview original; gere uma nova pre-visualizacao antes de atualizar o status.")
        _ensure_legacy_import_path()
        from azure_devops import server as azure_server

        payload = azure_server._update_story_status(story_id=story_id, status=target_status)
        if isinstance(payload, str):
            try:
                parsed_payload = json.loads(payload)
            except json.JSONDecodeError:
                parsed_payload = {"message": payload}
        elif isinstance(payload, dict):
            parsed_payload = payload
        else:
            parsed_payload = {"message": str(payload)}

        result = {
            "status": "updated",
            "story_id": story_id,
            "previous_status": latest_preview.get("current_status"),
            "current_status": target_status,
            "changed": True,
            "story_revision": latest_preview.get("story_revision"),
        }
        result.update(parsed_payload)
        return result

    def _parse_story_details(self, payload: str, *, story_id: int) -> dict[str, Any]:
        header = f"=== KL-{story_id}: "
        title = None
        current_status = None
        work_item_type = "User Story"
        url = None
        story_revision = None

        for line in payload.splitlines():
            stripped = line.strip()
            if stripped.startswith(header) and stripped.endswith(" ==="):
                title = stripped[len(header):-4]
            elif stripped.startswith("Status:"):
                current_status = stripped.split(":", 1)[1].strip()

        if not title or not current_status:
            raise RuntimeError("Nao foi possivel interpretar os detalhes legados da story para montar o preview de status.")

        return {
            "title": title,
            "current_status": current_status,
            "work_item_type": work_item_type,
            "url": url,
            "story_revision": story_revision or f"story-status-{story_id}-{current_status}",
        }


class LegacyTaskClosureAdapter(TaskClosurePort):
    def close_task(self, request: dict[str, Any]) -> dict[str, Any]:
        preview = dict(request.get("preview", {}))
        if not preview.get("closure_allowed"):
            raise RuntimeError("O fechamento da task nao possui um preview valido.")

        _ensure_legacy_import_path()
        from azure_devops import server as azure_server

        task_id = int(request["task_id"])
        effort_payload = azure_server._update_work_item_effort(
            work_item_id=task_id,
            completed_work_hours=preview.get("completed_work_hours"),
            remaining_work_hours=preview.get("remaining_work_hours"),
            comment=preview.get("comment"),
        )
        effort_result = _parse_legacy_json_dict(
            effort_payload,
            invalid_message="O fechamento legado retornou JSON invalido ao atualizar esforco.",
            root_message="O fechamento legado retornou um payload raiz invalido ao atualizar esforco.",
        )
        status_result = azure_server._update_story_status(task_id, "Closed")
        return {
            "status": "closed",
            "task_id": task_id,
            "completed_work_hours": preview.get("completed_work_hours"),
            "remaining_work_hours": preview.get("remaining_work_hours"),
            "target_status": "Closed",
            "changed": True,
            "effort_result": effort_result,
            "status_result": status_result,
        }

class LegacyDayEndClosureAdapter(DayEndClosurePort):
    _ACTIVE_STATES = {"active", "ativa", "em andamento"}

    def collect_day_context(self, request: dict[str, Any]) -> dict[str, Any]:
        _ensure_legacy_import_path()
        from azure_devops import server as azure_server
        from local_tracking import server as tracking_server

        from kwikledgers_langgraph.day_end_closure_policy import (
            _human_activity_timestamps, _resolve_timezone, parse_daily_log_entries,
        )

        zone = _resolve_timezone(request.get("timezone"))
        current = datetime.now(zone)
        entries: list[dict[str, str]] = []
        stamps = {(current + timedelta(days=offset)).astimezone(timezone.utc).date().isoformat() for offset in (-1, 0, 1)}
        for stamp in stamps | {current.date().isoformat()}:
            log_path = tracking_server.DAILY_LOG_DIR / f"{stamp}.md"
            if log_path.exists():
                entries.extend(parse_daily_log_entries(log_path.read_text(encoding="utf-8")))
        if not _human_activity_timestamps(entries, current.date(), zone):
            return {"active_tasks": [], "activity_entries": entries}
        journal_dir = tracking_server.TRACKING_DIR / "Task_Control" / "day-end-closures"
        for journal in journal_dir.glob("*.json"):
            if json.loads(journal.read_text(encoding="utf-8")).get("phase") != "completed":
                raise RuntimeError(f"Fechamento incompleto em {journal.name}; verifique o Azure antes de novos lancamentos.")

        email = azure_server._resolve_active_user_email()
        if not email:
            raise RuntimeError("Nao foi possivel resolver o usuario ativo do Azure DevOps.")
        wit = azure_server.get_client().clients.get_work_item_tracking_client()

        active_tasks: list[dict[str, Any]] = []
        for item in azure_server._query_assigned_items(email):
            if str(item.get("work_item_type", "")).lower() != "task":
                continue
            if str(item.get("state", "")).strip().lower() not in self._ACTIVE_STATES:
                continue
            work_item = wit.get_work_item(id=int(item["id"]), expand="Relations")
            fields = work_item.fields or {}
            active_tasks.append({
                "id": item["id"],
                "title": item.get("title"),
                "revision": work_item.rev,
                "parent_id": self._parent_id(work_item),
                "area_path": fields.get("System.AreaPath"),
                "iteration_path": fields.get("System.IterationPath"),
                "assigned_to": email,
                "activated_at": str(fields.get("Microsoft.VSTS.Common.ActivatedDate") or "") or None,
                "completed_work_hours": fields.get("Microsoft.VSTS.Scheduling.CompletedWork"),
            })

        return {"active_tasks": active_tasks, "activity_entries": entries}

    @staticmethod
    def _parent_id(work_item: Any) -> int | None:
        for relation in getattr(work_item, "relations", None) or []:
            if getattr(relation, "rel", None) == "System.LinkTypes.Hierarchy-Reverse":
                return int(str(relation.url).rstrip("/").rsplit("/", 1)[-1])
        return None

    def execute_day_end_closure(self, request: dict[str, Any]) -> dict[str, Any]:
        preview = dict(request.get("preview", {}))
        if preview.get("status") != "ready" or preview.get("dry_run") or request.get("dry_run"):
            raise RuntimeError("O fechamento de fim de dia nao possui um preview pronto para execucao.")

        _ensure_legacy_import_path()
        from azure_devops import server as azure_server
        from local_tracking import server as tracking_server

        task_id = int(preview["task"]["id"])
        wit = azure_server.get_client().clients.get_work_item_tracking_client()
        current = wit.get_work_item(id=task_id)
        fields = current.fields or {}
        assigned = fields.get("System.AssignedTo") or {}
        email = assigned.get("uniqueName") if isinstance(assigned, dict) else str(assigned)
        if (
            current.rev != preview.get("expected_revision")
            or str(fields.get("System.WorkItemType", "")).lower() != "task"
            or str(fields.get("System.State", "")).lower() not in self._ACTIVE_STATES
            or str(email).casefold() != str(preview.get("assigned_to", "")).casefold()
            or str(email).casefold() != str(azure_server._resolve_active_user_email()).casefold()
        ):
            raise RuntimeError("A task mudou desde o preview; nenhuma hora foi lancada.")
        journal_dir = tracking_server.TRACKING_DIR / "Task_Control" / "day-end-closures"
        journal_dir.mkdir(parents=True, exist_ok=True)
        journal_path = journal_dir / f"{task_id}.json"
        try:
            with journal_path.open("x", encoding="utf-8") as handle:
                json.dump({"phase": "started", "preview": preview}, handle, ensure_ascii=False)
        except FileExistsError as error:
            raise RuntimeError("Este fechamento ja foi iniciado; confira o registro local antes de repetir.") from error

        def save_phase(phase: str, **updates: Any) -> None:
            journal_path.write_text(json.dumps({"phase": phase, "preview": preview, **updates}, ensure_ascii=False), encoding="utf-8")

        tracking_server._append_daily_action_log(
            f"Preview de fechamento da task {task_id}",
            f"Inicio: {preview['window_start']}. Fechamento: {preview['window_end']}. Horas: {preview['computed_hours']}.",
            "day-end-closure",
        )
        wit.update_work_item(id=task_id, document=[
            {"op": "test", "path": "/rev", "value": current.rev},
            {"op": "add", "path": "/fields/Microsoft.VSTS.Scheduling.CompletedWork", "value": preview["completed_work_hours"]},
            {"op": "add", "path": "/fields/System.State", "value": "Closed"},
            {"op": "add", "path": "/fields/System.History", "value": preview.get("comment")},
        ])
        save_phase("closed")
        result: dict[str, Any] = {
            "status": "closed",
            "task_id": task_id,
            "completed_work_hours": preview["completed_work_hours"],
            "changed": True,
        }

        continuation = preview.get("continuation")
        if continuation:
            try:
                if not continuation.get("parent_id") or continuation.get("assigned_to") != preview.get("assigned_to"):
                    raise RuntimeError("A continuacao precisa da historia pai e do mesmo usuario.")
                document = [
                    {"op": "add", "path": "/fields/" + field, "value": value}
                    for field, value in {
                        "System.Title": continuation["title"],
                        "System.State": "Active",
                        "System.AssignedTo": continuation["assigned_to"],
                        "System.AreaPath": continuation.get("area_path"),
                        "System.IterationPath": continuation.get("iteration_path"),
                        "System.Tags": f"kwikledgers-continuation-{task_id}",
                    }.items() if value is not None
                ]
                azure_server._append_parent_relation_patch(document, int(continuation["parent_id"]))
                save_phase("continuation_requested")
                created = wit.create_work_item(document=document, project=azure_server.get_project(), type="Task")
                new_id = int(created.id)
                save_phase("continuation_created", continuation_task_id=new_id)
                result["continuation_task_id"] = new_id
            except Exception as error:
                result.update({"status": "closed_continuation_failed", "continuation_error": str(error)})

        tracking_server._append_daily_action_log(
            f"Fechamento de fim de dia da task {task_id}",
            (
                f"Horas lancadas: {preview['computed_hours']}h (total {preview['completed_work_hours']}h) ate "
                f"{preview['window_end']}. Gatilho: {preview.get('trigger')}. "
                f"Task de continuacao: {result.get('continuation_task_id', 'nao criada')}."
            ),
            "day-end-closure",
        )
        save_phase("completed" if result["status"] == "closed" else "needs_attention", result=result)
        return result


def send_legacy_notification(title: str, message: str, urgency: str = "normal") -> Any:
    _ensure_legacy_import_path()
    from windows_calendar import server as windows_server

    return windows_server._send_notification(title, message, urgency)


class LegacySprintStoriesProvider(SprintStoriesProvider):
    def fetch_sprint_stories_detailed(self, request: dict[str, Any]) -> dict[str, Any]:
        _ensure_legacy_import_path()
        from azure_devops import server as azure_server

        return _parse_legacy_json_dict(
            azure_server._get_sprint_stories_detailed(),
            invalid_message="O provider legado retornou historias detalhadas do sprint em JSON invalido.",
            root_message="O provider legado retornou historias detalhadas do sprint em formato invalido.",
        )