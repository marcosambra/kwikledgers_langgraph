from __future__ import annotations

import importlib.util
import os
import unittest
from typing import Any
from unittest.mock import patch

from kwikledgers_langgraph.adapters import DailySummaryPorts
from kwikledgers_langgraph.observability import build_observability_context

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None

if LANGGRAPH_AVAILABLE:
    from kwikledgers_langgraph.graphs.daily_summary import build_daily_summary_graph


class FakeIdentityResolver:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls = 0

    def resolve_current_user_identity(self) -> dict[str, Any]:
        self.calls += 1
        return dict(self.result)


class FakeDailySummaryProvider:
    def fetch_daily_summary(self) -> dict[str, Any]:
        return {"counts": {"assigned_items": 3, "blocked_items": 1, "open_prs": 2}}


class FakeTrackingAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], str]] = []

    def sync_daily_tracking(self, summary: dict[str, Any], source: str) -> dict[str, Any]:
        self.calls.append((summary, source))
        return {"status": "synced", "source": source}


class FakeNotificationAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_daily_summary_notification(self, summary: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(summary)
        return {"status": "sent"}


class FastMCPTrackingAdapter(FakeTrackingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.execution_capability = ""

    def sync_daily_tracking(
        self, summary: dict[str, Any], source: str, *, execution_capability: str | None = None,
    ) -> dict[str, Any]:
        self.execution_capability = str(execution_capability or "")
        return super().sync_daily_tracking(summary, source)


class FastMCPNotificationAdapter(FakeNotificationAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.execution_capability = ""

    def send_daily_summary_notification(
        self, summary: dict[str, Any], *, execution_capability: str | None = None,
    ) -> dict[str, Any]:
        self.execution_capability = str(execution_capability or "")
        return super().send_daily_summary_notification(summary)


class FailingNotificationAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_daily_summary_notification(self, summary: dict[str, Any]) -> str:
        self.calls.append(summary)
        return "Erro ao enviar notificacao: powershell indisponivel"


def _base_state() -> dict[str, Any]:
    return {
        "source": "test-daily-summary",
        "notification_enabled": True,
        "tracking_events": [],
        "observability": build_observability_context(
            flow_name="daily_summary",
            source="test-daily-summary",
        ),
    }


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph não está instalado neste ambiente")
class DailySummaryGraphTestCase(unittest.TestCase):
    def test_resolves_identity_from_configured_port(self) -> None:
        identity = FakeIdentityResolver(
            {
                "resolved_email": "dev@kwikledgers.com",
                "display_name": "KwikLedgers Dev",
                "email_source": "environment",
            }
        )
        tracking = FakeTrackingAdapter()
        notifications = FakeNotificationAdapter()
        runtime = build_daily_summary_graph(
            DailySummaryPorts(
                identity=identity,
                provider=FakeDailySummaryProvider(),
                tracking=tracking,
                notifications=notifications,
            )
        )

        result = runtime.invoke(_base_state())

        self.assertEqual(identity.calls, 1)
        self.assertEqual(result["user_identity"]["resolved_email"], "dev@kwikledgers.com")
        self.assertEqual(result["tracking_events"][0]["payload"]["has_resolved_email"], True)
        self.assertNotIn("resolved_email", result["tracking_events"][0]["payload"])

    def test_existing_identity_skips_external_resolution(self) -> None:
        identity = FakeIdentityResolver(
            {
                "resolved_email": "ignored@kwikledgers.com",
                "display_name": "Ignored",
                "email_source": "environment",
            }
        )
        runtime = build_daily_summary_graph(
            DailySummaryPorts(
                identity=identity,
                provider=FakeDailySummaryProvider(),
                tracking=FakeTrackingAdapter(),
                notifications=FakeNotificationAdapter(),
            )
        )
        state = _base_state()
        state["user_identity"] = {
            "resolved_email": "state@kwikledgers.com",
            "display_name": "State User",
            "email_source": "state",
        }

        result = runtime.invoke(state)

        self.assertEqual(identity.calls, 0)
        self.assertEqual(result["user_identity"]["resolved_email"], "state@kwikledgers.com")

    def test_missing_identity_context_keeps_flow_running(self) -> None:
        identity = FakeIdentityResolver({})
        tracking = FakeTrackingAdapter()
        notifications = FakeNotificationAdapter()
        runtime = build_daily_summary_graph(
            DailySummaryPorts(
                identity=identity,
                provider=FakeDailySummaryProvider(),
                tracking=tracking,
                notifications=notifications,
            )
        )

        result = runtime.invoke(_base_state())

        self.assertEqual(identity.calls, 1)
        self.assertNotIn("user_identity", result)
        self.assertEqual(result["summary"]["counts"]["assigned_items"], 3)
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "notify_user")

    def test_notification_failure_is_recorded_as_failed_runtime_event(self) -> None:
        runtime = build_daily_summary_graph(
            DailySummaryPorts(
                identity=FakeIdentityResolver({}),
                provider=FakeDailySummaryProvider(),
                tracking=FakeTrackingAdapter(),
                notifications=FailingNotificationAdapter(),
            )
        )

        result = runtime.invoke(_base_state())

        self.assertIn("errors", result)
        self.assertIn("Erro ao enviar notificacao", result["errors"][0])
        self.assertEqual(result["observability"]["timeline"][-1]["status"], "failed")
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "notify_user")

    def test_fastmcp_internal_effects_receive_runtime_capabilities(self) -> None:
        tracking = FastMCPTrackingAdapter()
        notifications = FastMCPNotificationAdapter()
        runtime = build_daily_summary_graph(
            DailySummaryPorts(
                identity=FakeIdentityResolver({}),
                provider=FakeDailySummaryProvider(),
                tracking=tracking,
                notifications=notifications,
            )
        )
        state = _base_state()
        state["observability"]["backend_name"] = "fastmcp"

        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "test-key"}, clear=False):
            runtime.invoke(state)

        self.assertIn(".", tracking.execution_capability)
        self.assertIn(".", notifications.execution_capability)

    def test_notification_exception_preserves_summary_and_tracking(self) -> None:
        for backend in ("legacy", "fastmcp"):
            with self.subTest(backend=backend):
                tracking = FastMCPTrackingAdapter()
                notifications = FastMCPNotificationAdapter()
                runtime = build_daily_summary_graph(DailySummaryPorts(
                    provider=FakeDailySummaryProvider(), tracking=tracking, notifications=notifications,
                ))
                state = _base_state()
                state["observability"]["backend_name"] = backend
                with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "test-key"}), \
                     patch.object(notifications, "send_daily_summary_notification", side_effect=ValueError("notify failed")):
                    result = runtime.invoke(state)
                self.assertEqual(result["summary"]["counts"]["assigned_items"], 3)
                self.assertEqual(len(tracking.calls), 1)
                self.assertIn("notify failed", result["errors"][-1])
                self.assertEqual(result["observability"]["timeline"][-1]["status"], "failed")

    def test_notification_issuance_exception_is_best_effort(self) -> None:
        tracking = FastMCPTrackingAdapter()
        notifications = FastMCPNotificationAdapter()
        runtime = build_daily_summary_graph(DailySummaryPorts(
            provider=FakeDailySummaryProvider(), tracking=tracking, notifications=notifications,
        ))
        state = _base_state()
        state["observability"]["backend_name"] = "fastmcp"
        with patch("kwikledgers_langgraph.graphs.daily_summary.issue_runtime_execution_capability",
                   side_effect=["tracking-token", ValueError("notification issuance failed")]):
            result = runtime.invoke(state)
        self.assertEqual(len(tracking.calls), 1)
        self.assertEqual(notifications.calls, [])
        self.assertIn("notification issuance failed", result["errors"][-1])
        self.assertEqual(result["observability"]["timeline"][-1]["node"], "notify_user")


if __name__ == "__main__":
    unittest.main()