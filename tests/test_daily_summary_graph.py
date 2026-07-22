from __future__ import annotations

import importlib.util
import unittest
from typing import Any

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


if __name__ == "__main__":
    unittest.main()