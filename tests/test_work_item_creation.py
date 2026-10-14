from copy import deepcopy
from html import escape
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from langgraph.types import Command

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.lifecycle_adapters import LifecycleGateway, LifecycleJournal, SignedReceiptApproval, active_state
from kwikledgers_langgraph.lifecycle_runtime import SignedLifecycleCheckpointer
from kwikledgers_langgraph.work_item_creation import (
    ACTION, FIELD_REFERENCES, NATIVE_FIELDS, CreationValidationError, TechnicalDebtLifecycleRequired,
    build_work_item_creation_graph, format_creation_rich_text, prepare_creation, validate_native_arguments,
)
from test_lifecycle_adapters import APPROVAL_KEY, NOW, receipt_for


def formatter(value):
    if value and value.startswith("<p>") and value.endswith("</p>"):
        return value
    return "<p>" + escape(value) + "</p>" if value else None


def operator_for(path):
    return {"project": "KL", "area": "Squad", "repositories": {"payment": str(path)},
            "process_constraints": {kind: {"native_type": native, "aliases": aliases,
                "supported_fields": list(FIELD_REFERENCES.values()), "required_fields": ["System.Title"],
                "defaults": {"System.IterationPath": "KL\\Sprint 1"}, "allowed_values": {}}
                for kind, native, aliases in (("UserStory", "User Story", ["story"]),
                    ("Task", "Task", ["task"]), ("Bug", "Bug", ["bug"]))}}


class CreationHarness:
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "creation.db"
        self.operator = operator_for(self.directory.name)
        self.arguments = {"work_item_type": "UserStory", "title": "Approved scope", "repository_name": "payment",
                          "story_points": 3, "description": "Formal description."}
        self.journal = LifecycleJournal(self.path, b"j" * 32)
        self.addCleanup(self.journal.close)
        self.parent = {"id": 10, "revision": 4, "work_item_type": "User Story", "project": "KL"}
        self.effects = []
        self.client = Mock()
        self.client.call.side_effect = self.call
        self.issuer = Mock(side_effect=self.issue)
        self.approvals = SignedReceiptApproval(APPROVAL_KEY, clock=lambda: NOW)
        self.gateway = LifecycleGateway(self.client, self.journal, self.issuer, Mock())
        tracking = Mock()
        tracking.record.side_effect = lambda event, key: self.journal.put(key + ":event", event)
        self.ports = LifecyclePorts(context=Mock(), tracking=tracking, approvals=self.approvals,
            clock=SimpleNamespace(now=lambda: NOW.isoformat()), branches=SimpleNamespace(path=Mock()),
            work_items=SimpleNamespace(gateway=self.gateway))
        self.checkpoint = SignedLifecycleCheckpointer(self.journal)

    def call(self, name, arguments):
        if name == "get_story_details":
            return deepcopy(self.parent)
        if name != "create_work_item" or not arguments.get("execution_capability"):
            raise AssertionError("Unexpected or unauthorized mutation")
        self.effects.append(deepcopy(arguments))
        return {"id": 42, "revision": 1, "skipped_fields": []}

    def issue(self, state, *, action, capability_name, arguments):
        self.assertEqual(action, ACTION)
        self.assertEqual(capability_name, "create_work_item")
        self.assertEqual(state, active_state(ACTION))
        self.assertEqual(arguments, state["verified_lifecycle_binding"]["preview"])
        return "test:creation"

    def graph(self, arguments=None, checkpoint=None):
        return build_work_item_creation_graph(self.ports, checkpointer=checkpoint or self.checkpoint,
            arguments=arguments or self.arguments, operator=self.operator, formatter=formatter)

    def preview(self, arguments=None):
        graph = self.graph(arguments)
        config = {"configurable": {"thread_id": self.id()}}
        return graph, config, graph.invoke({"request": {}}, config)

    def approve(self, graph, config, first, receipt=None):
        return graph.invoke(Command(resume=receipt if receipt is not None else receipt_for(first["pending_approval"])), config)


class WorkItemCreationTests(CreationHarness, TestCase):
    def test_exact_native_schema(self):
        self.assertEqual(len(NATIVE_FIELDS), 15)
        for field in ("expected_revision", "expected_revisions", "execution_capability", "approved", "fields", "Custom.Priority"):
            with self.subTest(field=field), self.assertRaises(CreationValidationError):
                validate_native_arguments({**self.arguments, field: 1})

    def test_preview_is_raw_complete_native_payload_and_receipt_is_required(self):
        payload = {**self.arguments, "assigned_to": "operator@example.com", "tags": "Reviewed",
            "acceptance_criteria": "Acceptance.", "use_next_sprint": False, "parent_work_item_id": None,
            "remaining_work_hours": 0, "completed_work_hours": 0, "original_estimate_hours": 2}
        graph, config, first = self.preview(payload)
        pending = first["pending_approval"]
        expected, _ = prepare_creation(payload, self.operator, formatter)
        self.assertEqual(pending["preview"], expected)
        self.assertEqual(set(expected), NATIVE_FIELDS)
        self.assertTrue(first.get("__interrupt__"))
        self.assertFalse(self.effects)
        result = self.approve(graph, config, first)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(self.effects, [{**expected, "execution_capability": "test:creation"}])

    def test_each_formal_type_and_process_alias(self):
        for alias in ("UserStory", "User Story", "story", "Bug", "Task"):
            with self.subTest(alias=alias):
                payload = {**self.arguments, "work_item_type": alias}
                if alias == "Task":
                    payload["parent_work_item_id"] = 10
                preview, kind = prepare_creation(payload, self.operator, formatter)
                self.assertIn(kind.value, self.operator["process_constraints"])
                self.assertEqual(preview["area_path"], "KL\\Squad")

    def test_debt_routes_only_to_existing_debt_lifecycle(self):
        for alias in ("TechnicalDebt", "Technical Debt", "Tech Debt", "technical debts", "debt", "debito tecnico", "debitos tecnicos"):
            with self.subTest(alias=alias), self.assertRaises(TechnicalDebtLifecycleRequired):
                self.graph({**self.arguments, "work_item_type": alias})
        self.client.call.assert_not_called()

    def test_non_idempotent_native_formatter_fails_preview_instead_of_double_escaping(self):
        with self.assertRaisesRegex(CreationValidationError, "HTML-idempotent"):
            prepare_creation(self.arguments, self.operator, lambda value: "<p>" + escape(value) + "</p>")
        self.client.call.assert_not_called()

    def test_serialized_native_parent_uses_real_area_type_and_revision(self):
        self.parent.pop("project")
        self.parent.update({"area_path": "KL\\Squad", "iteration_path": "KL\\Sprint 1"})
        graph, config, first = self.preview({**self.arguments, "work_item_type": "Task", "parent_work_item_id": 10})
        self.assertEqual(first["context"]["creation_parent"]["project"], "KL")
        self.assertEqual(self.approve(graph, config, first)["outcome"], "completed")

    def test_invalid_fibonacci_and_native_types_fail_before_any_call(self):
        for value in (0, 4, True, -1, float("nan"), float("inf"), "3"):
            with self.subTest(value=value), self.assertRaises(CreationValidationError):
                self.graph({**self.arguments, "story_points": value})
        for payload in ({**self.arguments, "use_next_sprint": 1}, {**self.arguments, "remaining_work_hours": True},
                        {**self.arguments, "repository_name": "unknown"}, {**self.arguments, "area_path": "Other"}):
            with self.subTest(payload=payload), self.assertRaises(CreationValidationError):
                self.graph(payload)
        self.client.call.assert_not_called()

    def test_unknown_required_process_field_fails_accurately_before_preview(self):
        self.operator["process_constraints"]["UserStory"]["required_fields"].append("Custom.Compliance")
        with self.assertRaisesRegex(CreationValidationError, "cannot be expressed.*Custom.Compliance"):
            self.graph()
        self.client.call.assert_not_called()

    def test_required_supported_and_allowed_fields_are_enforced(self):
        constraint = self.operator["process_constraints"]["UserStory"]
        constraint["required_fields"].append("Microsoft.VSTS.Common.AcceptanceCriteria")
        with self.assertRaisesRegex(CreationValidationError, "Missing required"):
            self.graph()
        constraint["required_fields"].pop()
        constraint["supported_fields"].remove("Microsoft.VSTS.Scheduling.StoryPoints")
        with self.assertRaisesRegex(CreationValidationError, "does not support"):
            self.graph()
        constraint["supported_fields"].append("Microsoft.VSTS.Scheduling.StoryPoints")
        constraint["allowed_values"]["Microsoft.VSTS.Scheduling.StoryPoints"] = [5]
        with self.assertRaisesRegex(CreationValidationError, "violates process"):
            self.graph()

    def test_task_requires_parent_identity_project_type_and_real_revision(self):
        task = {**self.arguments, "work_item_type": "Task"}
        with self.assertRaises(CreationValidationError):
            self.graph(task)
        for field, value in (("id", True), ("revision", None), ("project", "Other"), ("work_item_type", "Technical Debt")):
            with self.subTest(field=field):
                original = deepcopy(self.parent)
                self.parent[field] = value
                graph = self.graph({**task, "parent_work_item_id": 10})
                result = graph.invoke({"request": {}}, {"configurable": {"thread_id": field}})
                self.assertEqual(result["outcome"], "blocked")
                self.assertFalse(result["pending_approval"])
                self.parent = original
        self.assertFalse(self.effects)

    def test_task_parent_revision_is_signed_context_not_synthetic_create_argument(self):
        graph, config, first = self.preview({**self.arguments, "work_item_type": "Task", "parent_work_item_id": 10})
        self.assertEqual(first["context"]["creation_parent"]["revision"], 4)
        self.assertNotIn("expected_revision", first["pending_approval"]["preview"])
        self.parent["revision"] += 1
        self.assertEqual(self.approve(graph, config, first)["outcome"], "blocked")
        self.issuer.assert_not_called()
        self.assertFalse(self.effects)

    def test_invalid_or_replayed_receipt_cannot_create(self):
        graph, config, first = self.preview()
        self.assertEqual(self.approve(graph, config, first, {"approved": True})["outcome"], "denied")
        other = self.graph()
        other_config = {"configurable": {"thread_id": "other"}}
        next_preview = other.invoke({"request": {}}, other_config)
        self.assertNotEqual(first["pending_approval"]["request_id"], next_preview["pending_approval"]["request_id"])
        self.assertEqual(self.approve(other, other_config, next_preview, receipt_for(first["pending_approval"]))["outcome"], "denied")
        self.assertFalse(self.effects)

    def test_unknown_execution_outcome_is_not_retried(self):
        graph, config, first = self.preview()
        self.client.call.side_effect = TimeoutError("Unknown outcome")
        result = self.approve(graph, config, first)
        self.assertEqual(result["outcome"], "reconciliation_required")
        key = first["run_id"] + ":" + ACTION
        with self.assertRaisesRegex(RuntimeError, "reconcile"):
            self.journal.once(key, first["pending_approval"]["preview"], Mock())

    def test_creation_uses_real_issuer_with_exact_approved_payload(self):
        from kwikledgers_langgraph.fastmcp_execution_capability import issue_lifecycle_execution_capability

        self.gateway.execution_issuer = lambda state, **arguments: issue_lifecycle_execution_capability(
            state, now=NOW, **arguments,
        )
        graph, config, first = self.preview()
        with patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "fixture-execution-key",
                       "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(self.path.parent / "capabilities.db")}):
            self.assertEqual(self.approve(graph, config, first)["outcome"], "completed")
        effect = dict(self.effects[0])
        self.assertIn(".", effect.pop("execution_capability"))
        self.assertEqual(effect, first["pending_approval"]["preview"])

    def test_neutral_formatter_is_idempotent_and_escapes_untrusted_html(self):
        for value in ("Plain text.", "Line 1\nLine 2", "<script>alert(1)</script>", '<p onclick="x">Text</p>'):
            with self.subTest(value=value):
                formatted = format_creation_rich_text(value)
                self.assertEqual(format_creation_rich_text(formatted), formatted)
                self.assertNotIn("<script>", formatted)
                self.assertNotIn('<p onclick=', formatted)