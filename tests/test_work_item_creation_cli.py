from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from kwikledgers_langgraph.work_item_creation import ACTION, CreationValidationError
from kwikledgers_langgraph.lifecycle_adapters import SignedReceiptApproval
from kwikledgers_langgraph.work_item_creation_cli import main, parser, run_work_item_creation_cli
from test_lifecycle_adapters import APPROVAL_KEY, NOW, receipt_for
from test_work_item_creation import CreationHarness, formatter


class WorkItemCreationCLIContractTests(CreationHarness, TestCase):
    def invoke(self, graph, **kwargs):
        return run_work_item_creation_cli(graph, thread_id=self.id(), arguments=self.arguments,
                                          operator=self.operator, **kwargs)

    def test_cli_preview_and_separate_resume(self):
        graph = self.graph()
        first = self.invoke(graph)
        self.assertEqual(first["prompt"], first["pending_approval"])
        self.assertFalse(self.effects)
        receipt = receipt_for(first["pending_approval"])
        result = self.invoke(graph, resume=True, receipt=receipt,
                     approval_request_id=first["pending_approval"]["request_id"])
        self.assertEqual(result["results"][ACTION]["id"], 42)
        with self.assertRaises(CreationValidationError):
            self.invoke(graph, resume=True, receipt=receipt, approval_request_id=first["pending_approval"]["request_id"])
        self.assertEqual(len(self.effects), 1)

    def test_no_boolean_approval_or_receipt_on_preview(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(["--approved"])
        graph = self.graph()
        with self.assertRaises(CreationValidationError):
            self.invoke(graph, receipt={})
        with self.assertRaises(CreationValidationError):
            self.invoke(graph, approval_request_id="request")
        self.assertFalse(self.effects)

    def test_parser_accepts_receipt_with_optional_matching_request_id(self):
        base = ["--operator-config", "operator.json", "--checkpoint", "state.db", "--thread-id", "thread",
                "--resume", "--approval-request-id", "request"]
        self.assertEqual(parser().parse_args(base).approval_request_id, "request")
        self.assertEqual(parser().parse_args(base + ["--receipt", "receipt.json"]).receipt, Path("receipt.json"))

    def test_request_id_alone_cannot_authorize_current_creation_preview(self):
        self.ports = replace(self.ports, approvals=SignedReceiptApproval(APPROVAL_KEY, clock=lambda: NOW))
        graph = self.graph()
        first = self.invoke(graph)
        self.assertFalse(self.effects)
        with self.assertRaises(CreationValidationError):
            self.invoke(graph, resume=True, receipt=receipt_for(first["pending_approval"]), approval_request_id="foreign")
        with self.assertRaises(CreationValidationError):
            self.invoke(graph, resume=True, approval_request_id=first["pending_approval"]["request_id"])
        self.assertFalse(self.effects)
        result = self.invoke(graph, resume=True, receipt=receipt_for(first["pending_approval"]),
                             approval_request_id=first["pending_approval"]["request_id"])
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["results"][ACTION]["id"], 42)
        self.assertEqual(len(self.effects), 1)

    def test_main_reopens_no_key_checkpoint_only_with_signed_receipt(self):
        operator_path = Path(self.directory.name) / "operator.json"
        arguments_path = Path(self.directory.name) / "arguments.json"
        receipt_path = Path(self.directory.name) / "receipt.json"
        operator_path.write_text(json.dumps(self.operator), encoding="utf-8")
        arguments_path.write_text(json.dumps(self.arguments), encoding="utf-8")
        base = ["--operator-config", str(operator_path), "--checkpoint", str(self.path), "--thread-id", self.id()]
        self.journal.close()

        def ports_for(**kwargs):
            self.journal = kwargs["journal"]
            self.gateway.journal = self.journal
            return replace(self.ports, approvals=kwargs["approvals"])

        for key in ("", "a" * 31):
            with self.subTest(key=key), patch.dict("os.environ", {"KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": key}, clear=True), \
                    patch("kwikledgers_langgraph.work_item_creation_cli.lifecycle_issuers", return_value=(self.issue, Mock())), \
                    patch("kwikledgers_langgraph.work_item_creation_cli.build_lifecycle_ports") as ports, \
                    redirect_stderr(StringIO()):
                self.assertEqual(main(base + ["--arguments-file", str(arguments_path)]), 1)
                ports.assert_not_called()
                self.assertFalse(self.effects)

        with patch.dict("os.environ", {"KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": APPROVAL_KEY.decode()}, clear=True), \
                patch("kwikledgers_langgraph.work_item_creation_cli.lifecycle_issuers", return_value=(self.issue, Mock())), \
            patch("kwikledgers_langgraph.work_item_creation_cli.SignedReceiptApproval", return_value=self.approvals), \
                patch("kwikledgers_langgraph.work_item_creation_cli.build_lifecycle_ports", side_effect=ports_for), \
                patch("kwikledgers_langgraph.work_item_creation_cli.native_formatter", side_effect=formatter):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(base + ["--arguments-file", str(arguments_path)]), 2)
            first = json.loads(output.getvalue())
            self.assertFalse(self.effects)
            suffix = ["--resume", "--approval-request-id", first["pending_approval"]["request_id"]]
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main(base + suffix)
            self.assertFalse(self.effects)
            receipt_path.write_text(json.dumps(receipt_for(first["pending_approval"])), encoding="utf-8")
            suffix += ["--receipt", str(receipt_path)]
            resumed = StringIO()
            with redirect_stdout(resumed):
                self.assertEqual(main(base + suffix), 0)
            self.assertEqual(json.loads(resumed.getvalue())["run_id"], first["run_id"])
            with redirect_stderr(StringIO()):
                self.assertEqual(main(base + suffix), 1)
        self.assertEqual(len(self.effects), 1)

    def test_resume_cannot_change_request_operator_or_flow(self):
        graph = Mock()
        original = {"context": {"creation_arguments": self.arguments, "creation_operator": self.operator},
                    "observability": {"flow_name": ACTION}, "pending_approval": {"action": ACTION}}
        for owner, field in (("context", "creation_arguments"), ("context", "creation_operator"),
                             ("observability", "flow_name"), ("pending_approval", "action")):
            changed = deepcopy(original)
            changed[owner][field] = "changed"
            graph.get_state.return_value = SimpleNamespace(values=changed, next=("gate",))
            with self.subTest(field=field), self.assertRaises(CreationValidationError):
                self.invoke(graph, resume=True, receipt={})
        graph.invoke.assert_not_called()

    def test_missing_inputs_fail_before_client_or_checkpoint_creation(self):
        with patch.dict("os.environ", {}, clear=True), redirect_stderr(StringIO()), \
                patch("kwikledgers_langgraph.work_item_creation_cli.build_lifecycle_ports") as ports:
            self.assertEqual(main(["--operator-config", "missing.json", "--arguments-file", "missing.json",
                "--checkpoint", str(Path(self.directory.name) / "missing.db"), "--thread-id", "thread"]), 1)
        ports.assert_not_called()
        self.assertFalse((Path(self.directory.name) / "missing.db").exists())

    def test_shared_state_and_approval_keys_do_not_block_operator_loading(self):
        with patch.dict("os.environ", {"KWIKLEDGERS_LIFECYCLE_STATE_KEY": "j" * 32,
                                       "KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": "j" * 32}), \
                patch("kwikledgers_langgraph.work_item_creation_cli.load_json") as loader, \
                patch("kwikledgers_langgraph.work_item_creation_cli.build_lifecycle_ports") as ports, \
                redirect_stderr(StringIO()):
            self.assertEqual(main(["--operator-config", "operator.json", "--arguments-file", "arguments.json",
                                  "--checkpoint", str(self.path), "--thread-id", "thread"]), 1)
        loader.assert_called_once()
        ports.assert_not_called()

    def test_cli_reopens_signed_checkpoint_without_arguments_file(self):
        operator_path = Path(self.directory.name) / "operator.json"
        arguments_path = Path(self.directory.name) / "arguments.json"
        receipt_path = Path(self.directory.name) / "receipt.json"
        operator_path.write_text(json.dumps(self.operator), encoding="utf-8")
        arguments_path.write_text(json.dumps(self.arguments), encoding="utf-8")
        base = ["--operator-config", str(operator_path), "--checkpoint", str(self.path), "--thread-id", self.id()]

        def ports_for(**kwargs):
            self.gateway.journal = kwargs["journal"]
            return self.ports

        with patch.dict("os.environ", {"KWIKLEDGERS_LIFECYCLE_STATE_KEY": "j" * 32,
                                       "KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY": "a" * 32}), \
                patch("kwikledgers_langgraph.work_item_creation_cli.lifecycle_issuers", return_value=(self.issue, Mock())), \
                patch("kwikledgers_langgraph.work_item_creation_cli.SignedReceiptApproval", return_value=self.approvals), \
                patch("kwikledgers_langgraph.work_item_creation_cli.build_lifecycle_ports", side_effect=ports_for), \
                patch("kwikledgers_langgraph.work_item_creation_cli.native_formatter", side_effect=formatter):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(base + ["--arguments-file", str(arguments_path)]), 2)
            first = json.loads(output.getvalue())
            self.assertFalse(self.effects)
            receipt_path.write_text(json.dumps(receipt_for(first["pending_approval"])), encoding="utf-8")
            resumed = StringIO()
            with redirect_stdout(resumed):
                self.assertEqual(main(base + ["--resume", "--receipt", str(receipt_path),
                                              "--approval-request-id", first["pending_approval"]["request_id"]]), 0)
            self.assertEqual(json.loads(resumed.getvalue())["outcome"], "completed")
            with redirect_stderr(StringIO()):
                self.assertEqual(main(base + ["--resume", "--receipt", str(receipt_path),
                                              "--approval-request-id", first["pending_approval"]["request_id"]]), 1)
        self.assertEqual(len(self.effects), 1)

    def test_main_enforces_separate_arguments_file_and_receipt(self):
        base = ["--operator-config", "operator.json", "--checkpoint", "state.db", "--thread-id", "thread"]
        for suffix in ([], ["--receipt", "receipt.json"], ["--resume"]):
            with self.subTest(suffix=suffix), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main(base + suffix)