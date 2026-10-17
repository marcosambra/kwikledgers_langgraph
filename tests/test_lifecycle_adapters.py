from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hmac
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from kwikledgers_langgraph.lifecycle_adapters import (
    ExplicitLocalApproval, GitBranches, LifecycleGateway, LifecycleJournal, MCPContext, MCPPullRequests,
    MCPWorkItems, MCPDebtPreparation, OperatorTaskPlan, OperatorTasks, SignedReceiptApproval, active_state, approval_binding, canonical, lifecycle_scope,
)
from kwikledgers_langgraph.graphs.lifecycle_engine import preview_digest


NOW = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
APPROVAL_KEY = b"a" * 32


def signed_receipt_for(pending, *, issued=NOW, expires=None, key=APPROVAL_KEY):
    claims = {field: pending[field] for field in ("action", "run_id", "request_id", "preview_digest")}
    claims.update({"decision": "approve", "subject": "external-human",
                   "issued_at": issued.isoformat(), "expires_at": (expires or issued + timedelta(minutes=5)).isoformat()})
    return {"claims": claims, "signature": hmac.new(key, canonical(claims).encode(), "sha256").hexdigest()}


def receipt_for(pending, *, issued=NOW, expires=None, key=APPROVAL_KEY):
    return {**approval_binding(pending), "decision": "approve", "source": "human",
            "subject": "local-operator", "confirmed_at": issued.isoformat()}


def approved_state(action, preview, context):
    pending = {"action": action, "run_id": "native-run", "request_id": "native-request",
               "preview": deepcopy(preview), "preview_digest": preview_digest(preview),
               "previewed_at": NOW.isoformat(), "approved": True}
    pending["confirmation"] = receipt_for(pending)
    return {"run_id": "native-run", "observability": {"active_run_id": "native-run"},
            "context": context, "pending_approval": pending}


class ExplicitLocalApprovalTests(TestCase):
    def setUp(self):
        self.verifier = ExplicitLocalApproval(clock=lambda: NOW)
        self.state = approved_state("activate", {"task_id": 11}, {})
        self.pending = approval_binding(self.state["pending_approval"])

    def test_confirmation_without_key_exposes_verified_context(self):
        confirmation = self.verifier.confirm(self.pending, "native-request")
        state = deepcopy(self.state)
        state["pending_approval"]["confirmation"] = confirmation
        with lifecycle_scope(state, self.verifier):
            verified = active_state("activate")
            self.assertEqual(verified["verified_lifecycle_binding"], self.pending)
            self.assertEqual(verified["verified_lifecycle_approval"]["method"], "local_explicit")
            self.assertNotIn("claims", verified["pending_approval"]["confirmation"])

    def test_foreign_binding_and_changed_exact_preview_are_rejected(self):
        confirmation = self.verifier.confirm(self.pending, "native-request")
        for field in ("action", "run_id", "request_id", "preview_digest", "previewed_at"):
            self.assertFalse(self.verifier.verify(self.pending, {**confirmation, field: "foreign"}))
        self.assertFalse(self.verifier.verify(self.pending, {**confirmation, "preview": {"task_id": 12}}))
        self.assertFalse(self.verifier.verify(self.pending, {**confirmation, "source": "agent"}))
        self.assertFalse(self.verifier.verify(self.pending, {**confirmation, "decision": "deny"}))
        self.assertFalse(self.verifier.verify(self.pending, {**confirmation, "confirmed_at": (NOW - timedelta(seconds=1)).isoformat()}))
        self.assertFalse(self.verifier.verify(self.pending, {**confirmation, "confirmed_at": (NOW + timedelta(seconds=1)).isoformat()}))

    def test_confirmation_requires_current_unapproved_request(self):
        for pending, request_id in ((self.pending, "foreign"), ({**self.pending, "approved": True}, "native-request")):
            with self.assertRaises(ValueError):
                self.verifier.confirm(pending, request_id)

    def test_signed_receipts_are_rejected_even_with_configured_key(self):
        receipt = signed_receipt_for(self.pending)
        self.assertFalse(self.verifier.verify(self.pending, receipt))
        self.assertFalse(ExplicitLocalApproval(APPROVAL_KEY, clock=lambda: NOW).verify(self.pending, receipt))
        self.assertFalse(SignedReceiptApproval(APPROVAL_KEY).verify(self.pending, receipt))


class LocalOperatorTaskTests(TestCase):
    def test_authorized_local_plan_runs_without_azure_approval_and_checks_tested_tree(self):
        with TemporaryDirectory() as directory:
            journal = LifecycleJournal(Path(directory) / "local.db")
            self.addCleanup(journal.close)
            branches = Mock()
            branches.path.return_value = Path(directory)
            plan = OperatorTaskPlan("plan", "payment", 10, 11, ("owned.py",),
                                    ("implement",), (("test",),), "Local change")
            tasks = OperatorTasks((plan,), branches, journal)
            context = {"repository": "payment", "story_id": 10, "task_id": 11}
            state = {"run_id": "run", "observability": {"active_run_id": "run"},
                     "context": context, "results": {"plan": tasks.plan(context)}}
            with lifecycle_scope(state), patch.object(tasks, "_run", return_value={"returncode": 0}):
                self.assertTrue(tasks.implement("plan", key="run:implement")["implemented"])
            with lifecycle_scope(state), patch.object(tasks, "_run", return_value={"returncode": 0}), \
                    patch.object(tasks, "_tree", return_value="tested"):
                state["results"]["test"] = tasks.test("plan", key="run:test")
            with lifecycle_scope(state), patch.object(tasks, "_tree", return_value="changed"), \
                    patch("kwikledgers_langgraph.lifecycle_adapters.git") as command:
                with self.assertRaisesRegex(ValueError, "unchanged"):
                    tasks.commit("plan", 11, key="run:commit")
                command.assert_not_called()
            with lifecycle_scope(state), patch.object(tasks, "_tree", return_value="tested"), \
                    patch("kwikledgers_langgraph.lifecycle_adapters.git", side_effect=(
                        "owned.py", "", "owned.py", "", "commit")):
                self.assertEqual(tasks.commit("plan", 11, key="run:commit")["commit"], "commit")


class LocalJournalTests(TestCase):
    def test_journal_rejects_symlinks_and_shared_writable_directories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.db"
            link = root / "link.db"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                LifecycleJournal(link)
            self.assertFalse(target.exists())
            shared = root / "shared"
            shared.mkdir()
            shared.chmod(0o777)
            with self.assertRaises(ValueError):
                LifecycleJournal(shared / "journal.db")
            self.assertFalse((shared / "journal.db").exists())

    def test_no_key_journal_reopens_and_replays_only_the_same_payload(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            journal = LifecycleJournal(path)
            operation = Mock(return_value={"id": 42})
            self.assertEqual(journal.once("create", {"title": "Exact"}, operation), {"id": 42})
            journal.close()
            journal = LifecycleJournal(path)
            try:
                self.assertEqual(journal.once("create", {"title": "Exact"}, operation), {"id": 42})
                with self.assertRaises(ValueError):
                    journal.once("create", {"title": "Changed"}, operation)
                operation.assert_called_once()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                journal.close()

    def test_unknown_outcome_survives_restart_without_key(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            journal = LifecycleJournal(path)
            operation = Mock(side_effect=OSError("Unknown"))
            with self.assertRaises(OSError):
                journal.once("create", {"title": "Exact"}, operation)
            journal.close()
            journal = LifecycleJournal(path)
            try:
                with self.assertRaisesRegex(RuntimeError, "unknown"):
                    journal.once("create", {"title": "Exact"}, operation)
                operation.assert_called_once()
            finally:
                journal.close()

    def test_checksum_corruption_and_signed_downgrade_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            journal = LifecycleJournal(path)
            journal.put("event", {"id": 42})
            with journal.db:
                journal.db.execute("UPDATE lifecycle_evidence SET payload = ?", ('{"id":43}',))
            with self.assertRaises(ValueError):
                journal.get("event")
            journal.close()
            path = Path(directory) / "signed.db"
            journal = LifecycleJournal(path, b"s" * 32)
            journal.put("event", {"id": 42})
            with journal.db:
                journal.db.execute("UPDATE lifecycle_evidence SET signature = ?", ("old-hmac",))
            journal.close()
            journal = LifecycleJournal(path)
            try:
                with self.assertRaises(ValueError):
                    journal.get("event")
            finally:
                journal.close()


class NativeCapabilityClient:
    def __init__(self):
        self.calls = []
        self.user = {"message": "Usuario ativo: Native User <native@example.com>"}
        self.items = {
            10: {"id": 10, "revision": 1, "state": "Desenvolvimento", "title": "Story",
                 "child_work_items": [{"id": 11}, {"id": 12}]},
            11: {"id": 11, "revision": 2, "state": "Active", "title": "Task", "child_work_items": []},
            12: {"id": 12, "revision": 3, "state": "Active", "title": "Other", "child_work_items": []},
        }
        self.project = "KL"
        self.relationship = "child"
        self.linked = False
        self.body_changed = False
        self.created = True
        self.existing_pr = None
        self.tracking_failure = False
        self.tracking_hook = None
        self.effect_calls = []

    def revision_guard(self, arguments):
        revisions = arguments.get("expected_revisions")
        if revisions is not None:
            if revisions != {str(identifier): self.items[identifier]["revision"] for identifier in (10, 11, 12)}:
                raise RuntimeError("Native revision conflict")
        identifier = arguments.get("work_item_id", arguments.get("story_id"))
        if "expected_revision" in arguments and arguments["expected_revision"] != self.items[identifier]["revision"]:
            raise RuntimeError("Native revision conflict")

    def effect(self, name, arguments):
        if not arguments.get("execution_capability"):
            raise AssertionError("Missing execution capability")
        self.effect_calls.append((name, deepcopy(arguments)))

    def call(self, capability_name, arguments):
        self.calls.append((capability_name, deepcopy(arguments)))
        if capability_name == "get_active_user":
            return deepcopy(self.user)
        if capability_name == "get_story_details":
            return deepcopy(self.items[arguments["story_id"]])
        if capability_name == "preview_story_branch_association":
            items = [{"id": identifier, "revision": self.items[identifier]["revision"],
                      "relationship_to_story": "parent" if identifier == 10 else self.relationship,
                      "branch_linked": self.linked} for identifier in (10, 11, 12)]
            return {"story_id": 10, "repository_name": "payment", "project": self.project,
                    "source_branch": arguments["source_branch"], "work_items": items,
                    "expected_revisions": {str(item["id"]): item["revision"] for item in items}}
        if capability_name == "preview_story_pull_request":
            association = self.call("preview_story_branch_association", {
                "story_id": arguments["story_id"], "source_branch": arguments["source_branch"]})
            return {"story_id": 10, "repository_name": "payment", "project": self.project,
                    "source_branch": arguments["source_branch"], "target_branch": "stage-pre-prod",
                    "is_draft": True, "title": arguments["title"],
                    "body_markdown": arguments["what_was_changed"] + "\n```mermaid\n" + arguments["mermaid_diagram"] + "\n```",
                    "branch_association_preview": association, "expected_revisions": association["expected_revisions"],
                    "can_open_pull_request": self.existing_pr is None, "existing_active_pull_request": self.existing_pr}
        if capability_name == "create_story_pull_request":
            self.revision_guard(arguments)
            self.effect(capability_name, arguments)
            result = self.call("preview_story_pull_request", arguments)
            result.update({"created": self.created, "pull_request": {
                "title": arguments["title"], "source_ref_name": f'refs/heads/{arguments["source_branch"]}',
                "target_ref_name": "refs/heads/stage-pre-prod", "is_draft": True,
                "web_url": "https://dev.azure.com/KL/_git/payment/pullrequest/1"}})
            if self.body_changed:
                result["body_markdown"] = "Changed"
            for identifier in (10, 11, 12):
                self.items[identifier]["revision"] += 1
            return result
        if capability_name == "associate_story_branch":
            self.revision_guard(arguments)
            self.effect(capability_name, arguments)
            if not self.linked:
                for identifier in (10, 11, 12):
                    self.items[identifier]["revision"] += 1
            self.linked = True
            return {"executed": True, "association_result": {"associated_work_item_ids": [10, 11, 12]},
                    "expected_revisions": {str(identifier): self.items[identifier]["revision"] for identifier in (10, 11, 12)}}
        if capability_name in ("update_work_item_content", "update_work_item_effort", "update_story_status"):
            self.revision_guard(arguments)
            self.effect(capability_name, arguments)
            identifier = arguments.get("work_item_id", arguments.get("story_id"))
            item = self.items[identifier]
            item["revision"] += 1
            if "status" in arguments:
                item["state"] = arguments["status"]
            if "completed_work_hours" in arguments:
                item["completed_work_hours"] = arguments["completed_work_hours"]
            return deepcopy(item)
        if capability_name == "append_daily_action_log":
            if self.tracking_hook:
                self.tracking_hook(arguments)
            if self.tracking_failure:
                raise OSError("Tracking unavailable")
            self.effect(capability_name, arguments)
            return {"written": True}
        if capability_name == "create_work_item":
            self.effect(capability_name, arguments)
            return {"id": 100 + sum(name == "create_work_item" for name, _ in self.effect_calls), "revision": 1}
        raise AssertionError(f"Unexpected capability: {capability_name}")


class NativeContextTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = NativeCapabilityClient()
        self.journal = LifecycleJournal(Path(self.directory.name) / "journal.db", b"j" * 32)
        self.addCleanup(self.journal.close)
        gateway = LifecycleGateway(self.client, self.journal, None, None)
        branches = GitBranches({"payment": Path(self.directory.name)}, gateway)
        self.context = MCPContext(gateway, branches, project="KL", area="Squad")
        self.request = {"story_id": 10, "task_id": 11, "repository_name": "payment"}

    def test_native_message_and_hierarchy_resolve_without_invented_fields(self):
        result = self.context.resolve(self.request)
        self.assertEqual(result["user_id"], "native@example.com")
        self.assertEqual(result["child_ids"], [11, 12])
        self.assertEqual(result["project"], "KL")
        self.assertTrue(all(name.startswith(("get_", "preview_")) for name, _ in self.client.calls))

    def test_email_only_native_identity(self):
        self.client.user = {"message": "Usuario ativo: native@example.com"}
        self.assertEqual(self.context.resolve(self.request)["user_id"], "native@example.com")

    def test_context_retains_task_story_and_ancestor_details(self):
        hierarchy = {"ancestors": [{"id": 10, "work_item_type": "Bug"},
                     {"id": 20, "work_item_type": "Feature", "description": "Feature scope"}],
                     "missing_levels": ["Epic"], "issues": [], "complete": False}
        self.client.items[11].update({"hierarchy_context": hierarchy, "description": "Task scope"})
        self.client.items[10].update({"description": "Story scope", "acceptance_criteria": "Acceptance"})
        result = self.context.resolve(self.request)
        self.assertEqual(result["hierarchy_context"], hierarchy)
        self.assertEqual(result["task_details"]["description"], "Task scope")
        self.assertEqual(result["story_details"]["acceptance_criteria"], "Acceptance")

    def test_missing_hierarchy_is_explicitly_incomplete(self):
        result = self.context.resolve(self.request)
        self.assertFalse(result["hierarchy_context"]["complete"])
        self.assertEqual(result["hierarchy_context"]["issues"], [{"code": "hierarchy_context_unavailable"}])

    def test_operator_plan_and_command_receive_hierarchy_without_shell_interpolation(self):
        context = self.context.resolve(self.request)
        plan = OperatorTaskPlan(plan_id="task-plan", repository="payment", story_id=10, task_id=11,
                                files=("app.py",), implementation_command=("trusted-command",),
                                test_commands=(("trusted-tests",),), logic="Implement task")
        executor = OperatorTasks((plan,), self.context.branches, self.journal)
        preview = executor.plan(context)
        self.assertEqual(preview["hierarchy_context"], context["hierarchy_context"])
        with lifecycle_scope({"run_id": "run", "observability": {"active_run_id": "run"}, "context": context}):
            with patch("kwikledgers_langgraph.lifecycle_adapters.subprocess.run",
                       return_value=Mock(returncode=0, stdout=b"", stderr=b"")) as run:
                executor._run(plan, plan.implementation_command)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["env"]["KWIKLEDGERS_TASK_CONTEXT"], canonical({
            field: context.get(field) for field in
            ("story_id", "task_id", "story_details", "task_details", "hierarchy_context")}))

    def test_unconfigured_identity_fails_closed(self):
        self.client.user = {"message": "Email nao configurado. Configure AZURE_USER_EMAIL"}
        with self.assertRaises(ValueError):
            self.context.resolve(self.request)

    def test_foreign_project_fails_closed(self):
        self.client.project = "Other"
        with self.assertRaises(ValueError):
            self.context.resolve(self.request)

    def test_related_item_is_not_accepted_as_child(self):
        self.client.relationship = "related"
        with self.assertRaises(ValueError):
            self.context.resolve(self.request)

    def test_explicit_parent_conflict_fails_closed(self):
        self.client.items[11]["parent_id"] = 99
        with self.assertRaises(ValueError):
            self.context.resolve(self.request)

    def test_unauthorized_repository_fails_before_native_reads(self):
        with self.assertRaises(ValueError):
            self.context.resolve({**self.request, "repository_name": "other"})
        self.assertEqual(self.client.calls, [])

    def test_missing_revision_fails_closed(self):
        del self.client.items[11]["revision"]
        with self.assertRaises(ValueError):
            self.context.resolve(self.request)


class NativePullRequestTests(TestCase):
    def setUp(self):
        NativeContextTests.setUp(self)
        self.scope = self.context.resolve(self.request)
        self.client.linked = True
        self.issuer = Mock(return_value="signed-capability")
        gateway = LifecycleGateway(self.client, self.journal, self.issuer, None)
        self.branches = GitBranches({"payment": Path(self.directory.name)}, gateway)
        self.prs = MCPPullRequests(gateway, self.branches)
        self.approvals = ExplicitLocalApproval(APPROVAL_KEY, clock=lambda: NOW)
        self.branch = "feature/KL-10-story"
        self.template = patch.object(Path, "read_text", return_value="Repository template")
        self.template.start()
        self.addCleanup(self.template.stop)
        self.exists = patch.object(Path, "is_file", return_value=True)
        self.exists.start()
        self.addCleanup(self.exists.stop)

    def preview(self):
        return {**self.prs.preview(self.scope, self.branch), "expected_revision": 1}

    def test_native_web_url_and_body_are_preserved(self):
        preview = self.preview()
        with lifecycle_scope(approved_state("draft_pr", preview, self.scope), self.approvals):
            result = self.prs.create_draft(preview, key="pr")
        self.assertEqual(result["url"], "https://dev.azure.com/KL/_git/payment/pullrequest/1")
        self.assertEqual(result["body_markdown"], preview["body"])
        state = self.issuer.call_args.args[0]
        self.assertEqual(state["verified_lifecycle_binding"]["action"], "draft_pr")
        self.assertEqual(self.issuer.call_args.kwargs["arguments"]["expected_revisions"], {"10": 1, "11": 2, "12": 3})

    def test_changed_created_body_requires_reconciliation(self):
        preview = self.preview()
        self.client.body_changed = True
        with lifecycle_scope(approved_state("draft_pr", preview, self.scope), self.approvals):
            with self.assertRaisesRegex(ValueError, "reconcile"):
                self.prs.create_draft(preview, key="pr")

    def test_existing_pr_without_actual_body_is_not_reused(self):
        self.client.existing_pr = {"web_url": "https://example.com/pr", "is_draft": True}
        with self.assertRaises(ValueError):
            self.preview()
        self.issuer.assert_not_called()

    def test_racing_existing_pr_requires_reconciliation(self):
        preview = self.preview()
        self.client.created = False
        with lifecycle_scope(approved_state("draft_pr", preview, self.scope), self.approvals):
            with self.assertRaisesRegex(ValueError, "reconcile"):
                self.prs.create_draft(preview, key="pr")

    def test_missing_branch_link_blocks_preview(self):
        self.client.linked = False
        with self.assertRaises(ValueError):
            self.preview()

    def test_conflicting_parent_revision_blocks_before_issuer(self):
        preview = {**self.preview(), "expected_revision": 99}
        with lifecycle_scope(approved_state("draft_pr", preview, self.scope), self.approvals):
            with self.assertRaises(ValueError):
                self.prs.create_draft(preview, key="pr")
        self.issuer.assert_not_called()

    def test_branch_links_require_authenticated_provenance(self):
        with patch.object(self.branches, "_owned", return_value=[self.branch]):
            with self.assertRaises(ValueError):
                self.branches.links(self.scope)
            self.journal.put("branch:payment:10", {"name": self.branch})
            links = self.branches.links(self.scope)
        self.assertEqual(links["associated_ids"], [10, 11, 12])
        self.assertEqual(links["expected_revisions"], {"10": 1, "11": 2, "12": 3})


class LifecycleEvidenceTests(TestCase):
    def setUp(self):
        NativeContextTests.setUp(self)
        self.now = NOW
        self.approvals = ExplicitLocalApproval(APPROVAL_KEY, clock=lambda: self.now)
        self.issuer = Mock(return_value="signed-capability")
        self.gateway = LifecycleGateway(self.client, self.journal, self.issuer, self.issuer)
        self.preview = {"task_id": 11, "assigned_to": "native@example.com", "area_path": "KL\\Squad",
                        "status": "Active", "expected_revision": 2}
        self.state = approved_state("activate", self.preview, {"repository": "payment"})

    def test_signed_receipts_are_always_rejected(self):
        pending = approval_binding(self.state["pending_approval"])
        self.assertFalse(self.approvals.verify(pending, signed_receipt_for(pending)))
        for field in ("action", "run_id", "request_id", "preview_digest"):
            foreign = {**pending, field: "foreign"}
            self.assertFalse(self.approvals.verify(pending, signed_receipt_for(foreign)))
        self.assertFalse(self.approvals.verify(pending, signed_receipt_for(pending, key=b"b" * 32)))
        self.assertFalse(self.approvals.verify(pending, signed_receipt_for(pending, expires=NOW + timedelta(seconds=601))))
        self.assertFalse(self.approvals.verify(pending, signed_receipt_for(pending, issued=NOW - timedelta(seconds=1))))
        self.assertFalse(self.approvals.verify(pending, signed_receipt_for(pending, issued=NOW + timedelta(seconds=1))))

    def test_expiration_is_checked_again_inside_active_scope(self):
        with lifecycle_scope(self.state, self.approvals):
            self.now -= timedelta(seconds=1)
            with self.assertRaises(ValueError):
                MCPWorkItems(self.gateway).activate(self.preview, key="activate")
        self.issuer.assert_not_called()

    def test_expiration_is_checked_after_journal_reservation(self):
        def reserve(key, arguments, operation):
            self.now -= timedelta(seconds=1)
            return operation()
        with lifecycle_scope(self.state, self.approvals), patch.object(self.journal, "once", side_effect=reserve):
            with self.assertRaises(ValueError):
                MCPWorkItems(self.gateway).activate(self.preview, key="activate")
        self.issuer.assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_expiration_during_issuance_blocks_the_native_call(self):
        def issue(*args, **kwargs):
            self.now -= timedelta(seconds=1)
            return "signed-capability"
        self.issuer.side_effect = issue
        with lifecycle_scope(self.state, self.approvals):
            with self.assertRaises(ValueError):
                MCPWorkItems(self.gateway).activate(self.preview, key="activate")
        self.issuer.assert_called_once()
        self.assertEqual(self.client.calls, [])

    def test_payload_tampering_inside_scope_never_reaches_issuer(self):
        with lifecycle_scope(self.state, self.approvals):
            with self.assertRaises(ValueError):
                MCPWorkItems(self.gateway).activate({**self.preview, "task_id": 12}, key="activate")
        self.issuer.assert_not_called()

    def test_forged_verified_binding_is_removed_from_tracking_scope(self):
        state = {**self.state, "verified_lifecycle_binding": {"action": "activate"}}
        with lifecycle_scope(state):
            self.assertNotIn("verified_lifecycle_binding", active_state())
            with self.assertRaises(ValueError):
                self.gateway.execute("activate", "update_work_item_content", {}, "mutation")
        self.issuer.assert_not_called()

    def test_wrong_capability_for_approved_action_is_denied(self):
        with lifecycle_scope(self.state, self.approvals):
            with self.assertRaises(ValueError):
                self.gateway.execute("activate", "create_work_item", {}, "mutation")
        self.issuer.assert_not_called()

    def test_internal_capability_cannot_mutate_azure(self):
        with lifecycle_scope(self.state):
            with self.assertRaises(ValueError):
                self.gateway.internal("update_work_item_content", {}, "mutation")
        self.issuer.assert_not_called()

    def test_internal_tracking_issuer_never_receives_mutation_binding(self):
        with lifecycle_scope(self.state, self.approvals):
            self.gateway.internal("append_daily_action_log", {
                "title": "lifecycle", "details": "{}", "source": "langgraph_lifecycle",
            }, "tracking")
        self.assertNotIn("verified_lifecycle_binding", self.issuer.call_args.args[0])
        self.assertNotIn("pending_approval", self.issuer.call_args.args[0])
        self.assertNotIn("approval_confirmation", self.issuer.call_args.args[0])
        self.assertEqual(self.issuer.call_args.kwargs["action"], "lifecycle_tracking")

    def test_operations_outside_scope_are_denied(self):
        with self.assertRaises(ValueError):
            self.gateway.execute("activate", "update_work_item_content", {}, "mutation")
        self.issuer.assert_not_called()

    def test_journal_success_is_replayed_once_and_payload_change_is_denied(self):
        operation = Mock(return_value={"ok": True})
        self.assertEqual(self.journal.once("once", {"id": 11}, operation), {"ok": True})
        self.assertEqual(self.journal.once("once", {"id": 11}, operation), {"ok": True})
        operation.assert_called_once()
        with self.assertRaises(ValueError):
            self.journal.once("once", {"id": 12}, operation)

    def test_unknown_outcome_is_not_automatically_retried_after_restart(self):
        operation = Mock(side_effect=OSError("Transport lost"))
        with self.assertRaises(OSError):
            self.journal.once("unknown", {"id": 11}, operation)
        restarted = LifecycleJournal(self.journal.path, b"j" * 32)
        self.addCleanup(restarted.close)
        with self.assertRaisesRegex(RuntimeError, "reconcile"):
            restarted.once("unknown", {"id": 11}, operation)
        operation.assert_called_once()

    def test_journal_rejects_tampered_signed_evidence(self):
        self.journal.put("entry", {"ok": True})
        with self.journal.db:
            self.journal.db.execute("UPDATE lifecycle_evidence SET payload = ? WHERE key = ?", ('{"ok":false}', "entry"))
        with self.assertRaisesRegex(ValueError, "nova prévia"):
            self.journal.get("entry")