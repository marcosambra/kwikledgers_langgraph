from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from kwikledgers_langgraph.adapters import IdentityResolverPort, NotificationPort, StoryPullRequestPort


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
        summary = json.loads(payload)
        if not isinstance(summary, dict):
            raise RuntimeError("O provider legado retornou um resumo diario invalido.")
        return summary


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
        preview = json.loads(payload)
        if not isinstance(preview, dict):
            raise RuntimeError("O preview legado de PR retornou um payload invalido.")
        return preview

    def create_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        _ensure_legacy_import_path()
        from azure_devops import server as azure_server

        payload = azure_server._create_story_pull_request(**request)
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise RuntimeError("A criacao legada de PR retornou um payload invalido.")
        return result