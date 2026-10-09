from __future__ import annotations

import os
from typing import Any

from kwikledgers_langgraph.adapters import IdentityResolverPort, NotificationPort


class EnvironmentIdentityResolverAdapter(IdentityResolverPort):
    def resolve_current_user_identity(self) -> dict[str, Any]:
        identity: dict[str, Any] = {}
        email = os.environ.get("KWIKLEDGERS_USER_EMAIL")
        display_name = os.environ.get("KWIKLEDGERS_USER_DISPLAY_NAME")
        if email:
            identity.update({"resolved_email": email, "email_source": "environment"})
        if display_name:
            identity["display_name"] = display_name
        return identity


class NoOpNotificationAdapter(NotificationPort):
    def send_daily_summary_notification(self, summary: dict[str, Any]) -> dict[str, Any]:
        return {"status": "skipped", "reason": "notifications_disabled", "counts": summary.get("counts", {})}