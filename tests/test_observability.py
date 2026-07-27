from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from kwikledgers_langgraph.observability import (
    build_langsmith_trace_outputs,
    build_observability_context,
    record_runtime_event,
    resolve_langsmith_tracing_settings,
    trace_runtime_execution,
)


class ObservabilityTestCase(unittest.TestCase):
    def test_build_observability_context_initializes_timeline(self) -> None:
        context = build_observability_context(
            flow_name="daily_summary",
            source="test",
        )

        self.assertIn("correlation_id", context)
        self.assertIn("active_run_id", context)
        self.assertEqual(context["flow_name"], "daily_summary")
        self.assertEqual(context["timeline"][0]["status"], "initialized")
        self.assertEqual(context["external_tracing"]["provider"], "langsmith")

    def test_resolve_langsmith_tracing_settings_defaults_to_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = resolve_langsmith_tracing_settings()

        self.assertFalse(settings["enabled"])
        self.assertFalse(settings["api_key_configured"])
        self.assertEqual(settings["reason"], "disabled_by_env")
        self.assertEqual(settings["project_name"], "kwikledgers-langgraph")

    def test_resolve_langsmith_tracing_settings_enables_without_exposing_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "KWIKLEDGERS_LANGSMITH_ENABLED": "true",
                "KWIKLEDGERS_LANGSMITH_PROJECT": "kwikledgers-dev-runtime",
                "LANGSMITH_API_KEY": "secret-key",
            },
            clear=True,
        ):
            settings = resolve_langsmith_tracing_settings()

        self.assertTrue(settings["enabled"])
        self.assertTrue(settings["api_key_configured"])
        self.assertEqual(settings["project_name"], "kwikledgers-dev-runtime")
        self.assertEqual(settings["reason"], "enabled")
        self.assertNotIn("secret-key", str({k: v for k, v in settings.items() if k != "api_key"}))

    def test_record_runtime_event_appends_timeline_entry(self) -> None:
        state = {
            "observability": build_observability_context(
                flow_name="daily_summary",
                source="test",
            )
        }

        result = record_runtime_event(
            state,
            node="fetch_summary",
            status="completed",
            message="Resumo diário carregado.",
            payload={"counts": {"assigned_items": 3}},
        )

        self.assertIn("observability", result)
        timeline = result["observability"]["timeline"]
        self.assertEqual(timeline[-1]["node"], "fetch_summary")
        self.assertEqual(timeline[-1]["status"], "completed")
        self.assertEqual(timeline[-1]["payload"]["counts"]["assigned_items"], 3)

    def test_trace_runtime_execution_returns_none_when_langsmith_is_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            state = {
                "source": "test",
                "observability": build_observability_context(
                    flow_name="daily_summary",
                    source="test",
                ),
            }
            with trace_runtime_execution(state) as run:
                self.assertIsNone(run)

    def test_trace_runtime_execution_uses_langsmith_when_enabled(self) -> None:
        captured: dict[str, object] = {}

        class FakeClient:
            def __init__(self, **kwargs: object) -> None:
                captured["client_kwargs"] = kwargs

        @contextmanager
        def fake_trace(**kwargs: object):
            captured["trace_kwargs"] = kwargs

            class FakeRun:
                def end(self, outputs: dict[str, object] | None = None) -> None:
                    captured["outputs"] = outputs

            yield FakeRun()

        state = {
            "source": "story-pr-cli",
            "pull_request_request": {
                "story_id": 120204,
                "repository_name": "accounting",
                "source_branch": "feature/KL-120204-balance-sheet-by-class",
            },
            "observability": build_observability_context(
                flow_name="story_pull_request",
                source="story-pr-cli",
            ),
        }
        with patch.dict(
            os.environ,
            {
                "KWIKLEDGERS_LANGSMITH_ENABLED": "true",
                "LANGSMITH_PROJECT": "kwikledgers-langgraph-local",
                "LANGSMITH_API_KEY": "secret-key",
            },
            clear=True,
        ):
            with patch(
                "kwikledgers_langgraph.observability._load_langsmith_runtime",
                return_value=(FakeClient, fake_trace),
            ):
                with trace_runtime_execution(state) as run:
                    self.assertIsNotNone(run)
                    run.end(outputs=build_langsmith_trace_outputs({"errors": []}))

        self.assertEqual(captured["client_kwargs"], {"api_key": "secret-key"})
        self.assertEqual(captured["trace_kwargs"]["project_name"], "kwikledgers-langgraph-local")
        self.assertEqual(captured["trace_kwargs"]["name"], "kwikledgers_langgraph.story_pull_request")
        self.assertEqual(captured["trace_kwargs"]["inputs"]["pull_request_request"]["story_id"], 120204)
        self.assertIn("flow:story_pull_request", captured["trace_kwargs"]["tags"])
        self.assertEqual(captured["outputs"], {"errors_count": 0})


if __name__ == "__main__":
    unittest.main()