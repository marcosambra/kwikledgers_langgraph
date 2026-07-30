from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from kwikledgers_langgraph.adapters import (
    IdentityResolverPort,
    NotificationPort,
    StoryBranchAssociationPort,
    StoryPullRequestPort,
    StoryStatusTransitionPort,
)


CANONICAL_STORY_STATUS_FLOW = (
    "New",
    "Refinamento",
    "Backlog Sprint",
    "Desenvolvimento",
    "Aguardando PR",
    "Homologação",
    "QA",
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
        target_status = str(request["target_status"])
        expected_current_status = str(request.get("expected_current_status") or "").strip() or None

        details = self._get_native_story_details(azure_server, story_id=story_id)
        if details is None:
            payload = azure_server._get_story_details(story_id)
            details = self._parse_story_details(payload, story_id=story_id)
        current_status = details["current_status"]
        current_index = self._find_status_index(current_status)
        target_index = self._find_status_index(target_status)
        transition_allowed = current_index is not None and target_index is not None and target_index == current_index + 1

        validation_messages: list[str] = []
        if details["work_item_type"] and details["work_item_type"] != "User Story":
            validation_messages.append("O item informado nao e uma User Story.")
        if expected_current_status and expected_current_status != current_status:
            validation_messages.append(
                "O status atual da story nao corresponde ao status esperado informado no request."
            )
        if current_index is None:
            validation_messages.append("O status atual da story nao faz parte do fluxo canonico esperado.")
        if target_index is None:
            validation_messages.append("O status de destino nao faz parte do fluxo canonico esperado.")
        if not transition_allowed and current_index is not None and target_index is not None:
            validation_messages.append("A transicao solicitada nao e a proxima etapa permitida no fluxo canonico.")

        return {
            "story_id": story_id,
            "title": details["title"],
            "work_item_type": details["work_item_type"],
            "url": details["url"],
            "current_status": current_status,
            "target_status": target_status,
            "expected_current_status": expected_current_status,
            "transition_allowed": not validation_messages and transition_allowed,
            "validation_messages": validation_messages,
            "canonical_flow": list(CANONICAL_STORY_STATUS_FLOW),
            "patch_preview": {"System.State": target_status},
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
        if not latest_preview.get("transition_allowed"):
            raise RuntimeError("A transicao de status solicitada nao e permitida pelo fluxo canonico da story.")

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

    def _find_status_index(self, status: str | None) -> int | None:
        if status in CANONICAL_STORY_STATUS_FLOW:
            return CANONICAL_STORY_STATUS_FLOW.index(status)
        return None

    def _get_native_story_details(self, azure_server: Any, *, story_id: int) -> dict[str, Any] | None:
        get_client = getattr(azure_server, "get_client", None)
        if not callable(get_client):
            return None

        try:
            connection = get_client()
            wit = connection.clients.get_work_item_tracking_client()
            item = wit.get_work_item(
                id=story_id,
                fields=[
                    "System.Id",
                    "System.Title",
                    "System.State",
                    "System.WorkItemType",
                ],
            )
        except Exception:
            return None

        fields = getattr(item, "fields", {}) or {}
        title = str(fields.get("System.Title") or "").strip()
        current_status = str(fields.get("System.State") or "").strip()
        if not title or not current_status:
            return None

        work_item_type = str(fields.get("System.WorkItemType") or "User Story").strip() or "User Story"
        revision = getattr(item, "rev", None)
        normalized_revision = str(revision).strip() if revision is not None else ""

        return {
            "title": title,
            "current_status": current_status,
            "work_item_type": work_item_type,
            "url": getattr(item, "url", None),
            "story_revision": normalized_revision or f"story-status-{story_id}-{current_status}",
        }

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