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
    LegacyTaskClosureAdapter,
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
        self.assertEqual(preview["story_revision"], "story-status-120204-Desenvolvimento")

    def test_story_status_preview_uses_native_story_revision_when_legacy_client_is_available(self) -> None:
        adapter = LegacyStoryStatusTransitionAdapter()

        class FakeWorkItemTrackingClient:
            def get_work_item(self, *, id: int, fields: list[str]) -> object:
                self.requested_id = id
                self.requested_fields = list(fields)
                return types.SimpleNamespace(
                    fields={
                        "System.Title": "Story",
                        "System.State": "Desenvolvimento",
                        "System.WorkItemType": "User Story",
                    },
                    rev=27,
                    url="https://dev.azure.com/kwikledgers/story/120204",
                )

        fake_wit = FakeWorkItemTrackingClient()
        fake_connection = types.SimpleNamespace(
            clients=types.SimpleNamespace(get_work_item_tracking_client=lambda: fake_wit)
        )

        with patch("kwikledgers_langgraph.legacy_adapters._ensure_legacy_import_path"):
            with patch.dict(os.environ, {}, clear=False):
                fake_package = types.ModuleType("azure_devops")
                fake_server = types.ModuleType("azure_devops.server")
                fake_server.get_client = lambda: fake_connection
                fake_server._get_story_details = lambda story_id: "fallback should not run"
                fake_package.server = fake_server
                with patch.dict(sys.modules, {"azure_devops": fake_package, "azure_devops.server": fake_server}):
                    preview = adapter.preview_story_status_transition(
                        {
                            "story_id": 120204,
                            "target_status": "Aguardando PR",
                            "expected_current_status": "Desenvolvimento",
                        }
                    )

        self.assertTrue(preview["transition_allowed"])
        self.assertEqual(preview["story_revision"], "27")
        self.assertEqual(preview["url"], "https://dev.azure.com/kwikledgers/story/120204")

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

    def test_task_closure_preview_computes_hours_with_ceil(self) -> None:
        adapter = LegacyTaskClosureAdapter()

        preview = adapter.preview_task_closure(
            {
                "task_id": 120310,
                "start_time": "2026-07-10T11:00:00+00:00",
                "close_time": "2026-07-10T11:00:01+00:00",
            }
        )

        self.assertEqual(preview["computed_hours"], 1)
        self.assertEqual(preview["completed_work_hours"], 1.0)
        self.assertIsNone(preview["remaining_work_hours"])
        self.assertTrue(preview["closure_allowed"])
        self.assertEqual(preview["validation_messages"], [])

    def test_task_closure_preview_respects_explicit_hours_override(self) -> None:
        adapter = LegacyTaskClosureAdapter()

        preview = adapter.preview_task_closure(
            {
                "task_id": 120310,
                "start_time": "2026-07-10T11:00:00+00:00",
                "close_time": "2026-07-10T13:00:00+00:00",
                "completed_work_hours": 5.5,
                "remaining_work_hours": 2.0,
            }
        )

        self.assertEqual(preview["computed_hours"], 2)
        self.assertEqual(preview["completed_work_hours"], 5.5)
        self.assertEqual(preview["remaining_work_hours"], 2.0)

    def test_task_closure_preview_blocks_close_time_before_start_time(self) -> None:
        adapter = LegacyTaskClosureAdapter()

        preview = adapter.preview_task_closure(
            {
                "task_id": 120310,
                "start_time": "2026-07-10T11:00:00+00:00",
                "close_time": "2026-07-10T09:00:00+00:00",
            }
        )

        self.assertFalse(preview["closure_allowed"])
        self.assertIn(
            "O horario de fechamento nao pode ser anterior ao horario de inicio.",
            preview["validation_messages"],
        )

    def test_task_closure_close_task_rejects_when_preview_not_allowed(self) -> None:
        adapter = LegacyTaskClosureAdapter()

        with self.assertRaises(RuntimeError):
            adapter.close_task(
                {
                    "task_id": 120310,
                    "preview": {"closure_allowed": False, "validation_messages": ["boom"]},
                }
            )

    def test_task_closure_close_task_updates_effort_and_status(self) -> None:
        adapter = LegacyTaskClosureAdapter()
        effort_calls: list[dict[str, object]] = []
        status_calls: list[dict[str, object]] = []

        with patch("kwikledgers_langgraph.legacy_adapters._ensure_legacy_import_path"):
            fake_package = types.ModuleType("azure_devops")
            fake_server = types.ModuleType("azure_devops.server")

            def fake_update_effort(**kwargs: object) -> str:
                effort_calls.append(kwargs)
                return "effort updated"

            def fake_update_status(*, story_id: int, status: str) -> str:
                status_calls.append({"story_id": story_id, "status": status})
                return f"Historia KL-{story_id} atualizada para: {status}"

            fake_server._update_work_item_effort = fake_update_effort
            fake_server._update_story_status = fake_update_status
            fake_package.server = fake_server
            with patch.dict(sys.modules, {"azure_devops": fake_package, "azure_devops.server": fake_server}):
                result = adapter.close_task(
                    {
                        "task_id": 120310,
                        "preview": {
                            "closure_allowed": True,
                            "completed_work_hours": 2.0,
                            "remaining_work_hours": None,
                            "comment": "fechamento automatico",
                        },
                    }
                )

        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["task_id"], 120310)
        self.assertEqual(result["completed_work_hours"], 2.0)
        self.assertEqual(len(effort_calls), 1)
        self.assertEqual(effort_calls[0]["work_item_id"], 120310)
        self.assertEqual(effort_calls[0]["completed_work_hours"], 2.0)
        self.assertIsNone(effort_calls[0]["remaining_work_hours"])
        self.assertEqual(status_calls, [{"story_id": 120310, "status": "Closed"}])

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