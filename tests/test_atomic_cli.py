from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from kwikledgers_langgraph.atomic_cli import load_json, main, parser, run_atomic_cli
from kwikledgers_langgraph.atomic_runtime import MUTATION_SCHEMAS, PREVIEW_SCHEMAS, READ_SCHEMAS
from kwikledgers_langgraph.lifecycle_adapters import ExplicitLocalApproval, LifecycleJournal
from kwikledgers_langgraph.lifecycle_runtime import SignedLifecycleCheckpointer
from test_atomic_runtime import AtomicRuntimeHarness, PAYLOADS
from test_lifecycle_adapters import receipt_for


class AtomicCLIContractTests(TestCase):
    def setUp(self):
        self.graph = Mock()
        self.graph.get_state.return_value = SimpleNamespace(values={}, next=())
        self.graph.invoke.return_value = {"outcome": "running", "run_id": "run"}
        self.payload = PAYLOADS["send_notification"]
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def invoke(self, **kwargs):
        return run_atomic_cli(self.graph, name="send_notification", thread_id="thread",
                              arguments=self.payload, **kwargs)

    def interrupted(self):
        self.graph.get_state.return_value = SimpleNamespace(values={
            "context": {"atomic_capability": "send_notification", "atomic_arguments": self.payload},
            "observability": {"flow_name": "atomic_send_notification"},
            "pending_approval": {"action": "atomic_send_notification"}}, next=("atomic_send_notification_gate",))

    def test_parser_exposes_every_read_and_preview(self):
        for name in (*READ_SCHEMAS, *PREVIEW_SCHEMAS):
            with self.subTest(name=name):
                parsed = parser().parse_args(["read", name, "--arguments-file", "arguments.json"])
                self.assertEqual(parsed.name, name)

    def test_parser_exposes_only_approved_atomic_mutations(self):
        for name in MUTATION_SCHEMAS:
            with self.subTest(name=name):
                args = parser().parse_args(["mutation", name, "--operator-config", "operator.json",
                    "--checkpoint", "state.db", "--thread-id", "thread", "--arguments-file", "arguments.json"])
                self.assertEqual(args.name, name)

    def test_parser_has_no_unsafe_boolean_approval(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(["mutation", "send_notification", "--approved", "--operator-config", "operator.json",
                                "--checkpoint", "state.db", "--thread-id", "thread"])

    def test_parser_rejects_governed_generic_mutations(self):
        for name in ("create_work_item", "update_story_status", "update_work_item_effort", "associate_story_branch"):
            with self.subTest(name=name), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["mutation", name])

    def test_initial_invocation_cannot_include_receipt(self):
        with self.assertRaises(ValueError):
            self.invoke(receipt={"claims": {}})
        self.graph.invoke.assert_not_called()

    def test_initial_invocation_cannot_include_explicit_approval(self):
        with self.assertRaises(ValueError):
            self.invoke(approval_request_id="request")
        self.graph.invoke.assert_not_called()

    def test_parser_exposes_request_id_and_rejects_receipt_combination(self):
        base = ["mutation", "send_notification", "--operator-config", "operator.json",
                "--checkpoint", "state.db", "--thread-id", "thread", "--resume",
                "--approval-request-id", "request"]
        self.assertEqual(parser().parse_args(base).approval_request_id, "request")
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(base + ["--receipt", "receipt.json"])

    def test_existing_thread_cannot_be_reset(self):
        self.interrupted()
        with self.assertRaises(ValueError):
            self.invoke()
        self.graph.invoke.assert_not_called()

    def test_resume_requires_interrupted_thread(self):
        with self.assertRaises(ValueError):
            self.invoke(resume=True, receipt={})
        self.graph.invoke.assert_not_called()

    def test_resume_requires_separate_receipt(self):
        self.interrupted()
        with self.assertRaises(ValueError):
            self.invoke(resume=True)
        self.graph.invoke.assert_not_called()

    def test_resume_cannot_change_arguments(self):
        self.interrupted()
        self.graph.get_state.return_value.values["context"]["atomic_arguments"] = {"title": "Other", "message": "Ready."}
        with self.assertRaises(ValueError):
            self.invoke(resume=True, receipt={})
        self.graph.invoke.assert_not_called()

    def test_resume_cannot_change_capability_or_flow(self):
        for field in ("atomic_capability", "flow_name", "action"):
            with self.subTest(field=field):
                self.interrupted()
                state = self.graph.get_state.return_value.values
                owner = "context" if field == "atomic_capability" else "observability" if field == "flow_name" else "pending_approval"
                state[owner][field] = "different"
                with self.assertRaises(ValueError):
                    self.invoke(resume=True, receipt={})
        self.graph.invoke.assert_not_called()

    def test_resume_rejects_receipt_before_invoking_graph(self):
        self.interrupted()
        receipt = {"claims": {"subject": "human"}, "signature": "external"}
        with self.assertRaisesRegex(ValueError, "removed"):
            self.invoke(resume=True, receipt=receipt)
        self.graph.invoke.assert_not_called()

    def test_output_preserves_reconciliation_evidence(self):
        self.graph.invoke.return_value = {
            "outcome": "reconciliation_required", "outcome_reason": "post_execution_evidence_failed",
            "results": {"atomic_send_notification": {"executed": True}},
            "direct_pr_url": "https://example.test/pr/1", "errors": ["tracking_unavailable"],
        }
        output = self.invoke()
        self.assertEqual(output["outcome"], "reconciliation_required")
        self.assertEqual(output["outcome_reason"], "post_execution_evidence_failed")
        self.assertEqual(output["results"], self.graph.invoke.return_value["results"])
        self.assertEqual(output["direct_pr_url"], "https://example.test/pr/1")
        self.graph.invoke.assert_called_once()

    def test_main_mutation_exit_codes(self):
        base = ["mutation", "send_notification", "--operator-config", "operator.json",
                "--checkpoint", str(self.root / "state.db"), "--thread-id", "thread",
                "--arguments-file", "arguments.json"]
        checkpoint = Mock()
        checkpoint.get.return_value = None
        for outcome, expected in (("completed", 0), ("blocked", 1), ("denied", 1),
                                  ("waiting", 2), ("interrupted", 2), ("reconciliation_required", 3)):
            with self.subTest(outcome=outcome), \
                    patch("kwikledgers_langgraph.atomic_cli.lifecycle_issuers", return_value=(Mock(), Mock())), \
                    patch("kwikledgers_langgraph.atomic_cli.load_json", return_value={
                        "repositories": {}, "project": "KL", "area": "Squad"}), \
                    patch("kwikledgers_langgraph.atomic_cli.validate_arguments", return_value=self.payload), \
                    patch("kwikledgers_langgraph.atomic_cli.ExplicitLocalApproval"), \
                    patch("kwikledgers_langgraph.atomic_cli.LifecycleJournal"), \
                    patch("kwikledgers_langgraph.atomic_cli.SignedLifecycleCheckpointer", return_value=checkpoint), \
                    patch("kwikledgers_langgraph.atomic_cli.build_lifecycle_ports"), \
                    patch("kwikledgers_langgraph.atomic_cli.build_atomic_runtime"), \
                    patch("kwikledgers_langgraph.atomic_cli.run_atomic_cli", return_value={"outcome": outcome}), \
                    redirect_stdout(StringIO()):
                self.assertEqual(main(base), expected)

    def test_nonempty_thread_is_required(self):
        with self.assertRaises(ValueError):
            run_atomic_cli(self.graph, name="send_notification", thread_id=" ", arguments=self.payload)
        self.graph.invoke.assert_not_called()

    def test_main_read_does_not_require_keys_issuers_or_operator_config(self):
        arguments = self.root / "read.json"
        arguments.write_text("{}", encoding="utf-8")
        client = Mock()
        client.call.return_value = {"native": "snapshot"}
        output = StringIO()
        with patch("kwikledgers_langgraph.atomic_cli.FastMCPClient", return_value=client), \
                patch("kwikledgers_langgraph.atomic_cli.lifecycle_issuers") as issuer, redirect_stdout(output):
            self.assertEqual(main(["read", "read_tracking_snapshot", "--arguments-file", str(arguments)]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"native": "snapshot"})
        issuer.assert_not_called()
        client.call.assert_called_once_with("read_tracking_snapshot", {})

    def test_strict_json_rejects_duplicates_nonfinite_and_nonobjects(self):
        path = self.root / "invalid.json"
        for contents in ('{"approved": false, "approved": true}', '{"count": NaN}', '[]'):
            with self.subTest(contents=contents):
                path.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_json(path)

    def test_main_requires_receipt_only_on_separate_resume(self):
        base = ["mutation", "send_notification", "--operator-config", "operator.json",
                "--checkpoint", "state.db", "--thread-id", "thread"]
        for suffix in (["--receipt", "receipt.json"], ["--resume"], []):
            with self.subTest(suffix=suffix), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main(base + suffix)

    def test_unavailable_specific_issuer_fails_closed_without_client(self):
        base = ["mutation", "send_notification", "--operator-config", "operator.json",
                "--checkpoint", str(self.root / "state.db"), "--thread-id", "thread", "--arguments-file", "arguments.json"]
        with patch("kwikledgers_langgraph.atomic_cli.lifecycle_issuers", side_effect=RuntimeError("Unavailable")), \
                patch("kwikledgers_langgraph.atomic_cli.FastMCPClient") as client, redirect_stderr(StringIO()):
            self.assertEqual(main(base), 1)
        client.assert_not_called()


class AtomicCLIIntegrationTests(AtomicRuntimeHarness, TestCase):
    def test_catalog_mutations_accept_separate_local_approval_without_any_key(self):
        self.journal.close()
        self.journal = LifecycleJournal(self.path)
        self.gateway.journal = self.journal
        self.ports = replace(self.ports, approvals=ExplicitLocalApproval())
        self.checkpoint = SignedLifecycleCheckpointer(self.journal)
        for name in MUTATION_SCHEMAS:
            with self.subTest(name=name):
                graph = self.graph(name)
                thread_id = self.id() + ":" + name
                first = run_atomic_cli(graph, name=name, thread_id=thread_id, arguments=PAYLOADS[name])
                count = len(self.client.effects)
                request_id = first["pending_approval"]["request_id"]
                with self.assertRaises(ValueError):
                    run_atomic_cli(graph, name=name, thread_id=thread_id, arguments=PAYLOADS[name],
                                   resume=True, approval_request_id="foreign")
                self.assertEqual(len(self.client.effects), count)
                result = run_atomic_cli(graph, name=name, thread_id=thread_id, arguments=PAYLOADS[name],
                                        resume=True, approval_request_id=request_id)
                self.assertEqual(result["outcome"], "completed")
                self.assertEqual(len(self.client.effects), count + 1)
                with self.assertRaises(ValueError):
                    run_atomic_cli(graph, name=name, thread_id=thread_id, arguments=PAYLOADS[name],
                                   resume=True, approval_request_id=request_id)
                self.assertEqual(len(self.client.effects), count + 1)

    def test_main_reopens_no_key_checkpoint_for_explicit_approval(self):
        operator = Path(self.directory.name) / "operator.json"
        arguments = Path(self.directory.name) / "arguments.json"
        operator.write_text(json.dumps({"repositories": {"payment": self.directory.name},
                                       "project": "KL", "area": "Squad"}), encoding="utf-8")
        arguments.write_text(json.dumps(PAYLOADS["send_notification"]), encoding="utf-8")
        base = ["mutation", "send_notification", "--operator-config", str(operator),
                "--checkpoint", str(self.path), "--thread-id", self.id()]
        self.journal.close()

        def ports_for(**kwargs):
            self.journal = kwargs["journal"]
            self.gateway.journal = self.journal
            return replace(self.ports, approvals=kwargs["approvals"])

        with patch.dict("os.environ", {}, clear=True), \
                patch("kwikledgers_langgraph.atomic_cli.lifecycle_issuers", return_value=(self.issue, Mock())), \
                patch("kwikledgers_langgraph.atomic_cli.build_lifecycle_ports", side_effect=ports_for):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(base + ["--arguments-file", str(arguments)]), 2)
            first = json.loads(output.getvalue())
            self.assertEqual(self.client.effects, [])
            resumed = StringIO()
            suffix = ["--resume", "--approval-request-id", first["pending_approval"]["request_id"]]
            with redirect_stdout(resumed):
                self.assertEqual(main(base + suffix), 0)
            self.assertEqual(json.loads(resumed.getvalue())["run_id"], first["run_id"])
            with redirect_stderr(StringIO()):
                self.assertEqual(main(base + suffix), 1)
        self.assertEqual(len(self.client.effects), 1)

    def test_cli_preview_and_resume_with_external_receipt(self):
        graph = self.graph("send_notification")
        first = run_atomic_cli(graph, name="send_notification", thread_id=self.id(), arguments=PAYLOADS["send_notification"])
        self.assertEqual(first["prompt"], first["pending_approval"])
        self.assertEqual(self.client.effects, [])
        result = run_atomic_cli(graph, name="send_notification", thread_id=self.id(), arguments=PAYLOADS["send_notification"],
                                resume=True, approval_request_id=first["pending_approval"]["request_id"])
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["results"]["atomic_send_notification"]["executed"], True)
        with self.assertRaises(ValueError):
            run_atomic_cli(graph, name="send_notification", thread_id=self.id(), arguments=PAYLOADS["send_notification"],
                           resume=True, approval_request_id=first["pending_approval"]["request_id"])
        self.assertEqual(len(self.client.effects), 1)

    def test_main_reopens_signed_checkpoint_and_resumes_without_arguments_file(self):
        operator = Path(self.directory.name) / "operator.json"
        arguments = Path(self.directory.name) / "arguments.json"
        receipt_path = Path(self.directory.name) / "receipt.json"
        operator.write_text(json.dumps({"repositories": {"payment": self.directory.name}, "project": "KL", "area": "Squad"}),
                            encoding="utf-8")
        arguments.write_text(json.dumps(PAYLOADS["send_notification"]), encoding="utf-8")
        base = ["mutation", "send_notification", "--operator-config", str(operator),
                "--checkpoint", str(self.path), "--thread-id", self.id()]

        def ports_for(**kwargs):
            self.gateway.journal = kwargs["journal"]
            return self.ports

        with patch.dict("os.environ", {"KWIKLEDGERS_LIFECYCLE_STATE_KEY": "j" * 32,
                                      "KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": "a" * 32}), \
                patch("kwikledgers_langgraph.atomic_cli.lifecycle_issuers", return_value=(self.issue, Mock())), \
                patch("kwikledgers_langgraph.atomic_cli.ExplicitLocalApproval", return_value=self.approvals), \
                patch("kwikledgers_langgraph.atomic_cli.build_lifecycle_ports", side_effect=ports_for):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(base + ["--arguments-file", str(arguments)]), 2)
            first = json.loads(output.getvalue())
            self.assertEqual(self.client.effects, [])
            receipt_path.write_text(json.dumps(receipt_for(first["pending_approval"])), encoding="utf-8")
            resumed = StringIO()
            with redirect_stdout(resumed):
                self.assertEqual(main(base + ["--resume", "--approval-request-id", first["pending_approval"]["request_id"]]), 0)
            final = json.loads(resumed.getvalue())
            self.assertEqual(final["outcome"], "completed")
            self.assertEqual(final["run_id"], first["run_id"])
            with redirect_stderr(StringIO()):
                self.assertEqual(main(base + ["--resume", "--approval-request-id", first["pending_approval"]["request_id"]]), 1)
        self.assertEqual(len(self.client.effects), 1)