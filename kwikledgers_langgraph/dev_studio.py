"""Mocked ports and graph factories used only by LangGraph Studio (`langgraph dev`).

Nunca importe este módulo a partir de código de produção: os ports aqui não
tocam Azure DevOps, tracking local ou notificações reais, servindo apenas para
visualização interativa e depuração da topologia dos grafos.
"""
from __future__ import annotations

from typing import Any

from kwikledgers_langgraph.adapters import (
    DailySummaryPorts,
    StoryBranchAssociationPorts,
    StoryPullRequestPorts,
    StoryStatusTransitionPorts,
)


class _MockDailySummaryProvider:
    def fetch_daily_summary(self) -> dict[str, Any]:
        return {
            "counts": {"stories": 3, "tasks": 5, "bugs": 1},
            "blocked_items": [],
        }


class _MockTrackingSync:
    def sync_daily_tracking(self, summary: dict[str, Any], source: str) -> Any:
        return {"status": "ok", "source": source}


class _MockNotifications:
    def send_daily_summary_notification(self, summary: dict[str, Any]) -> Any:
        return {"status": "sent"}


class _MockIdentityResolver:
    def resolve_current_user_identity(self) -> dict[str, Any]:
        return {"display_name": "Dev Studio", "resolved_email": "dev@kwikledgers.local"}


class _MockStoryPullRequestPort:
    def preview_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"title": f"PR mock para a story {request.get('story_id')}"}

    def create_story_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"title": f"PR mock criada para a story {request.get('story_id')}", "url": "https://example.invalid/pr/1"}


class _MockStoryBranchAssociationPort:
    def preview_story_branch_association(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "operation": "story_branch_association",
            "story_id": request.get("story_id"),
            "repository_name": request.get("repository_name"),
            "source_branch": request.get("source_branch"),
        }

    def associate_story_branch(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "associated", "story_id": request.get("story_id")}


class _MockStoryStatusTransitionPort:
    def preview_story_status_transition(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "story_id": request.get("story_id"),
            "target_status": request.get("target_status"),
        }

    def update_story_status(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "updated", "story_id": request.get("story_id")}


def daily_summary_graph(config: dict[str, Any] | None = None) -> Any:
    from kwikledgers_langgraph.graphs.daily_summary import build_daily_summary_graph

    ports = DailySummaryPorts(
        provider=_MockDailySummaryProvider(),
        tracking=_MockTrackingSync(),
        identity=_MockIdentityResolver(),
        notifications=_MockNotifications(),
    )
    return build_daily_summary_graph(ports)


def story_pull_request_graph(config: dict[str, Any] | None = None) -> Any:
    from kwikledgers_langgraph.graphs.story_pull_request import build_story_pull_request_graph

    return build_story_pull_request_graph(StoryPullRequestPorts(pull_requests=_MockStoryPullRequestPort()))


def story_branch_association_graph(config: dict[str, Any] | None = None) -> Any:
    from kwikledgers_langgraph.graphs.story_branch_association import build_story_branch_association_graph

    return build_story_branch_association_graph(
        StoryBranchAssociationPorts(branches=_MockStoryBranchAssociationPort())
    )


def story_status_transition_graph(config: dict[str, Any] | None = None) -> Any:
    from kwikledgers_langgraph.graphs.story_status_transition import build_story_status_transition_graph

    return build_story_status_transition_graph(
        StoryStatusTransitionPorts(statuses=_MockStoryStatusTransitionPort())
    )
