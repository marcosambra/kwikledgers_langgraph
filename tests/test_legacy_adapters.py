from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kwikledgers_langgraph.legacy_adapters import (
    EnvironmentIdentityResolverAdapter,
    NoOpNotificationAdapter,
    _resolve_mcp_path,
)


class LegacyAdaptersTestCase(unittest.TestCase):
    def test_environment_identity_resolver_returns_empty_context_without_vars(self) -> None:
        adapter = EnvironmentIdentityResolverAdapter()

        with patch.dict(os.environ, {}, clear=True):
            result = adapter.resolve_current_user_identity()

        self.assertEqual(result, {})

    def test_environment_identity_resolver_reads_explicit_identity_vars(self) -> None:
        adapter = EnvironmentIdentityResolverAdapter()

        with patch.dict(
            os.environ,
            {
                "KWIKLEDGERS_USER_EMAIL": "dev@kwikledgers.com",
                "KWIKLEDGERS_USER_DISPLAY_NAME": "KwikLedgers Dev",
            },
            clear=True,
        ):
            result = adapter.resolve_current_user_identity()

        self.assertEqual(result["resolved_email"], "dev@kwikledgers.com")
        self.assertEqual(result["display_name"], "KwikLedgers Dev")
        self.assertEqual(result["email_source"], "environment")

    def test_noop_notification_adapter_reports_skipped_status(self) -> None:
        adapter = NoOpNotificationAdapter()

        result = adapter.send_daily_summary_notification({"counts": {"assigned_items": 2}})

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["counts"]["assigned_items"], 2)

    def test_resolve_mcp_path_uses_explicit_env_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            explicit_path = Path(temp_dir)
            with patch.dict(os.environ, {"KWIKLEDGERS_MCP_SERVERS_PATH": str(explicit_path)}, clear=False):
                resolved = _resolve_mcp_path()

        self.assertEqual(resolved, explicit_path.resolve())


if __name__ == "__main__":
    unittest.main()