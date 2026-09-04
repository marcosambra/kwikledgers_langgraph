from __future__ import annotations

import os
import sys
import tempfile
import unittest
import types
from pathlib import Path
from unittest.mock import patch

from kwikledgers_langgraph.legacy_adapters import (
    EnvironmentIdentityResolverAdapter,
    LegacyStoryBranchAssociationAdapter,
    LegacyStoryPullRequestAdapter,
    LegacyStoryStatusTransitionAdapter,
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

    def test_resolve_mcp_path_rejects_invalid_explicit_path(self) -> None:
        with patch.dict(
            os.environ,
            {"KWIKLEDGERS_MCP_SERVERS_PATH": "/path/that/does/not/exist"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                _resolve_mcp_path()

    def test_story_status_preview_blocks_jump_outside_canonical_flow(self) -> None:
        adapter = LegacyStoryStatusTransitionAdapter()

        with patch("kwikledgers_langgraph.legacy_adapters._ensure_legacy_import_path"):
            with patch.dict(os.environ, {}, clear=False):
                fake_package = types.ModuleType("azure_devops")
                fake_server = types.ModuleType("azure_devops.server")
                fake_server._get_story_details = lambda story_id: "=== KL-120204: Story ===\nStatus: Desenvolvimento\n"
                fake_package.server = fake_server
                with patch.dict(sys.modules, {"azure_devops": fake_package, "azure_devops.server": fake_server}):
                    preview = adapter.preview_story_status_transition(
                        {"story_id": 120204, "target_status": "QA", "expected_current_status": "Desenvolvimento"}
                    )

        self.assertFalse(preview["transition_allowed"])
        self.assertEqual(preview["current_status"], "Desenvolvimento")

    def test_story_status_update_rejects_revision_change_between_preview_and_execute(self) -> None:
        adapter = LegacyStoryStatusTransitionAdapter()

        with patch.object(
            adapter,
            "preview_story_status_transition",
            side_effect=[
                {
                    "story_id": 120204,
                    "current_status": "Desenvolvimento",
                    "target_status": "Aguardando PR",
                    "transition_allowed": True,
                    "validation_messages": [],
                    "story_revision": "rev-2",
                }
            ],
        ):
            with self.assertRaises(RuntimeError):
                adapter.update_story_status(
                    {
                        "story_id": 120204,
                        "target_status": "Aguardando PR",
                        "preview": {
                            "story_id": 120204,
                            "current_status": "Desenvolvimento",
                            "target_status": "Aguardando PR",
                            "transition_allowed": True,
                            "validation_messages": [],
                            "story_revision": "rev-1",
                        },
                    }
                )

    def test_story_branch_preview_rejects_invalid_json(self) -> None:
        adapter = LegacyStoryBranchAssociationAdapter()

        with patch("kwikledgers_langgraph.legacy_adapters._ensure_legacy_import_path"):
            fake_package = types.ModuleType("azure_devops")
            fake_server = types.ModuleType("azure_devops.server")
            fake_server._preview_story_branch_association = lambda **_kwargs: "{"
            fake_package.server = fake_server
            with patch.dict(sys.modules, {"azure_devops": fake_package, "azure_devops.server": fake_server}):
                with self.assertRaises(RuntimeError):
                    adapter.preview_story_branch_association(
                        {"story_id": 1, "repository_name": "accounting", "source_branch": "feature/foo"}
                    )

    def test_story_pull_request_preview_rejects_non_dict_root(self) -> None:
        adapter = LegacyStoryPullRequestAdapter()

        with patch("kwikledgers_langgraph.legacy_adapters._ensure_legacy_import_path"):
            fake_package = types.ModuleType("azure_devops")
            fake_server = types.ModuleType("azure_devops.server")
            fake_server._preview_story_pull_request = lambda **_kwargs: "[]"
            fake_package.server = fake_server
            with patch.dict(sys.modules, {"azure_devops": fake_package, "azure_devops.server": fake_server}):
                with self.assertRaises(RuntimeError):
                    adapter.preview_story_pull_request(
                        {
                            "story_id": 1,
                            "repository_name": "accounting",
                            "source_branch": "feature/foo",
                            "what_was_changed": "x",
                            "affected_processes": "x",
                            "expected_impacts": "x",
                            "important_points": "x",
                            "mermaid_diagram": "graph TD; A-->B;",
                        }
                    )

    def test_story_branch_preview_propagates_legacy_error(self) -> None:
        adapter = LegacyStoryBranchAssociationAdapter()

        with patch("kwikledgers_langgraph.legacy_adapters._ensure_legacy_import_path"):
            fake_package = types.ModuleType("azure_devops")
            fake_server = types.ModuleType("azure_devops.server")

            def raise_error(**_kwargs: object) -> str:
                raise RuntimeError("legacy branch preview boom")

            fake_server._preview_story_branch_association = raise_error
            fake_package.server = fake_server
            with patch.dict(sys.modules, {"azure_devops": fake_package, "azure_devops.server": fake_server}):
                with self.assertRaises(RuntimeError):
                    adapter.preview_story_branch_association(
                        {"story_id": 1, "repository_name": "accounting", "source_branch": "feature/foo"}
                    )

    def test_story_pull_request_create_propagates_legacy_error(self) -> None:
        adapter = LegacyStoryPullRequestAdapter()

        with patch("kwikledgers_langgraph.legacy_adapters._ensure_legacy_import_path"):
            fake_package = types.ModuleType("azure_devops")
            fake_server = types.ModuleType("azure_devops.server")

            def raise_error(**_kwargs: object) -> str:
                raise RuntimeError("legacy pr create boom")

            fake_server._create_story_pull_request = raise_error
            fake_package.server = fake_server
            with patch.dict(sys.modules, {"azure_devops": fake_package, "azure_devops.server": fake_server}):
                with self.assertRaises(RuntimeError):
                    adapter.create_story_pull_request(
                        {
                            "story_id": 1,
                            "repository_name": "accounting",
                            "source_branch": "feature/foo",
                            "what_was_changed": "x",
                            "affected_processes": "x",
                            "expected_impacts": "x",
                            "important_points": "x",
                            "mermaid_diagram": "graph TD; A-->B;",
                        }
                    )


if __name__ == "__main__":
    unittest.main()