from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import patch

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
        build_ports.assert_called_once_with(backend_name="fastmcp", notifications_enabled=False)
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

        build_ports.assert_called_once_with(backend_name="fastmcp", notifications_enabled=True)
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
                exit_code = main(["--backend", "legacy", "--disable-notifications"])

        self.assertEqual(exit_code, 0)
        run_daily_summary_mock.assert_called_once_with(
            source="langgraph-cli",
            notifications_enabled=False,
            backend_name="legacy",
        )


if __name__ == "__main__":
    unittest.main()