from unittest import TestCase
from unittest.mock import Mock

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.graphs.lifecycle_engine import LifecycleStep, build_lifecycle_graph
from kwikledgers_langgraph.lifecycle_cli import lifecycle_exit_code, run_cli


class LifecycleEngineTests(TestCase):
    def setUp(self):
        self.context = Mock()
        self.context.resolve.return_value = {
            "story_id": 1, "task_id": 2, "child_ids": [2], "repository": "payment",
        }
        self.tracking = Mock()
        self.approvals = Mock()
        self.approvals.verify.return_value = False
        clock = Mock()
        clock.now.return_value = "2026-09-29T09:00:00+00:00"
        self.ports = LifecyclePorts(self.context, self.tracking, self.approvals, clock)
        self.execute = Mock(return_value={"ok": True})
        self.graph = build_lifecycle_graph(
            self.ports, (LifecycleStep("mutation", lambda state: {"id": 2}, self.execute),),
            name="test", checkpointer=MemorySaver(),
        )
        self.config = {"configurable": {"thread_id": self.id()}}

    def test_initial_approval_and_results_cannot_skip_preview(self):
        result = self.graph.invoke({"request": {}, "pending_approval": {"approved": True},
                                    "results": {"mutation": {"ok": True}}}, self.config)
        self.assertTrue(result["__interrupt__"])
        self.assertFalse(result["pending_approval"]["approved"])
        self.assertEqual(result["results"], {})
        self.execute.assert_not_called()

    def test_unverified_resume_denied(self):
        self.graph.invoke({"request": {}}, self.config)
        result = self.graph.invoke(Command(resume={"approved": True}), self.config)
        self.assertEqual(result["outcome"], "denied")
        self.execute.assert_not_called()

    def test_verified_resume_executes_exact_preview(self):
        first = self.graph.invoke({"request": {}}, self.config)
        self.approvals.verify.return_value = True
        result = self.graph.invoke(Command(resume="authenticated-receipt"), self.config)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(self.execute.call_args.args[1], {"id": 2})
        self.assertEqual(self.approvals.verify.call_args.args[0], first["pending_approval"])

    def test_tracking_failure_blocks_mutation(self):
        self.tracking.record.side_effect = OSError("disk full")
        result = self.graph.invoke({"request": {}}, self.config)
        self.assertEqual(result["outcome"], "blocked")
        self.assertIn("test: pre_execution_failed", result["errors"])
        self.assertIn("test: tracking_unavailable", result["errors"])
        self.execute.assert_not_called()

    def test_result_and_pr_url_survive_executed_tracking_failure(self):
        self.execute.return_value = {"url": "https://example.test/pr/1"}
        graph = build_lifecycle_graph(
            self.ports, (LifecycleStep("draft_pr", lambda state: {}, self.execute),
                         LifecycleStep("next", lambda state: {}, Mock())),
            name="test", checkpointer=MemorySaver(),
        )
        graph.invoke({"request": {}}, self.config)
        self.approvals.verify.return_value = True

        def record(payload, **kwargs):
            if payload["phase"] == "executed":
                raise OSError("secret-token")

        self.tracking.record.side_effect = record
        result = graph.invoke(Command(resume="receipt"), self.config)
        self.assertEqual(result["results"]["draft_pr"], self.execute.return_value)
        self.assertEqual(result["direct_pr_url"], self.execute.return_value["url"])
        self.assertEqual(result["outcome"], "reconciliation_required")
        self.assertEqual(result["outcome_reason"], "post_execution_evidence_failed")
        self.assertEqual(result["pending_approval"], {})
        saved = graph.get_state(self.config)
        self.assertEqual(saved.next, ())
        self.assertEqual(saved.values["results"], result["results"])
        self.assertEqual(saved.values["direct_pr_url"], result["direct_pr_url"])
        self.assertNotIn("secret-token", str(result["errors"]))
        self.execute.assert_called_once()

    def test_completion_tracking_failure_preserves_result(self):
        self.graph.invoke({"request": {}}, self.config)
        self.approvals.verify.return_value = True

        def record(payload, **kwargs):
            if payload["phase"] == "completed":
                raise OSError("secret-token")

        self.tracking.record.side_effect = record
        result = self.graph.invoke(Command(resume="receipt"), self.config)
        self.assertEqual(result["outcome"], "reconciliation_required")
        self.assertEqual(result["outcome_reason"], "completion_tracking_failed")
        self.assertEqual(result["results"], {"mutation": {"ok": True}})
        self.assertEqual(lifecycle_exit_code(result), 3)
        self.assertNotIn("secret-token", str(result["errors"]))
        self.execute.assert_called_once()

    def test_execution_failure_classification_and_safe_reasons(self):
        cases = (
            (RuntimeError("Execution outcome unknown; reconcile before retry"), "reconciliation_required"),
            (RuntimeError("Execution already reserved; reconcile before retry"), "reconciliation_required"),
            (TimeoutError("secret-token"), "reconciliation_required"),
            (RuntimeError("secret-token"), "blocked"),
            (ValueError("secret-token"), "blocked"),
            (PermissionError("secret-token"), "blocked"),
        )
        for index, (error, outcome) in enumerate(cases):
            with self.subTest(error=type(error).__name__, outcome=outcome):
                config = {"configurable": {"thread_id": f"{self.id()}:{index}"}}
                self.graph.invoke({"request": {}}, config)
                self.approvals.verify.return_value = True
                self.execute.side_effect = error
                self.execute.reset_mock()
                result = self.graph.invoke(Command(resume="receipt"), config)
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["results"], {})
                self.assertNotIn("secret-token", str(result["errors"]))
                self.assertEqual(self.graph.get_state(config).next, ())
                self.execute.assert_called_once()

    def test_execution_started_tracking_failure_does_not_attempt_mutation(self):
        self.graph.invoke({"request": {}}, self.config)
        self.approvals.verify.return_value = True

        def record(payload, **kwargs):
            if payload["phase"] == "execution_started":
                raise TimeoutError("secret-token")

        self.tracking.record.side_effect = record
        result = self.graph.invoke(Command(resume="receipt"), self.config)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["outcome_reason"], "pre_execution_failed")
        self.execute.assert_not_called()

    def test_cli_waits_then_exposes_completed_results(self):
        first = run_cli(self.graph, thread_id=self.id())
        self.assertEqual(first["outcome"], "waiting")
        self.assertEqual(lifecycle_exit_code(first), 2)
        self.approvals.verify.return_value = True
        result = run_cli(self.graph, thread_id=self.id(), resume=True,
                 approval_request_id=first["pending_approval"]["request_id"])
        self.assertEqual(result["results"], {"mutation": {"ok": True}})
        self.assertEqual(lifecycle_exit_code(result), 0)
        with self.assertRaises(ValueError):
            run_cli(self.graph, thread_id=self.id())
        self.execute.assert_called_once()

    def test_cli_exit_codes_are_terminal_outcome_specific(self):
        for outcome, expected in (("completed", 0), ("blocked", 1), ("denied", 1),
                                  ("running", 2), ("waiting", 2), ("interrupted", 2),
                                  ("reconciliation_required", 3)):
            with self.subTest(outcome=outcome):
                self.assertEqual(lifecycle_exit_code({"outcome": outcome}), expected)

    def test_cli_unknown_execution_is_terminal_without_retry(self):
        first = run_cli(self.graph, thread_id=self.id())
        self.approvals.verify.return_value = True
        self.execute.side_effect = TimeoutError("secret-token")
        result = run_cli(self.graph, thread_id=self.id(), resume=True,
                 approval_request_id=first["pending_approval"]["request_id"])
        self.assertEqual(result["outcome"], "reconciliation_required")
        self.assertEqual(result["outcome_reason"], "execution_outcome_unknown")
        self.assertEqual(lifecycle_exit_code(result), 3)
        with self.assertRaises(ValueError):
                run_cli(self.graph, thread_id=self.id(), resume=True,
                    approval_request_id=first["pending_approval"]["request_id"])
        self.execute.assert_called_once()

    def test_requires_checkpointer(self):
        with self.assertRaises(ValueError):
            build_lifecycle_graph(self.ports, (), name="test", checkpointer=None)