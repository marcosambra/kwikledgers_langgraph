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
    resolve_backend_name,
)
from kwikledgers_langgraph.legacy_adapters import LegacyNotificationAdapter, NoOpNotificationAdapter


class BackendFactoryTestCase(unittest.TestCase):
    def test_resolve_backend_name_defaults_to_legacy(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            self.assertEqual(resolve_backend_name(), LEGACY_BACKEND)

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

    def test_build_daily_summary_ports_rejects_fastmcp_while_paused(self) -> None:
        with self.assertRaises(BackendSelectionError):
            build_daily_summary_ports(backend_name=FASTMCP_BACKEND, notifications_enabled=True)


if __name__ == "__main__":
    unittest.main()