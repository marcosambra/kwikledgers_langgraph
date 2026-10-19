from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from kwikledgers_langgraph.backend_factory import BackendSelectionError
from kwikledgers_langgraph.cli import _build_initial_state, main, run_daily_summary

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


class FakeRuntime:
    def __init__(self, *, result: object) -> None:
        self.result = result
        self.invocations: list[dict[str, object]] = []

    def invoke(self, state: dict[str, object]) -> object:
        self.invocations.append(state)
        return self.result


class DailySummaryCliTestCase(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client_patch = patch("kwikledgers_langgraph.cli.FastMCPClient", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def test_build_initial_state_records_backend_name_in_observability(self) -> None:
        state = _build_initial_state("test-daily-summary-cli", True, "fastmcp")

        self.assertEqual(state["observability"]["backend_name"], "fastmcp")
        self.assertEqual(state["observability"]["timeline"][0]["payload"]["backend_name"], "fastmcp")

    def test_run_daily_summary_defaults_to_fastmcp_backend(self) -> None:
        runtime = FakeRuntime(result={"summary": {"counts": {"assigned_items": 1, "blocked_items": 0}}})

        with patch("kwikledgers_langgraph.cli.build_daily_summary_runtime", return_value=runtime):
            with patch("kwikledgers_langgraph.cli.build_daily_summary_ports") as build_ports:
                result = run_daily_summary(source="test-source", notifications_enabled=False)

        self.assertEqual(result["summary"]["counts"]["assigned_items"], 1)
        build_ports.assert_called_once_with(backend_name="fastmcp", notifications_enabled=False,
                                           client=self.client, sprint_path=None, story_id=None)
        self.assertEqual(runtime.invocations[0]["observability"]["backend_name"], "fastmcp")

    def test_run_daily_summary_selects_fastmcp_backend(self) -> None:
        runtime = FakeRuntime(result={"summary": {"counts": {"assigned_items": 1, "blocked_items": 0}}})

        with patch("kwikledgers_langgraph.cli.build_daily_summary_runtime", return_value=runtime):
            with patch("kwikledgers_langgraph.cli.build_daily_summary_ports") as build_ports:
                run_daily_summary(
                    source="test-source",
                    notifications_enabled=True,
                    backend_name="fastmcp",
                )

        build_ports.assert_called_once_with(backend_name="fastmcp", notifications_enabled=True,
                                           client=self.client, sprint_path=None, story_id=None)
        self.assertEqual(runtime.invocations[0]["observability"]["backend_name"], "fastmcp")

    def test_run_daily_summary_rejects_invalid_runtime_result(self) -> None:
        runtime = FakeRuntime(result="invalid")

        with patch("kwikledgers_langgraph.cli.build_daily_summary_runtime", return_value=runtime):
            with patch("kwikledgers_langgraph.cli.build_daily_summary_ports"):
                with self.assertRaises(RuntimeError):
                    run_daily_summary(source="test-source", notifications_enabled=False)

    @unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph nao esta instalado neste ambiente")
    def test_main_accepts_backend_argument(self) -> None:
        result = {"summary": {"counts": {"assigned_items": 0, "blocked_items": 0}}}

        with patch("kwikledgers_langgraph.cli.run_daily_summary", return_value=result) as run_daily_summary_mock:
            with patch("kwikledgers_langgraph.cli._record_runtime_observability"):
                exit_code = main(["--backend", "fastmcp", "--disable-notifications"])

        self.assertEqual(exit_code, 0)
        run_daily_summary_mock.assert_called_once_with(
            source="langgraph-cli",
            notifications_enabled=False,
            backend_name="fastmcp",
            sprint_path=None, story_id=None,
        )

    def test_json_contract_and_selection(self):
        result = {"summary": {"sprint_context": {"path": "Sprint 21"}}}
        with patch("kwikledgers_langgraph.cli.run_daily_summary", return_value=result) as run, \
             patch("kwikledgers_langgraph.cli._record_runtime_observability"), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--json", "--sprint-path", "Sprint 21", "--story-id", "42"]), 0)
        envelope = json.loads(output.getvalue())
        self.assertEqual(envelope["schema_version"], 1)
        self.assertEqual(envelope["operation"], "daily_summary")
        self.assertEqual(envelope["status"], "completed")
        self.assertEqual(envelope["result"], result)
        self.assertEqual(run.call_args.kwargs["sprint_path"], "Sprint 21")
        self.assertEqual(run.call_args.kwargs["story_id"], 42)

    def test_json_errors_are_nonzero_and_never_success_shaped(self):
        from contextlib import redirect_stderr

        with patch("kwikledgers_langgraph.cli.run_daily_summary", side_effect=RuntimeError("secret")), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["--json"]), 1)
        envelope = json.loads(output.getvalue())
        self.assertEqual(envelope["status"], "failed")
        self.assertIsNone(envelope["result"])
        self.assertNotIn("secret", output.getvalue() + errors.getvalue())


if __name__ == "__main__":
    unittest.main()