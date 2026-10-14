import base64
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import pickle
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from kwikledgers_langgraph.lifecycle_cli import lifecycle_confirmation, lifecycle_issuers, main, parser, run_cli
from kwikledgers_langgraph.lifecycle_adapters import (
    LifecycleJournal, SignedReceiptApproval, active_state, build_lifecycle_ports,
)
from kwikledgers_langgraph.lifecycle_runtime import SignedLifecycleCheckpointer, build_lifecycle_runtime
from test_lifecycle_adapters import APPROVAL_KEY, NOW, NativeCapabilityClient, approved_state, approval_binding, receipt_for


class LifecycleCLIContractTests(TestCase):
    def setUp(self):
        self.graph = Mock()
        self.graph.get_state.return_value = SimpleNamespace(values={}, next=())
        self.graph.invoke.return_value = {"outcome": "running", "run_id": "run"}

    def test_issuer_contract_has_no_generic_fallback(self):
        generic = Mock()
        with patch("kwikledgers_langgraph.lifecycle_cli.issuers", SimpleNamespace(
                issue_execution_capability=generic, issue_runtime_execution_capability=Mock())):
            with self.assertRaisesRegex(RuntimeError, "issue_lifecycle_execution_capability"):
                lifecycle_issuers()
        generic.assert_not_called()

    def test_specific_issuers_are_used(self):
        execution, runtime = Mock(), Mock()
        with patch("kwikledgers_langgraph.lifecycle_cli.issuers", SimpleNamespace(
                issue_lifecycle_execution_capability=execution, issue_runtime_execution_capability=runtime)):
            self.assertEqual(lifecycle_issuers(), (execution, runtime))

    def test_approval_cannot_be_supplied_with_initial_preview(self):
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id="thread", receipt={"claims": {}})
        self.graph.invoke.assert_not_called()

    def test_receipt_and_branch_selection_cannot_be_combined(self):
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id="thread", resume=True, receipt={}, branch_type="feature")
        self.graph.invoke.assert_not_called()

    def test_resume_cannot_change_context(self):
        self.graph.get_state.return_value = SimpleNamespace(
            values={"context": {"story_id": 10, "task_id": 11, "repository": "payment"},
                    "pending_approval": {"action": "activate"}}, next=("activate_gate",))
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id="thread", resume=True, receipt={}, request={"task_id": 12})
        self.graph.invoke.assert_not_called()

    def test_existing_thread_cannot_be_reset(self):
        self.graph.get_state.return_value = SimpleNamespace(values={"run_id": "run"}, next=())
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id="thread")
        self.graph.invoke.assert_not_called()

    def test_debt_parser_exposes_prepared_preview_key(self):
        args = parser().parse_args(["technical_debt_execute", "--operator-config", "operator.json",
                                   "--checkpoint", "checkpoint.db", "--thread-id", "debt",
                                   "--repository-name", "payment", "--preview-key", "prepared"])
        self.assertEqual(args.preview_key, "prepared")

    def test_parser_exposes_separate_local_approval_and_rejects_combined_stages(self):
        base = ["story_start", "--operator-config", "operator.json", "--checkpoint", "state.db",
                "--thread-id", "thread", "--resume", "--approval-request-id", "request"]
        self.assertEqual(parser().parse_args(base).approval_request_id, "request")
        self.assertEqual(parser().parse_args(base + ["--receipt", "receipt.json"]).receipt, Path("receipt.json"))
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(base + ["--receipt", "receipt.json", "--branch-type", "feature"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main([argument for argument in base if argument != "--resume"])


    def test_explicit_approval_requires_separate_resume_and_exact_request(self):
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id="thread", approval_request_id="request")
        self.graph.invoke.assert_not_called()
        state = approved_state("activate", {"task_id": 11}, {})
        state["pending_approval"] = approval_binding(state["pending_approval"])
        self.graph.get_state.return_value = SimpleNamespace(values=state, next=("activate_gate",))
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id="thread", resume=True, receipt=receipt_for(state["pending_approval"]),
                    approval_request_id="foreign")
        self.graph.invoke.assert_not_called()
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id="thread", resume=True, approval_request_id="native-request")
        self.graph.invoke.assert_not_called()
        run_cli(self.graph, thread_id="thread", resume=True, receipt=receipt_for(state["pending_approval"]),
                approval_request_id="native-request")
        confirmation = self.graph.invoke.call_args.args[0].resume
        self.assertTrue(SignedReceiptApproval(APPROVAL_KEY, clock=lambda: NOW).verify(state["pending_approval"], confirmation))

    def test_signed_approval_cannot_change_active_run_or_use_malformed_envelope(self):
        state = approved_state("activate", {"task_id": 11}, {})
        state["pending_approval"] = approval_binding(state["pending_approval"])
        with self.assertRaises(ValueError):
            lifecycle_confirmation(state, receipt={}, approval_request_id="native-request")
        state["observability"]["active_run_id"] = "foreign"
        with self.assertRaises(ValueError):
            lifecycle_confirmation(state, receipt=receipt_for(state["pending_approval"]),
                                   approval_request_id="native-request")


class LocalCheckpointTests(TestCase):
    def test_legacy_pickle_is_rejected_even_with_its_key(self):
        with TemporaryDirectory() as directory:
            journal = LifecycleJournal(Path(directory) / "checkpoint.db", b"s" * 32)
            try:
                SignedLifecycleCheckpointer(journal)
                payload = base64.b64encode(pickle.dumps(({"thread": {}}, {}, {}))).decode("ascii")
                with journal.db:
                    journal.db.execute("INSERT INTO lifecycle_checkpoint VALUES (1, ?, ?)",
                                       (payload, journal._signature("checkpoint", payload)))
                with patch("pickle.loads") as deserialize:
                    with self.assertRaisesRegex(ValueError, "nova prévia"):
                        SignedLifecycleCheckpointer(journal)
                deserialize.assert_not_called()
            finally:
                journal.close()

    def test_unsigned_pickle_is_never_deserialized(self):
        with TemporaryDirectory() as directory:
            journal = LifecycleJournal(Path(directory) / "checkpoint.db")
            try:
                SignedLifecycleCheckpointer(journal)
                payload = base64.b64encode(pickle.dumps(({}, {}, {}))).decode("ascii")
                with journal.db:
                    journal.db.execute("INSERT INTO lifecycle_checkpoint VALUES (1, ?, ?)",
                                       (payload, journal._signature("checkpoint", payload)))
                with patch("pickle.loads") as deserialize:
                    with self.assertRaisesRegex(ValueError, "nova prévia"):
                        SignedLifecycleCheckpointer(journal)
                deserialize.assert_not_called()
            finally:
                journal.close()

    def test_no_key_checkpoint_roundtrips_storage_writes_and_blobs(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.db"
            journal = LifecycleJournal(path)
            checkpoint = SignedLifecycleCheckpointer(journal)
            checkpoint.storage["thread"][""]["checkpoint"] = (("json", b"{}"), ("json", b"{}"), None)
            checkpoint.writes[("thread", "", "checkpoint")][("task", 0)] = ("task", "channel", ("json", b"{}"), "path")
            checkpoint.blobs[("thread", "", "channel", "1")] = ("json", b"{}")
            checkpoint._persist()
            self.assertTrue(journal.db.execute("SELECT payload FROM lifecycle_checkpoint").fetchone()[0].startswith("json:"))
            journal.close()
            journal = LifecycleJournal(path)
            try:
                reopened = SignedLifecycleCheckpointer(journal)
                self.assertEqual(reopened.storage, checkpoint.storage)
                self.assertEqual(reopened.writes, checkpoint.writes)
                self.assertEqual(reopened.blobs, checkpoint.blobs)
            finally:
                journal.close()

    def test_corrupt_checkpoint_and_missing_signing_key_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.db"
            journal = LifecycleJournal(path, b"s" * 32)
            SignedLifecycleCheckpointer(journal)._persist()
            journal.close()
            journal = LifecycleJournal(path)
            try:
                SignedLifecycleCheckpointer(journal)
            finally:
                journal.close()
            journal = LifecycleJournal(path, b"s" * 32)
            try:
                with journal.db:
                    journal.db.execute("UPDATE lifecycle_checkpoint SET payload = 'json:[]'")
                with self.assertRaisesRegex(ValueError, "nova prévia"):
                    SignedLifecycleCheckpointer(journal)
            finally:
                journal.close()


class OperationalLifecycleTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "pull_request_template.md").write_text("Repository template", encoding="utf-8")
        self.client = NativeCapabilityClient()
        self.now = NOW
        self.branch_exists = False
        self.branch = "feature/KL-10-story"
        self.head = "base"
        self.emitted = []
        self.journals = []
        self.addCleanup(self.close_journals)
        self.git_mock = patch("kwikledgers_langgraph.lifecycle_adapters.git", side_effect=self.git)
        self.git_mock.start()
        self.addCleanup(self.git_mock.stop)
        self.tasks = Mock()
        self.tasks.plan.return_value = {"plan_id": "trusted", "files": ["owned.py"], "logic": "Change",
                                       "tests": [["unit"]], "risks": []}
        self.tasks.implement.return_value = {"implemented": True}
        self.tasks.test.return_value = {"passed": True, "evidence": [{"returncode": 0}]}
        self.tasks.commit.return_value = {"commit": "commit", "tests_passed": True}
        self.request = {"story_id": 10, "task_id": 11, "repository_name": "payment"}
        self.thread = self.id()
        self.open_runtime("story_start")

    def close_journals(self):
        for journal in self.journals:
            journal.close()

    def git(self, repository, *arguments):
        if arguments[0] == "ls-remote":
            return "base refs/heads/stage-pre-prod"
        if arguments[0] == "for-each-ref":
            return self.branch if self.branch_exists else ""
        if arguments[0] == "status":
            return ""
        if arguments[0] == "rev-parse":
            return self.head if arguments[1] == "HEAD" else "base"
        if arguments[0] == "switch":
            self.branch_exists = True
            return ""
        if arguments[0] in ("fetch", "merge-base"):
            return ""
        raise AssertionError(f"Unexpected git command: {arguments}")

    def execution_issuer(self, state, *, action, capability_name, arguments):
        trusted = active_state(action)
        self.assertEqual(state["verified_lifecycle_binding"], trusted["verified_lifecycle_binding"])
        self.assertEqual(state["pending_approval"]["approved"], True)
        self.emitted.append((action, capability_name, deepcopy(arguments)))
        return f"test:{action}:{capability_name}"

    def runtime_issuer(self, state, *, action, capability_name, arguments):
        self.assertEqual(active_state()["run_id"], state["run_id"])
        self.assertEqual(action, "lifecycle_tracking")
        self.assertEqual(capability_name, "append_daily_action_log")
        self.assertNotIn("verified_lifecycle_binding", state)
        return "test:tracking"

    def open_runtime(self, flow, preview_key=None, *, state_key=b"j" * 32, approvals=None):
        self.flow = flow
        self.journal = LifecycleJournal(self.root / "checkpoint.db", state_key)
        self.journals.append(self.journal)
        self.approvals = approvals if approvals is not None else SignedReceiptApproval(APPROVAL_KEY, clock=lambda: self.now)
        ports = build_lifecycle_ports(repositories={"payment": self.root}, project="KL", area="Squad",
                                      journal=self.journal, approvals=self.approvals, client=self.client,
                                      execution_issuer=self.execution_issuer, runtime_issuer=self.runtime_issuer,
                                      tasks=self.tasks)
        self.ports = replace(ports, clock=SimpleNamespace(now=lambda: self.now.isoformat()))
        self.graph = build_lifecycle_runtime(flow, self.ports, checkpointer=SignedLifecycleCheckpointer(self.journal),
                             preview_key=preview_key)

    def start(self):
        return run_cli(self.graph, thread_id=self.thread, request=self.request)

    def approve(self, result, request_id=None, *, receipt=None):
        return run_cli(self.graph, thread_id=self.thread, resume=True,
                       receipt=receipt if receipt is not None else receipt_for(result["pending_approval"], issued=self.now),
                       approval_request_id=request_id if request_id is not None else result["pending_approval"]["request_id"])

    def finish_start(self):
        result = self.start()
        self.assertEqual(result["pending_approval"]["action"], "activate")
        result = self.approve(result)
        self.assertEqual(result["prompt"]["action"], "select_branch_type")
        result = run_cli(self.graph, thread_id=self.thread, resume=True, branch_type="feature")
        self.assertEqual(result["pending_approval"]["action"], "branch")
        result = self.approve(result)
        self.assertEqual(result["pending_approval"]["action"], "branch_links")
        result = self.approve(result)
        self.assertEqual(result["outcome"], "completed")
        return result

    def completion(self):
        self.finish_start()
        self.journal.put("task:payment:11:commit", {"commit": "commit", "tests_passed": True})
        self.head = "commit"
        self.thread += ":completion"
        self.open_runtime("story_completion")
        return self.approve(self.start())

    def azure_effects(self):
        return [(name, arguments) for name, arguments in self.client.effect_calls if name != "append_daily_action_log"]

    def test_native_start_uses_signed_receipts_and_all_item_revisions(self):
        first = self.start()
        self.assertEqual(self.azure_effects(), [])
        self.assertEqual(first["pending_approval"]["preview"]["assigned_to"], "native@example.com")
        self.assertEqual(first["pending_approval"]["preview"]["area_path"], "KL\\Squad")
        result = self.approve(first)
        result = run_cli(self.graph, thread_id=self.thread, resume=True, branch_type="feature")
        result = self.approve(result)
        self.assertEqual(result["pending_approval"]["preview"]["expected_revisions"], {"10": 1, "11": 3, "12": 3})
        self.assertEqual(self.approve(result)["outcome"], "completed")
        self.assertEqual([name for name, _ in self.azure_effects()], ["update_work_item_content", "associate_story_branch"])
        context = {"repository": "payment", "story_id": 10, "task_id": 11}
        self.assertTrue(self.ports.tracking.story_start_complete(context))
        self.tasks.implement.assert_not_called()

    def test_checkpoint_restart_accepts_the_same_pending_receipt_once(self):
        first = self.start()
        receipt = receipt_for(first["pending_approval"], issued=self.now)
        self.journal.close()
        self.journals.remove(self.journal)
        self.open_runtime("story_start")
        result = self.approve(first, receipt=receipt)
        self.assertEqual(result["run_id"], first["run_id"])
        self.assertEqual(result["prompt"]["action"], "select_branch_type")
        self.assertEqual(len(self.azure_effects()), 1)
        with self.assertRaises(ValueError):
            self.approve(first, receipt=receipt)
        self.assertEqual(len(self.azure_effects()), 1)

    def test_no_key_restart_requires_signed_receipt_then_new_stage_approval(self):
        self.journal.close()
        self.journals.remove(self.journal)
        self.open_runtime("story_start", state_key=None)
        first = self.start()
        self.assertEqual(self.azure_effects(), [])
        request_id = first["pending_approval"]["request_id"]
        receipt = receipt_for(first["pending_approval"], issued=self.now)
        self.journal.close()
        self.journals.remove(self.journal)
        self.open_runtime("story_start", state_key=None)
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id=self.thread, resume=True, receipt=receipt, approval_request_id="foreign")
        self.assertEqual(self.azure_effects(), [])
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id=self.thread, resume=True, approval_request_id=request_id)
        self.assertEqual(self.azure_effects(), [])
        result = self.approve(first)
        self.assertEqual(result["run_id"], first["run_id"])
        self.assertEqual(result["prompt"]["action"], "select_branch_type")
        self.assertEqual(len(self.azure_effects()), 1)
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id=self.thread, resume=True, receipt=receipt, approval_request_id=request_id)
        result = run_cli(self.graph, thread_id=self.thread, resume=True, branch_type="feature")
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id=self.thread, resume=True, receipt=receipt, approval_request_id=request_id)
        self.assertEqual(len(self.azure_effects()), 1)
        result = self.approve(result)
        self.assertEqual(result["pending_approval"]["action"], "branch_links")
        result = self.approve(result)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual([name for name, _ in self.azure_effects()], ["update_work_item_content", "associate_story_branch"])

    def test_lifecycle_main_requires_key_and_signed_receipts_on_reopen(self):
        operator = self.root / "operator.json"
        operator.write_text(json.dumps({"repositories": {"payment": str(self.root)},
                                        "project": "KL", "area": "Squad"}), encoding="utf-8")
        base = ["story_start", "--operator-config", str(operator), "--checkpoint", str(self.root / "checkpoint.db"),
                "--thread-id", self.thread]
        self.journal.close()
        self.journals.remove(self.journal)

        def ports_for(**kwargs):
            return replace(build_lifecycle_ports(client=self.client, tasks=self.tasks, **kwargs),
                           clock=SimpleNamespace(now=lambda: self.now.isoformat()))

        def invoke(suffix, expected_code):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(base + suffix), expected_code)
            return json.loads(output.getvalue())

        def approve(result, expected_code):
            receipt_path = self.root / "receipt.json"
            receipt_path.write_text(json.dumps(receipt_for(result["pending_approval"], issued=self.now)), encoding="utf-8")
            return invoke(["--resume", "--receipt", str(receipt_path),
                           "--approval-request-id", result["pending_approval"]["request_id"]], expected_code)

        with patch.dict("os.environ", {}, clear=True), \
                patch("kwikledgers_langgraph.lifecycle_cli.lifecycle_issuers",
                      return_value=(self.execution_issuer, self.runtime_issuer)), \
                patch("kwikledgers_langgraph.lifecycle_cli.build_lifecycle_ports", side_effect=ports_for):
            with redirect_stderr(StringIO()):
                self.assertEqual(main(base + ["--story-id", "10", "--task-id", "11",
                                              "--repository-name", "payment"]), 1)
            self.assertEqual(self.azure_effects(), [])
        self.assertEqual(self.azure_effects(), [])
        with patch.dict("os.environ", {"KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": APPROVAL_KEY.decode()}, clear=True), \
                patch("kwikledgers_langgraph.lifecycle_cli.SignedReceiptApproval", return_value=self.approvals), \
                patch("kwikledgers_langgraph.lifecycle_cli.lifecycle_issuers",
                      return_value=(self.execution_issuer, self.runtime_issuer)), \
                patch("kwikledgers_langgraph.lifecycle_cli.build_lifecycle_ports", side_effect=ports_for):
            first = invoke(["--story-id", "10", "--task-id", "11", "--repository-name", "payment"], 2)
            with redirect_stderr(StringIO()):
                self.assertEqual(main(base + ["--resume", "--approval-request-id",
                                              first["pending_approval"]["request_id"]]), 1)
            self.assertEqual(self.azure_effects(), [])
            result = approve(first, 2)
            self.assertEqual(result["prompt"]["action"], "select_branch_type")
            result = invoke(["--resume", "--branch-type", "feature"], 2)
            result = approve(result, 2)
            result = approve(result, 0)
            self.assertEqual(result["run_id"], first["run_id"])
            self.assertEqual(result["outcome"], "completed")
        self.assertEqual([name for name, _ in self.azure_effects()], ["update_work_item_content", "associate_story_branch"])

    def test_expired_receipt_never_reaches_native_mutation(self):
        first = self.start()
        receipt = receipt_for(first["pending_approval"])
        self.now += timedelta(minutes=5)
        result = run_cli(self.graph, thread_id=self.thread, resume=True, receipt=receipt)
        self.assertEqual(result["outcome"], "denied")
        self.assertEqual(self.azure_effects(), [])

    def test_revision_change_blocks_activation(self):
        first = self.start()
        self.client.items[11]["revision"] += 1
        result = self.approve(first)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(self.azure_effects(), [])

    def test_tracking_failure_at_start_blocks_before_preview(self):
        self.client.tracking_failure = True
        result = self.start()
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["pending_approval"], {})
        self.assertEqual(self.azure_effects(), [])

    def test_missing_area_records_start_but_never_activates(self):
        self.ports.context.area = ""
        result = self.start()
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(self.ports.tracking.started_at("payment", 11), NOW.isoformat())
        self.assertEqual(self.azure_effects(), [])

    def test_changed_operator_area_invalidates_pending_activation(self):
        first = self.start()
        self.ports.context.area = "Other"
        result = self.approve(first)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(self.azure_effects(), [])

    def test_tracking_failure_before_execution_blocks_mutation(self):
        first = self.start()
        def fail_execution(arguments):
            event = json.loads(arguments["details"])
            if event["action"] == "activate" and event["phase"] == "execution_started":
                raise OSError("Tracking unavailable")
        self.client.tracking_hook = fail_execution
        self.assertEqual(self.approve(first)["outcome"], "blocked")
        self.assertEqual(self.azure_effects(), [])

    def test_receipt_expiring_between_gate_and_execute_is_rechecked(self):
        first = self.start()
        receipt = receipt_for(first["pending_approval"])
        def expire_execution(arguments):
            event = json.loads(arguments["details"])
            if event["action"] == "activate" and event["phase"] == "execution_started":
                self.now = NOW + timedelta(minutes=5)
        self.client.tracking_hook = expire_execution
        self.assertEqual(run_cli(self.graph, thread_id=self.thread, resume=True, receipt=receipt)["outcome"], "blocked")
        self.assertEqual(self.azure_effects(), [])

    def test_completion_returns_web_url_then_requires_new_status_receipt(self):
        result = self.completion()
        self.assertEqual(result["pending_approval"]["action"], "draft_pr")
        old_receipt = receipt_for(result["pending_approval"], issued=self.now)
        result = self.approve(result, receipt=old_receipt)
        self.assertEqual(result["direct_pr_url"], "https://dev.azure.com/KL/_git/payment/pullrequest/1")
        self.assertEqual(result["pending_approval"]["action"], "awaiting_pr")
        self.assertEqual(self.client.items[10]["state"], "Desenvolvimento")
        self.assertFalse(self.approvals.verify(result["pending_approval"], old_receipt))
        result = self.approve(result)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(self.client.items[10]["state"], "Aguardando PR")

    def test_pr_child_revision_change_blocks_draft(self):
        result = self.completion()
        self.client.items[12]["revision"] += 1
        result = self.approve(result)
        self.assertEqual(result["outcome"], "blocked")
        self.assertNotIn("create_story_pull_request", [name for name, _ in self.azure_effects()])

    def test_created_body_mismatch_blocks_story_status(self):
        result = self.completion()
        self.client.body_changed = True
        self.assertEqual(self.approve(result)["outcome"], "blocked")
        self.assertEqual(self.client.items[10]["state"], "Desenvolvimento")
        self.assertNotIn("update_story_status", [name for name, _ in self.azure_effects()])

    def test_post_mutation_tracking_failure_keeps_signed_result_and_stops(self):
        first = self.start()
        def fail_executed(arguments):
            event = json.loads(arguments["details"])
            if event["action"] == "activate" and event["phase"] == "executed":
                raise OSError("Tracking unavailable")
        self.client.tracking_hook = fail_executed
        result = self.approve(first)
        self.assertEqual(result["outcome"], "reconciliation_required")
        self.assertIn("activate", result["results"])
        self.assertIsNotNone(self.journal.get(first["run_id"] + ":activate:result"))
        self.assertEqual(len(self.azure_effects()), 1)

    def test_task_cycle_uses_mock_executor_and_preserves_remaining_work(self):
        self.finish_start()
        self.thread += ":task"
        self.open_runtime("task_cycle")
        result = self.start()
        self.assertEqual(result["pending_approval"]["action"], "implement")
        result = self.approve(result)
        self.assertEqual(result["pending_approval"]["action"], "commit")
        self.now += timedelta(hours=1, minutes=1)
        result = self.approve(result)
        self.assertEqual(result["pending_approval"]["preview"]["completed_work_hours"], 2)
        self.assertEqual(result["pending_approval"]["action"], "hours")
        hours_revision = result["pending_approval"]["preview"]["expected_revision"]
        result = self.approve(result)
        self.assertEqual(result["pending_approval"]["action"], "close")
        self.assertEqual(result["pending_approval"]["preview"]["expected_revision"], hours_revision + 1)
        result = self.approve(result)
        self.assertEqual(result["outcome"], "completed")
        updates = [arguments for name, arguments in self.azure_effects() if name == "update_work_item_effort"]
        self.assertEqual(len(updates), 2)
        self.assertTrue(all("remaining_work_hours" not in arguments for arguments in updates))
        self.assertEqual(self.tasks.implement.call_args.args, ("trusted",))
        self.assertEqual(self.tasks.commit.call_args.args, ("trusted", 11))

    def test_changed_close_revision_prevents_closed_status(self):
        self.finish_start()
        self.thread += ":task"
        self.open_runtime("task_cycle")
        result = self.approve(self.approve(self.approve(self.start())))
        self.assertEqual(result["pending_approval"]["action"], "close")
        self.client.items[11]["revision"] += 1
        result = self.approve(result)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(self.client.items[11]["state"], "Active")

    def test_debt_items_require_separate_receipts_and_created_parent_binding(self):
        self.journal.put("prepared", {"repository": "payment", "technical_debt_preview": {
            "repository_name": "payment", "creation_blocked": False, "exact_match": False,
            "parent": {"work_item_type": "Technical Debt", "title": "Debt", "iteration_path": "KL\\Sprint"},
            "children": [{"work_item_type": "Task", "title": "First"},
                         {"work_item_type": "Task", "title": "Second"}],
        }})
        self.thread += ":debt"
        self.open_runtime("technical_debt_execute", preview_key="prepared")
        result = self.start()
        self.assertEqual(result["pending_approval"]["action"], "debt_parent")
        result = self.approve(result)
        self.assertEqual(result["pending_approval"]["action"], "debt_child_0")
        self.assertEqual(result["pending_approval"]["preview"]["parent_work_item_id"], 101)
        result = self.approve(result)
        self.assertEqual(result["pending_approval"]["action"], "debt_child_1")
        result = self.approve(result)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual([(action, name) for action, name, _ in self.emitted], [
            ("debt_parent", "create_work_item"), ("debt_child_0", "create_work_item"),
            ("debt_child_1", "create_work_item")])

    def test_checkpoint_signature_is_verified_before_deserialization(self):
        self.start()
        with self.journal.db:
            self.journal.db.execute("UPDATE lifecycle_checkpoint SET payload = ? WHERE id = 1", ("untrusted",))
        with patch("pickle.loads") as deserialize:
            with self.assertRaisesRegex(ValueError, "nova prévia"):
                SignedLifecycleCheckpointer(self.journal)
        deserialize.assert_not_called()