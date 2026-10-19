from copy import deepcopy
from datetime import timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from langgraph.types import Command

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.atomic_runtime import (
    ATOMIC_ACTION_CAPABILITIES, CAPABILITY_CATALOG, LIFECYCLE_ROUTES,
    MUTATION_SCHEMAS, PREVIEW_SCHEMAS, READ_SCHEMAS, REVISION_TARGETS,
    AtomicLifecycleGateway, LifecycleRequired, build_atomic_runtime, read_capability, validate_arguments,
)
from kwikledgers_langgraph.lifecycle_adapters import (
    LifecycleGateway, LifecycleJournal, ExplicitLocalApproval, active_state, lifecycle_scope,
)
from kwikledgers_langgraph.lifecycle_runtime import SignedLifecycleCheckpointer
from test_lifecycle_adapters import APPROVAL_KEY, NOW, approved_state, receipt_for


PAYLOADS = {
    "add_story_comment": {"story_id": 10, "comment": "Reviewed scope."},
    "move_work_item_to_next_sprint": {"work_item_id": 10, "comment": "Carry over."},
    "update_work_item_content": {"work_item_id": 10, "description": "Reviewed description."},
    "create_calendar_event": {"title": "Review", "start": "2026-10-02T09:00:00+00:00",
                               "end": "2026-10-02T10:00:00+00:00"},
    "schedule_reminder": {"title": "Review", "remind_at": "2026-10-02T09:00:00+00:00", "message": "Review."},
    "send_notification": {"title": "Review", "message": "Ready."},
    "update_task_control": {"summary": '{"items": []}'},
    "record_ai_metrics": {"entry_title": "Review", "summary": "Reviewed.", "actions_taken": ["review"]},
    "save_technical_debt_preview_cache": {"repository_name": "payment", "title": "Prepared debt", "story_points": 3,
        "child_tasks": [{"title": "Implementation", "description": "Implement.", "remaining_work_hours": 2}]},
}
READ_PAYLOADS = {
    "get_user_stories": {"email": "operator@example.com"},
    "get_open_prs": {"email": "operator@example.com"},
    "get_story_details": {"story_id": 10},
    "find_similar_technical_debts": {"repository_name": "payment", "title": "Debt"},
    "preview_story_branch_association": {"story_id": 10, "repository_name": "payment",
                                         "source_branch": "feature/KL-10-story"},
    "preview_story_pull_request": {"story_id": 10, "repository_name": "payment",
        "source_branch": "feature/KL-10-story", "what_was_changed": "Change.",
        "affected_processes": "Process.", "expected_impacts": "Impact.", "important_points": "Point.",
        "mermaid_diagram": "flowchart LR\n A-->B"},
}


class AtomicNativeClient:
    def __init__(self):
        self.calls = []
        self.effects = []
        self.details = {"id": 10, "revision": 4, "work_item_type": "User Story", "state": "Desenvolvimento"}
        self.failure = False
        self.invalid_result = False
        self.details_hook = None

    def call(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if name == "get_story_details":
            if self.details_hook:
                self.details_hook()
            return deepcopy(self.details)
        if name in READ_SCHEMAS or name in PREVIEW_SCHEMAS:
            return {"native": name, "arguments": deepcopy(arguments)}
        if name not in MUTATION_SCHEMAS:
            raise AssertionError("Unexpected capability")
        if not arguments.get("execution_capability"):
            raise AssertionError("Mutation without execution capability")
        if name in REVISION_TARGETS and arguments["expected_revision"] != self.details["revision"]:
            raise RuntimeError("Native revision conflict")
        self.effects.append((name, deepcopy(arguments)))
        if name in REVISION_TARGETS:
            self.details["revision"] += 1
        if self.failure:
            raise TimeoutError("Unknown outcome")
        if name == "update_work_item_content" and "title" in arguments:
            return {"executed": True, "id": self.details["id"], "title": arguments["title"],
                    "revision": self.details["revision"]}
        return [] if self.invalid_result else {"executed": True, "native": name}


class AtomicRuntimeHarness:
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "atomic.db"
        self.journal = LifecycleJournal(self.path, b"j" * 32)
        self.addCleanup(lambda: self.journal.close())
        self.now = NOW
        self.client = AtomicNativeClient()
        self.issued = []
        self.approvals = ExplicitLocalApproval(APPROVAL_KEY, clock=lambda: self.now)
        approval_clock = patch("kwikledgers_langgraph.lifecycle_cli.ExplicitLocalApproval",
                               side_effect=lambda: ExplicitLocalApproval(clock=lambda: self.now))
        approval_clock.start()
        self.addCleanup(approval_clock.stop)
        self.gateway = LifecycleGateway(self.client, self.journal, self.issue, Mock())
        self.tracking = Mock()
        self.tracking.record.side_effect = lambda event, key: self.journal.put(key + ":event", event)
        self.ports = LifecyclePorts(context=Mock(), tracking=self.tracking, approvals=self.approvals,
            clock=SimpleNamespace(now=lambda: self.now.isoformat()),
            branches=SimpleNamespace(path=Mock(return_value=Path(self.directory.name))),
            work_items=SimpleNamespace(gateway=self.gateway))
        self.checkpoint = SignedLifecycleCheckpointer(self.journal)

    def issue(self, state, *, action, capability_name, arguments):
        trusted = active_state(action)
        self.assertEqual(state["verified_lifecycle_binding"], trusted["verified_lifecycle_binding"])
        self.assertEqual(ATOMIC_ACTION_CAPABILITIES[action], capability_name)
        preview = deepcopy(state["verified_lifecycle_binding"]["preview"])
        preview.pop("item_context", None)
        self.assertEqual(preview, arguments)
        self.issued.append((action, capability_name, deepcopy(arguments)))
        return "test:execution-capability"

    def graph(self, name, arguments=None):
        return build_atomic_runtime(name, self.ports, checkpointer=self.checkpoint,
                                    arguments=deepcopy(PAYLOADS[name] if arguments is None else arguments))

    def preview(self, name="add_story_comment", arguments=None):
        graph = self.graph(name, arguments)
        config = {"configurable": {"thread_id": self.id() + ":" + name}}
        first = graph.invoke({"request": {}}, config)
        return graph, config, first

    def approve(self, graph, config, first, receipt=None):
        return graph.invoke(Command(resume=receipt if receipt is not None else
                                    receipt_for(first["pending_approval"], issued=self.now)), config)


class AtomicTitleTests(AtomicRuntimeHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.client.details.update(
            work_item_type="Task", title="Combined task",
            browser_url="https://dev.azure.com/org/Project/_workitems/edit/10",
        )
        self.arguments = {"work_item_id": 10, "title": "[FETCH BANK] Paginate transactions"}

    def test_title_preview_shows_current_and_proposed_title_without_writes(self):
        _, _, first = self.preview("update_work_item_content", self.arguments)
        preview = first["pending_approval"]["preview"]
        self.assertEqual(preview["title"], self.arguments["title"])
        self.assertEqual(preview["expected_revision"], 4)
        self.assertEqual(preview["item_context"], {
            "current_title": "Combined task", "work_item_type": "Task",
            "browser_url": self.client.details["browser_url"],
        })
        self.assertTrue(first.get("__interrupt__"))
        self.assertEqual(self.client.effects, [])
        self.assertEqual(self.issued, [])

    def test_title_edit_requires_nonempty_single_line_text(self):
        for title in ("", "  ", "\n", "Title\nOther", "Title\rOther", None, 123, True):
            with self.subTest(title=title), self.assertRaises(ValueError):
                validate_arguments("update_work_item_content", {**self.arguments, "title": title})

    def test_title_edit_only_supports_tasks(self):
        for kind in ("User Story", "Bug", "Feature", "Epic", "Technical Debt"):
            with self.subTest(kind=kind):
                self.client.details["work_item_type"] = kind
                graph = self.graph("update_work_item_content", self.arguments)
                first = graph.invoke({"request": {}}, {"configurable": {"thread_id": self.id() + kind}})
                self.assertEqual(first["outcome"], "blocked")
        self.assertEqual(self.client.effects, [])

    def test_title_edit_requires_authoritative_display_context(self):
        for field in ("title", "browser_url"):
            with self.subTest(field=field):
                original = self.client.details.pop(field)
                graph = self.graph("update_work_item_content", self.arguments)
                first = graph.invoke({"request": {}}, {"configurable": {"thread_id": self.id() + field}})
                self.assertEqual(first["outcome"], "blocked")
                self.client.details[field] = original
        self.assertEqual(self.client.effects, [])

    def test_title_edit_approved_payload_excludes_display_metadata(self):
        graph, config, first = self.preview("update_work_item_content", {
            **self.arguments, "description": "<p>Backend scope</p>",
        })
        result = self.approve(graph, config, first)
        self.assertEqual(result["outcome"], "completed")
        name, payload = self.client.effects[0]
        self.assertEqual(name, "update_work_item_content")
        self.assertEqual(payload, {
            **self.arguments, "description": "<p>Backend scope</p>", "expected_revision": 4,
            "execution_capability": "test:execution-capability",
        })
        self.assertNotIn("item_context", self.issued[0][2])
        self.assertEqual(graph.invoke(None, config)["outcome"], "completed")
        self.assertEqual(len(self.client.effects), 1)

    def test_title_edit_rejects_boolean_approval(self):
        graph, config, first = self.preview("update_work_item_content", self.arguments)
        self.assertEqual(self.approve(graph, config, first, {"approved": True})["outcome"], "denied")
        self.assertEqual(self.client.effects, [])

    def test_title_edit_rechecks_revision_and_item_type(self):
        for field, value in (("revision", 5), ("work_item_type", "Bug")):
            with self.subTest(field=field):
                graph = self.graph("update_work_item_content", self.arguments)
                config = {"configurable": {"thread_id": self.id() + field}}
                first = graph.invoke({"request": {}}, config)
                original = self.client.details[field]
                self.client.details[field] = value
                self.assertEqual(self.approve(graph, config, first)["outcome"], "blocked")
                self.client.details[field] = original
        self.assertEqual(self.issued, [])
        self.assertEqual(self.client.effects, [])

    def test_title_edit_metadata_cannot_be_supplied_as_business_arguments(self):
        with self.assertRaises(ValueError):
            validate_arguments("update_work_item_content", {**self.arguments, "item_context": {}})

    def test_title_edit_cannot_resume_a_different_title_or_display_context(self):
        _, config, first = self.preview("update_work_item_content", self.arguments)
        changed = self.graph("update_work_item_content", {**self.arguments, "title": "Unapproved"})
        self.assertEqual(self.approve(changed, config, first)["outcome"], "blocked")
        self.assertEqual(self.client.effects, [])

    def test_title_edit_unknown_result_requires_reconciliation_and_is_not_retried(self):
        graph, config, first = self.preview("update_work_item_content", self.arguments)
        self.client.failure = True
        result = self.approve(graph, config, first)
        self.assertEqual(result["outcome"], "reconciliation_required")
        self.assertIsNone(self.journal.get(first["run_id"] + ":atomic_update_work_item_content:result"))
        self.assertIsNotNone(self.journal.get(first["run_id"] + ":atomic_update_work_item_content:intent"))
        self.assertEqual(graph.invoke(None, config)["outcome"], "reconciliation_required")
        self.assertEqual(len(self.client.effects), 1)

    def test_title_edit_server_error_requires_reconciliation(self):
        from kwikledgers_langgraph.fastmcp_client import FastMCPClient

        graph, config, first = self.preview("update_work_item_content", self.arguments)
        original = self.client.call

        def server_error(name, arguments):
            if name == "update_work_item_content":
                original(name, arguments)
                return FastMCPClient._normalize(SimpleNamespace(isError=True), name)
            return original(name, arguments)

        with patch.object(self.client, "call", side_effect=server_error):
            result = self.approve(graph, config, first)
        self.assertEqual(result["outcome"], "reconciliation_required")
        self.assertEqual(len(self.client.effects), 1)
        self.assertIsNone(self.journal.get(first["run_id"] + ":atomic_update_work_item_content:result"))

    def test_title_edit_unconfirmed_native_title_is_not_successful(self):
        graph, config, first = self.preview("update_work_item_content", self.arguments)
        original = self.client.call

        def different_title(name, arguments):
            result = original(name, arguments)
            return {**result, "title": "Unexpected"} if name == "update_work_item_content" else result

        with patch.object(self.client, "call", side_effect=different_title):
            result = self.approve(graph, config, first)
        self.assertEqual(result["outcome"], "reconciliation_required")
        self.assertEqual(len(self.client.effects), 1)


class AtomicRuntimeTests(AtomicRuntimeHarness, TestCase):
    def test_catalog_covers_all_declared_capabilities(self):
        expected = {"get_active_user", "get_my_work_items", "get_my_blocked_items", "get_my_daily_summary",
            "get_user_stories", "get_sprint_stories", "get_sprint_stories_detailed", "get_story_details", "get_open_prs",
            "preview_story_branch_association", "preview_story_pull_request", "associate_story_branch",
            "create_story_pull_request", "create_work_item", "find_similar_technical_debts",
            "save_technical_debt_preview_cache", "update_work_item_content", "update_work_item_effort",
            "move_work_item_to_next_sprint", "update_story_status", "add_story_comment", "sync_daily_tracking",
            "update_task_control", "append_daily_action_log", "record_ai_metrics", "read_tracking_snapshot",
            "send_notification", "create_calendar_event", "get_upcoming_deadlines", "schedule_reminder"}
        self.assertEqual(set(CAPABILITY_CATALOG), expected | {"get_sprint_work_report", "get_work_item_process_metadata"})
        self.assertEqual((len(READ_SCHEMAS), len(PREVIEW_SCHEMAS), len(MUTATION_SCHEMAS), len(LIFECYCLE_ROUTES)), (14, 2, 9, 7))

    def test_all_reads_and_previews_call_native_directly(self):
        for name in (*READ_SCHEMAS, *PREVIEW_SCHEMAS):
            with self.subTest(name=name):
                payload = READ_PAYLOADS.get(name, {})
                result = read_capability(self.client, name, payload)
                self.assertIsInstance(result, dict)
                self.assertEqual(self.client.calls[-1], (name, payload))
        self.assertEqual(self.issued, [])
        self.assertEqual(self.client.effects, [])

    def test_all_atomic_mutations_require_external_receipts(self):
        for name in MUTATION_SCHEMAS:
            with self.subTest(name=name):
                previous = len(self.client.effects)
                graph, config, first = self.preview(name)
                self.assertTrue(first.get("__interrupt__"))
                self.assertEqual(len(self.client.effects), previous)
                self.assertEqual(first["pending_approval"]["action"], "atomic_" + name)
                payload = deepcopy(PAYLOADS[name])
                if name in REVISION_TARGETS:
                    payload["expected_revision"] = self.client.details["revision"]
                self.assertEqual(first["pending_approval"]["preview"], payload)
                result = self.approve(graph, config, first)
                self.assertEqual(result["outcome"], "completed")
                self.assertEqual(len(self.client.effects), previous + 1)
                self.assertEqual(self.issued[-1], ("atomic_" + name, name, payload))
                key = first["run_id"] + ":atomic_" + name
                self.assertIsNotNone(self.journal.get(key + ":intent"))
                self.assertIsNotNone(self.journal.get(key + ":result"))
        self.gateway.runtime_issuer.assert_not_called()

    def test_governed_capabilities_require_specific_lifecycles(self):
        for name, route in LIFECYCLE_ROUTES.items():
            with self.subTest(name=name):
                with self.assertRaises(LifecycleRequired) as error:
                    build_atomic_runtime(name, self.ports, checkpointer=self.checkpoint, arguments={})
                self.assertIn(route, str(error.exception))
                with self.assertRaises(LifecycleRequired):
                    validate_arguments(name, {})
        self.assertEqual(self.client.calls, [])

    def test_mutations_cannot_be_dispatched_as_reads(self):
        for name in MUTATION_SCHEMAS:
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_capability(self.client, name, PAYLOADS[name])
        self.assertEqual(self.client.calls, [])

    def test_workflow_and_approval_fields_are_rejected_for_every_schema(self):
        for name in (*READ_SCHEMAS, *PREVIEW_SCHEMAS, *MUTATION_SCHEMAS):
            base = PAYLOADS.get(name, READ_PAYLOADS.get(name, {}))
            for field in ("approved", "approval_confirmation", "execution_capability", "pending_approval", "run_id",
                          "request_id", "active_run_id", "approval_simulation_requested", "expected_revision"):
                with self.subTest(name=name, field=field), self.assertRaises(ValueError):
                    validate_arguments(name, {**base, field: True})

    def test_content_schema_rejects_governed_fields(self):
        for field in ("status", "assigned_to", "area_path", "completed_work_hours", "remaining_work_hours",
                      "original_estimate_hours", "story_points", "iteration_path", "parent_work_item_id"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.graph("update_work_item_content", {**PAYLOADS["update_work_item_content"], field: "Active"})
        self.assertEqual(self.client.calls, [])

    def test_content_requires_nonempty_edit(self):
        for payload in ({"work_item_id": 10}, {"work_item_id": 10, "comment": "  "}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_arguments("update_work_item_content", payload)

    def test_content_does_not_bypass_technical_debt_lifecycle(self):
        self.client.details["work_item_type"] = "Technical Debt"
        _, _, first = self.preview("update_work_item_content")
        self.assertEqual(first["outcome"], "blocked")
        self.assertEqual(first["pending_approval"], {})
        self.assertEqual(self.client.effects, [])

    def test_unknown_content_item_type_fails_closed(self):
        self.client.details.pop("work_item_type")
        _, _, first = self.preview("update_work_item_content")
        self.assertEqual(first["outcome"], "blocked")

    def test_content_type_is_rechecked_before_execution(self):
        graph, config, first = self.preview("update_work_item_content")
        self.client.details["work_item_type"] = "Technical Debt"
        self.assertEqual(self.approve(graph, config, first)["outcome"], "blocked")
        self.assertEqual(self.issued, [])

    def test_stale_revisions_block_all_azure_atomic_mutations(self):
        for name in REVISION_TARGETS:
            with self.subTest(name=name):
                graph, config, first = self.preview(name)
                self.client.details["revision"] += 1
                self.assertEqual(self.approve(graph, config, first)["outcome"], "blocked")
        self.assertEqual(self.issued, [])
        self.assertEqual(self.client.effects, [])

    def test_native_identity_and_revision_are_required(self):
        for field, value in (("id", 11), ("id", True), ("revision", 0), ("revision", True), ("revision", "4")):
            with self.subTest(field=field, value=value):
                self.client.details = {"id": 10, "revision": 4, "work_item_type": "User Story"}
                self.client.details[field] = value
                graph = self.graph("add_story_comment")
                result = graph.invoke({"request": {}}, {"configurable": {"thread_id": self.id() + repr((field, value))}})
                self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(self.client.effects, [])

    def test_unsigned_boolean_approval_is_denied(self):
        graph, config, first = self.preview()
        result = self.approve(graph, config, first, {"approved": True})
        self.assertEqual(result["outcome"], "denied")
        self.assertEqual(self.client.effects, [])

    def test_tampered_receipt_is_denied(self):
        graph, config, first = self.preview()
        receipt = receipt_for(first["pending_approval"])
        receipt["action"] = "atomic_send_notification"
        self.assertEqual(self.approve(graph, config, first, receipt)["outcome"], "denied")
        self.assertEqual(self.issued, [])

    def test_expired_receipt_is_denied(self):
        graph, config, first = self.preview()
        receipt = receipt_for(first["pending_approval"])
        self.now -= timedelta(seconds=1)
        self.assertEqual(self.approve(graph, config, first, receipt)["outcome"], "denied")
        self.assertEqual(self.client.effects, [])

    def test_receipt_expiring_during_fresh_revision_read_blocks_execution(self):
        graph, config, first = self.preview()
        self.client.details_hook = lambda: setattr(self, "now", self.now - timedelta(seconds=1))
        receipt = receipt_for(first["pending_approval"])
        self.assertEqual(self.approve(graph, config, first, receipt)["outcome"], "blocked")
        self.assertEqual(self.issued, [])

    def test_checkpoint_restart_retains_same_run_and_approval(self):
        _, config, first = self.preview()
        self.journal.close()
        self.journal = LifecycleJournal(self.path, b"j" * 32)
        self.gateway.journal = self.journal
        self.checkpoint = SignedLifecycleCheckpointer(self.journal)
        graph = self.graph("add_story_comment")
        result = self.approve(graph, config, first)
        self.assertEqual(result["run_id"], first["run_id"])
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(len(self.client.effects), 1)

    def test_checkpoint_cannot_resume_with_different_payload(self):
        _, config, first = self.preview()
        graph = self.graph("add_story_comment", {"story_id": 10, "comment": "Different."})
        self.assertEqual(self.approve(graph, config, first)["outcome"], "blocked")
        self.assertEqual(self.client.effects, [])

    def test_gateway_requires_active_verified_scope(self):
        gateway = AtomicLifecycleGateway(self.client, self.journal, self.issue, Mock())
        with self.assertRaises(ValueError):
            gateway.execute("atomic_send_notification", "send_notification", PAYLOADS["send_notification"], "no-scope")
        self.assertEqual(self.issued, [])

    def test_gateway_rejects_action_capability_mismatch(self):
        gateway = AtomicLifecycleGateway(self.client, self.journal, self.issue, Mock())
        state = approved_state("atomic_send_notification", PAYLOADS["send_notification"], {})
        with lifecycle_scope(state, self.approvals), self.assertRaises(ValueError):
            gateway.execute("atomic_send_notification", "update_story_status", {}, "mismatch")

    def test_gateway_rejects_payload_different_from_approved_preview(self):
        gateway = AtomicLifecycleGateway(self.client, self.journal, self.issue, Mock())
        state = approved_state("atomic_send_notification", PAYLOADS["send_notification"], {})
        with lifecycle_scope(state, self.approvals), self.assertRaises(ValueError):
            gateway.execute("atomic_send_notification", "send_notification", {"title": "Other", "message": "Ready."}, "changed")
        self.assertEqual(self.issued, [])

    def test_journal_replay_returns_result_without_new_token_or_effect(self):
        payload = {**PAYLOADS["add_story_comment"], "expected_revision": 4}
        action = "atomic_add_story_comment"
        gateway = AtomicLifecycleGateway(self.client, self.journal, self.issue, Mock())
        state = approved_state(action, payload, {})
        with lifecycle_scope(state, self.approvals):
            first = gateway.execute(action, "add_story_comment", payload, "replay")
            second = gateway.execute(action, "add_story_comment", payload, "replay")
        self.assertEqual(first, second)
        self.assertEqual(len(self.issued), 1)
        self.assertEqual(len(self.client.effects), 1)
        self.assertEqual(self.client.details["revision"], 5)

    def test_unknown_outcome_is_durable_and_never_retried(self):
        payload = PAYLOADS["schedule_reminder"]
        action = "atomic_schedule_reminder"
        gateway = AtomicLifecycleGateway(self.client, self.journal, self.issue, Mock())
        state = approved_state(action, payload, {})
        self.client.failure = True
        with lifecycle_scope(state, self.approvals):
            with self.assertRaises(TimeoutError):
                gateway.execute(action, "schedule_reminder", payload, "unknown")
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                gateway.execute(action, "schedule_reminder", payload, "unknown")
        self.assertIsNotNone(self.journal.get("unknown:intent"))
        self.assertIsNone(self.journal.get("unknown:result"))
        self.assertEqual(len(self.client.effects), 1)

    def test_invalid_native_result_remains_unknown(self):
        self.client.invalid_result = True
        graph, config, first = self.preview("send_notification")
        self.assertEqual(self.approve(graph, config, first)["outcome"], "blocked")
        self.assertIsNone(self.journal.get(first["run_id"] + ":atomic_send_notification:result"))
        self.assertIsNotNone(self.journal.get(first["run_id"] + ":atomic_send_notification:intent"))

    def test_missing_issuer_blocks_without_effect(self):
        self.gateway.execution_issuer = None
        graph, config, first = self.preview()
        self.assertEqual(self.approve(graph, config, first)["outcome"], "blocked")
        self.assertEqual(self.client.effects, [])

    def test_tracking_failure_blocks_before_preview(self):
        self.tracking.record.side_effect = OSError("Tracking unavailable")
        _, _, first = self.preview()
        self.assertEqual(first["outcome"], "blocked")
        self.assertEqual(self.client.effects, [])

    def test_post_mutation_tracking_failure_preserves_result_without_retry(self):
        graph, config, first = self.preview("send_notification")

        def record(event, *, key):
            if event["action"] == "atomic_send_notification" and event["phase"] == "executed":
                raise OSError("Tracking unavailable")
            self.journal.put(key + ":event", event)

        self.tracking.record.side_effect = record
        result = self.approve(graph, config, first)
        self.assertEqual(result["outcome"], "reconciliation_required")
        self.assertIn("atomic_send_notification", result["results"])
        self.assertIsNotNone(self.journal.get(first["run_id"] + ":atomic_send_notification:result"))
        self.assertEqual(len(self.client.effects), 1)

    def test_empty_execution_token_never_reaches_native_mutation(self):
        self.gateway.execution_issuer = Mock(return_value="")
        graph, config, first = self.preview("send_notification")
        self.assertEqual(self.approve(graph, config, first)["outcome"], "blocked")
        self.assertEqual(self.client.effects, [])

    def test_build_copies_arguments_before_preview(self):
        payload = deepcopy(PAYLOADS["send_notification"])
        graph = self.graph("send_notification", payload)
        payload["title"] = "Changed after build"
        result = graph.invoke({"request": {}}, {"configurable": {"thread_id": self.id()}})
        self.assertEqual(result["pending_approval"]["preview"], PAYLOADS["send_notification"])

    def test_cache_rejects_child_fields_native_handler_would_ignore(self):
        payload = {**PAYLOADS["save_technical_debt_preview_cache"],
                   "child_tasks": [{"title": "Task", "original_estimate_hours": 2}]}
        with self.assertRaises(ValueError):
            validate_arguments("save_technical_debt_preview_cache", payload)

    def test_operator_repository_is_required_for_cache(self):
        self.ports.branches.path.side_effect = ValueError("Unauthorized repository")
        _, _, first = self.preview("save_technical_debt_preview_cache")
        self.assertEqual(first["outcome"], "blocked")
        self.assertEqual(self.client.effects, [])

    def test_schema_rejects_coercion_nonfinite_and_nested_bypass(self):
        invalid = [
            ("get_story_details", {"story_id": True}),
            ("get_story_details", {"story_id": "10"}),
            ("get_upcoming_deadlines", {"days": -1}),
            ("record_ai_metrics", {**PAYLOADS["record_ai_metrics"], "remaining_story_points": float("inf")}),
            ("record_ai_metrics", {**PAYLOADS["record_ai_metrics"], "actions_taken": "review"}),
            ("save_technical_debt_preview_cache", {**PAYLOADS["save_technical_debt_preview_cache"], "story_points": 4}),
            ("save_technical_debt_preview_cache", {**PAYLOADS["save_technical_debt_preview_cache"], "child_tasks": [{"title": "Task", "status": "Closed"}]}),
            ("update_task_control", {"summary": "[]"}),
            ("update_task_control", {"summary": '{"points": NaN}'}),
            ("send_notification", {**PAYLOADS["send_notification"], "urgency": "critical"}),
            ("preview_story_branch_association", {**READ_PAYLOADS["preview_story_branch_association"], "include_child_work_items": 1}),
            ("preview_story_branch_association", {**READ_PAYLOADS["preview_story_branch_association"], "related_work_item_ids": [10, 10]}),
        ]
        for name, payload in invalid:
            with self.subTest(name=name, payload=payload), self.assertRaises(ValueError):
                validate_arguments(name, payload)

    def test_calendar_schema_requires_aware_ordered_timestamps(self):
        for payload in (
                {**PAYLOADS["create_calendar_event"], "start": "2026-10-02T09:00:00"},
                {**PAYLOADS["create_calendar_event"], "end": "2026-10-02T08:00:00+00:00"},
                {**PAYLOADS["create_calendar_event"], "reminder_minutes": True}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_arguments("create_calendar_event", payload)

    def test_unknown_names_and_missing_checkpointer_fail_closed(self):
        with self.assertRaises(ValueError):
            validate_arguments("unregistered", {})
        with self.assertRaises(ValueError):
            build_atomic_runtime("send_notification", self.ports, checkpointer=None, arguments=PAYLOADS["send_notification"])