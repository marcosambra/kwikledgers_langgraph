from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from kwikledgers_langgraph.backend_factory import (
    BACKEND_ENV_VAR,
    FASTMCP_BACKEND,
    LEGACY_BACKEND,
    BackendSelectionError,
    build_daily_summary_ports,
    build_sprint_stories_ports,
    build_story_branch_association_ports,
    build_story_pull_request_ports,
    build_story_status_transition_ports,
    build_task_closure_ports,
    resolve_backend_name,
)
from kwikledgers_langgraph.fastmcp_adapters import (
    FastMCPDailySummaryProvider, FastMCPNotificationAdapter, FastMCPSprintStoriesProvider,
    FastMCPStoryBranchAssociationAdapter, FastMCPStoryPullRequestAdapter,
    FastMCPStoryStatusTransitionAdapter, FastMCPTaskClosureAdapter, FastMCPTrackingSyncAdapter,
)
from kwikledgers_langgraph.legacy_adapters import LegacyNotificationAdapter, NoOpNotificationAdapter


class BackendFactoryTestCase(unittest.TestCase):
    def test_resolve_backend_name_defaults_to_fastmcp(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_backend_name(), FASTMCP_BACKEND)

    def test_resolve_backend_name_reads_environment_override(self) -> None:
        with patch.dict(os.environ, {BACKEND_ENV_VAR: LEGACY_BACKEND}, clear=False):
            self.assertEqual(resolve_backend_name(), LEGACY_BACKEND)

    def test_resolve_backend_name_rejects_unknown_backend(self) -> None:
        with self.assertRaises(BackendSelectionError):
            resolve_backend_name("unknown")

    def test_build_daily_summary_ports_uses_legacy_notification_adapter_when_enabled(self) -> None:
        ports = build_daily_summary_ports(backend_name=LEGACY_BACKEND, notifications_enabled=True)

        self.assertIsInstance(ports.notifications, LegacyNotificationAdapter)

    def test_build_daily_summary_ports_uses_noop_notification_adapter_when_disabled(self) -> None:
        ports = build_daily_summary_ports(backend_name=LEGACY_BACKEND, notifications_enabled=False)

        self.assertIsInstance(ports.notifications, NoOpNotificationAdapter)

    def test_daily_fastmcp_shares_injected_client_and_token_provider(self) -> None:
        client = object()
        issuer = lambda name, arguments: "token"
        ports = build_daily_summary_ports(
            backend_name=FASTMCP_BACKEND, notifications_enabled=True, client=client,
            execution_capability_provider=issuer,
        )
        for adapter, expected in ((ports.provider, FastMCPDailySummaryProvider),
                                  (ports.tracking, FastMCPTrackingSyncAdapter),
                                  (ports.notifications, FastMCPNotificationAdapter)):
            self.assertIsInstance(adapter, expected)
            self.assertIs(adapter.client, client)
        self.assertIs(ports.tracking.execution_capability_provider, issuer)
        self.assertIs(ports.notifications.execution_capability_provider, issuer)

    def test_daily_fastmcp_disables_notifications_without_calling_client(self) -> None:
        ports = build_daily_summary_ports(backend_name=FASTMCP_BACKEND, notifications_enabled=False, client=object())
        self.assertIsInstance(ports.notifications, NoOpNotificationAdapter)

    def test_all_five_other_factories_support_fastmcp_and_preserve_legacy(self) -> None:
        client = object()
        for builder, attribute, expected in (
            (build_story_pull_request_ports, "pull_requests", FastMCPStoryPullRequestAdapter),
            (build_story_branch_association_ports, "branches", FastMCPStoryBranchAssociationAdapter),
            (build_story_status_transition_ports, "statuses", FastMCPStoryStatusTransitionAdapter),
            (build_task_closure_ports, "closures", FastMCPTaskClosureAdapter),
            (build_sprint_stories_ports, "stories", FastMCPSprintStoriesProvider),
        ):
            with self.subTest(builder=builder.__name__):
                adapter = getattr(builder(backend_name=FASTMCP_BACKEND, client=client), attribute)
                self.assertIsInstance(adapter, expected)
                self.assertIs(adapter.client, client)
                legacy = getattr(builder(backend_name=LEGACY_BACKEND), attribute)
                self.assertTrue(type(legacy).__name__.startswith("Legacy"))
                with self.assertRaises(BackendSelectionError):
                    builder(backend_name="unknown", client=client)

    def test_daily_rejects_unknown_backend(self) -> None:
        with self.assertRaises(BackendSelectionError):
            build_daily_summary_ports(backend_name="unknown", notifications_enabled=False)

    def test_fastmcp_default_client_is_lazy_no_connection_during_construction(self) -> None:
        with patch("kwikledgers_langgraph.fastmcp_client.FastMCPClient.call") as call:
            ports = build_daily_summary_ports(backend_name=FASTMCP_BACKEND, notifications_enabled=True)
        self.assertIsInstance(ports.provider, FastMCPDailySummaryProvider)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()