import base64
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from langgraph.checkpoint.memory import MemorySaver

from kwikledgers_langgraph.fastmcp_execution_capability import issue_runtime_execution_capability
from kwikledgers_langgraph.lifecycle_adapters import LifecycleJournal, build_lifecycle_ports
from kwikledgers_langgraph.lifecycle_cli import checkpoint_lock
from kwikledgers_langgraph.lifecycle_runtime import SignedLifecycleCheckpointer, build_technical_debt_execution_graph
from kwikledgers_langgraph.technical_debt_preparation_cli import main, parser, validate_operator


STATE_KEY = "s" * 32
RUNTIME_KEY = "r" * 32
ITERATION = "KL\\2026\\Sprint 20"


class PreparationClient:
    def __init__(self):
        self.calls = []
        self.duplicate = False
        self.fail_similarity = False
        self.fail_tracking = False

    def call(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if name == "find_similar_technical_debts":
            if self.fail_similarity:
                raise OSError("Similarity unavailable")
            return {"repository_name": arguments["repository_name"], "query_title": arguments["title"],
                    "duplicate_found": self.duplicate, "suggested_matches": []}
        if name == "get_my_daily_summary":
            return {"sprint_context": {"path": "KL\\2026\\Sprint 19"}}
        if name == "get_active_user":
            return {"email": "native@example.com"}
        if name == "append_daily_action_log":
            if self.fail_tracking:
                raise OSError("Tracking unavailable")
            if not arguments.get("execution_capability"):
                raise AssertionError("Tracking requires a native runtime capability")
            return {"written": True}
        raise AssertionError(f"Unexpected external capability: {name}")


class TechnicalDebtPreparationCLITests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.tracking_root = self.directory / "AI_Tracking"
        self.tracking_root.mkdir()
        self.checkpoint = self.directory / "journal.db"
        self.operator_path = self.directory / "operator.json"
        self.arguments_path = self.directory / "arguments.json"
        self.operator = {"project": "KL", "area": "Squad", "plans": [],
                         "repositories": {"kl_store": str(self.directory)}, "iteration_paths": [ITERATION]}
        item = {"title": "Fix contract", "context": "Context <script>unsafe</script>",
                "problem": "Problem", "impact": "Impact", "approach": "Approach",
                "acceptance_criteria": "Regression tests pass.", "developer_suggestion": "Check regression.",
                "assigned_to": "native@example.com"}
        self.arguments = {"repository_name": "kl_store", "iteration_path": ITERATION,
                          "parent": dict(item), "children": [dict(item)]}
        self.client = PreparationClient()
        self.issuer = Mock(wraps=issue_runtime_execution_capability)
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"KWIKLEDGERS_LIFECYCLE_STATE_KEY": STATE_KEY,
                                  "KWIKLEDGERS_AI_TRACKING_ROOT": str(self.tracking_root),
                                  "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(self.directory / "capabilities.sqlite3"),
                                  "KWIKLEDGERS_FASTMCP_EXECUTION_KEY": RUNTIME_KEY}, clear=True).start()
        patch("kwikledgers_langgraph.lifecycle_adapters.FastMCPClient", return_value=self.client).start()
        patch("kwikledgers_langgraph.technical_debt_preparation_cli.issue_runtime_execution_capability",
              self.issuer).start()
        self.write_inputs()

    def write_inputs(self):
        self.operator_path.write_text(json.dumps(self.operator), encoding="utf-8")
        self.arguments_path.write_text(json.dumps(self.arguments), encoding="utf-8")

    def invoke(self):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--operator-config", str(self.operator_path),
                         "--arguments-file", str(self.arguments_path), "--checkpoint", str(self.checkpoint)])
        return code, json.loads(stdout.getvalue() or stderr.getvalue())

    def open_journal(self):
        journal = LifecycleJournal(self.checkpoint, STATE_KEY.encode())
        self.addCleanup(journal.close)
        return journal

    def test_child_without_responsible_uses_native_user_in_the_exact_preview(self):
        self.arguments["children"][0].pop("assigned_to")
        self.write_inputs()
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(self.client.calls[0], ("get_active_user", {}))
        self.assertEqual(output["results"]["technical_debt_preview"]["children"][0]["assigned_to"],
                         "native@example.com")

    def test_preparation_signs_cache_and_native_tracking_without_approval_or_azure_mutation(self):
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(output["outcome"], "preview_only")
        self.assertEqual(output["pending_approval"], {})
        self.assertEqual(output["preview_key"], f'{output["run_id"]}:preview_cache')
        self.assertEqual([name for name, _ in self.client.calls],
                         ["find_similar_technical_debts", "append_daily_action_log"])
        lookup = self.client.calls[0][1]
        self.assertEqual(lookup, {"repository_name": "kl_store", "title": "[KL STORE] Fix contract",
                                  "description": self.arguments["parent"]["context"]})
        preview = output["results"]["technical_debt_preview"]
        self.assertIn("&lt;script&gt;", preview["parent"]["description"])
        self.assertNotIn("acceptance_criteria", preview["children"][0])
        journal = self.open_journal()
        cached = journal.get(output["preview_key"])
        self.assertEqual(cached["repository"], "kl_store")
        self.assertEqual(cached["technical_debt_preview"],
                         {field: value for field, value in preview.items() if field != "preview_cache_path"})
        self.assertEqual(preview["preview_cache_path"],
                         str(self.tracking_root / "Repo_Work_Item_Context/technical_debt_previews/kl_store.json"))
        self.assertEqual(json.loads(Path(preview["preview_cache_path"]).read_text(encoding="utf-8")),
                         cached["technical_debt_preview"])
        self.assertEqual(journal.get(f'{output["run_id"]}:similarity')["repository"], "kl_store")
        self.assertEqual(journal.get(f'{output["run_id"]}:prepared:event')["run_id"], output["run_id"])
        issued = self.issuer.call_args
        self.assertEqual(issued.kwargs["action"], "lifecycle_tracking")
        self.assertEqual(issued.kwargs["capability_name"], "append_daily_action_log")
        self.assertEqual(issued.args[0]["observability"]["active_run_id"], output["run_id"])
        self.assertNotIn("verified_lifecycle_binding", issued.args[0])
        encoded, signature = self.client.calls[1][1]["execution_capability"].split(".")
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        self.assertEqual(signature, "local-v1")
        claims = json.loads(payload)
        self.assertEqual(claims["run_id"], output["run_id"])
        self.assertEqual(claims["request_id"], "runtime:lifecycle_tracking")
        self.assertEqual(claims["capability"], "append_daily_action_log")
        native_arguments = {field: value for field, value in self.client.calls[1][1].items()
                            if field != "execution_capability"}
        digest = hashlib.sha256(json.dumps(native_arguments, ensure_ascii=False, sort_keys=True,
                                          allow_nan=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(claims["payload_digest"], digest)

    def test_reopened_cache_is_accepted_by_existing_execution_builder(self):
        code, output = self.invoke()
        self.assertEqual(code, 0)
        journal = self.open_journal()
        approvals, execution_issuer = Mock(), Mock()
        ports = build_lifecycle_ports(repositories={"kl_store": self.directory}, project="KL", area="Squad",
                                      journal=journal, approvals=approvals, client=self.client,
                                      execution_issuer=execution_issuer, runtime_issuer=self.issuer)
        graph = build_technical_debt_execution_graph(ports, checkpointer=SignedLifecycleCheckpointer(journal),
                                                    preview_key=output["preview_key"])
        config = {"configurable": {"thread_id": self.id()}}
        result = graph.invoke({"request": {"repository_name": "kl_store"}}, config)
        self.assertEqual(result["context"], {"repository": "kl_store", "debt_preview_key": output["preview_key"]})
        self.assertTrue(result["__interrupt__"])
        self.assertEqual(result["pending_approval"]["action"], "debt_parent")
        self.assertFalse(result["pending_approval"]["approved"])
        self.assertEqual(result["pending_approval"]["preview"]["title"], "[KL STORE] Fix contract")
        self.assertEqual(graph.get_state(config).next, ("debt_parent_gate",))
        approvals.verify.assert_not_called()
        execution_issuer.assert_not_called()
        self.assertEqual([name for name, _ in self.client.calls],
                         ["find_similar_technical_debts", "append_daily_action_log",
                          "append_daily_action_log", "append_daily_action_log"])

    def test_duplicate_is_successful_preparation_but_execution_builder_rejects_cache(self):
        self.client.duplicate = True
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(output["results"]["technical_debt_preview"]["creation_blocked"])
        ports = build_lifecycle_ports(repositories={"kl_store": self.directory}, project="KL", area="Squad",
                                      journal=self.open_journal(), approvals=Mock(), client=self.client)
        with self.assertRaisesRegex(ValueError, "Exact-match"):
            build_technical_debt_execution_graph(ports, checkpointer=MemorySaver(), preview_key=output["preview_key"])

    def test_operator_task_acceptance_support_not_untrusted_request_controls_preview(self):
        self.operator["task_acceptance_supported"] = True
        self.arguments["task_acceptance_supported"] = False
        self.write_inputs()
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("acceptance_criteria", output["results"]["technical_debt_preview"]["children"][0])

    def test_obsolete_state_key_does_not_change_the_local_preparation_contract(self):
        for name, value in (("KWIKLEDGERS_LIFECYCLE_STATE_KEY", "s" * 31),):
            with self.subTest(name=name, value=value), patch.dict("os.environ", {name: value}):
                code, output = self.invoke()
                self.assertEqual((code, output["outcome"]), (0, "preview_only"))
                self.assertTrue(self.checkpoint.exists())
        self.assertEqual([name for name, _ in self.client.calls],
                         ["find_similar_technical_debts", "append_daily_action_log"])
        self.issuer.assert_called_once()

    def test_preparation_without_configured_keys_uses_the_runtime_issuer_contract(self):
        self.issuer.side_effect = lambda *args, **kwargs: "local-runtime-capability"
        with patch.dict("os.environ", {"KWIKLEDGERS_AI_TRACKING_ROOT": str(self.tracking_root)}, clear=True):
            code, output = self.invoke()
        self.assertEqual((code, output["outcome"]), (0, "preview_only"))
        journal = LifecycleJournal(self.checkpoint)
        self.addCleanup(journal.close)
        self.assertEqual(journal.get(output["preview_key"])["repository"], "kl_store")
        self.issuer.assert_called_once()

    def test_next_sprint_uses_native_context_and_requires_authorized_main_sprint(self):
        self.arguments.pop("iteration_path")
        self.arguments["target_iteration"] = "next_sprint"
        self.write_inputs()
        with patch("kwikledgers_langgraph.lifecycle_adapters.UTCClock.now",
                   return_value="2026-10-17T12:00:00+00:00"):
            code, output = self.invoke()
        self.assertEqual((code, output["outcome"]), (0, "preview_only"))
        self.assertEqual(output["results"]["technical_debt_preview"]["parent"]["iteration_path"], ITERATION)
        self.assertEqual(self.client.calls[0], ("get_my_daily_summary", {}))
        self.operator["iteration_paths"] = [ITERATION + "\\Sustentacao"]
        self.write_inputs()
        with patch("kwikledgers_langgraph.lifecycle_adapters.UTCClock.now",
                   return_value="2026-10-17T12:00:00+00:00"):
            code, output = self.invoke()
        self.assertEqual((code, output["outcome"]), (1, "blocked"))

    def test_unauthorized_repository_iteration_and_next_sprint_fail_before_native_reads(self):
        for changes in ({"repository_name": "other"}, {"iteration_path": "Other"},
                        {"target_iteration": "next_sprint"}):
            with self.subTest(changes=changes):
                original = deepcopy(self.arguments)
                self.arguments.update(changes)
                self.write_inputs()
                code, output = self.invoke()
                self.assertEqual((code, output["outcome"]), (1, "blocked"))
                self.assertNotIn("preview_key", output)
                self.arguments = original
        self.assertEqual(self.client.calls, [])

    def test_native_and_tracking_failures_never_report_preparation_success(self):
        for failure in ("fail_similarity", "fail_tracking"):
            with self.subTest(failure=failure):
                setattr(self.client, failure, True)
                code, output = self.invoke()
                self.assertEqual((code, output["outcome"]), (1, "blocked"))
                self.assertNotIn("preview_key", output)
                setattr(self.client, failure, False)
        self.assertTrue(all(name in {"find_similar_technical_debts", "append_daily_action_log"}
                            for name, _ in self.client.calls))

    def test_cache_tampering_is_rejected_by_journal_and_execution_builder(self):
        _, output = self.invoke()
        journal = self.open_journal()
        with journal.db:
            journal.db.execute("UPDATE lifecycle_evidence SET payload = ? WHERE key = ?",
                               ('{"repository":"other"}', output["preview_key"]))
        with self.assertRaisesRegex(ValueError, "corrompida"):
            journal.get(output["preview_key"])
        ports = build_lifecycle_ports(repositories={"kl_store": self.directory}, project="KL", area="Squad",
                                      journal=journal, approvals=Mock(), client=self.client)
        with self.assertRaisesRegex(ValueError, "corrompida"):
            build_technical_debt_execution_graph(ports, checkpointer=MemorySaver(), preview_key=output["preview_key"])

    def test_lock_contention_blocks_before_native_reads(self):
        with checkpoint_lock(self.checkpoint):
            code, output = self.invoke()
        self.assertEqual((code, output["outcome"]), (1, "blocked"))
        self.assertEqual(self.client.calls, [])

    def test_strict_json_loader_rejects_duplicate_nonfinite_and_nonobject_inputs(self):
        for path in (self.operator_path, self.arguments_path):
            for content in ('{"repository_name":"kl_store","repository_name":"other"}',
                            '{"value":NaN}', '[]'):
                with self.subTest(path=path.name, content=content):
                    self.write_inputs()
                    path.write_text(content, encoding="utf-8")
                    code, output = self.invoke()
                    self.assertEqual((code, output["outcome"]), (1, "blocked"))
        self.assertEqual(self.client.calls, [])

    def test_operator_validation_preserves_only_preparation_settings(self):
        policy = validate_operator(self.operator)
        self.assertEqual(policy, {"repositories": {"kl_store": self.directory},
                                  "iteration_paths": (ITERATION,), "task_acceptance_supported": False})
        for changes in ({"repositories": []}, {"repositories": {}}, {"repositories": {"kl_store": 1}},
                        {"repositories": {"": "path"}}, {"iteration_paths": ITERATION},
                        {"iteration_paths": []}, {"iteration_paths": [1]},
                        {"iteration_paths": [" "]}, {"task_acceptance_supported": "false"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_operator({**self.operator, **changes})

    def test_cli_requires_files_and_has_no_approval_or_mutation_switch(self):
        base = ["--operator-config", "operator.json", "--arguments-file", "arguments.json",
                "--checkpoint", "journal.db"]
        self.assertEqual(parser().parse_args(base).checkpoint, Path("journal.db"))
        with redirect_stderr(StringIO()):
            for flag in ("--receipt", "--approve-create", "--resume"):
                with self.subTest(flag=flag), self.assertRaises(SystemExit):
                    parser().parse_args(base + [flag])
            with self.assertRaises(SystemExit):
                parser().parse_args([])