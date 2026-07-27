from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Any

from kwikledgers_langgraph.state import AgentState, append_decision, merge_observability


_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _is_truthy_env(value: str | None) -> bool:
    return bool(value and value.strip().lower() in _TRUTHY_ENV_VALUES)


def _resolve_langsmith_api_key() -> str | None:
    return os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")


def _resolve_langsmith_project_name() -> str:
    return (
        os.getenv("KWIKLEDGERS_LANGSMITH_PROJECT")
        or os.getenv("LANGSMITH_PROJECT")
        or "kwikledgers-langgraph"
    )


def _resolve_langsmith_api_url() -> str | None:
    return os.getenv("LANGSMITH_ENDPOINT") or os.getenv("LANGCHAIN_ENDPOINT")


def resolve_langsmith_tracing_settings() -> dict[str, Any]:
    tracing_requested = any(
        (
            _is_truthy_env(os.getenv("KWIKLEDGERS_LANGSMITH_ENABLED")),
            _is_truthy_env(os.getenv("LANGSMITH_TRACING")),
            _is_truthy_env(os.getenv("LANGSMITH_TRACING_V2")),
        )
    )
    api_key = _resolve_langsmith_api_key()
    enabled = tracing_requested and bool(api_key)
    if enabled:
        reason = "enabled"
    elif tracing_requested:
        reason = "missing_api_key"
    else:
        reason = "disabled_by_env"
    return {
        "enabled": enabled,
        "project_name": _resolve_langsmith_project_name(),
        "api_url": _resolve_langsmith_api_url(),
        "api_key": api_key,
        "api_key_configured": bool(api_key),
        "reason": reason,
    }


def _build_external_tracing_context() -> dict[str, Any]:
    settings = resolve_langsmith_tracing_settings()
    return {
        "provider": "langsmith",
        "enabled": settings["enabled"],
        "project_name": settings["project_name"],
        "api_key_configured": settings["api_key_configured"],
        "reason": settings["reason"],
    }


def _load_langsmith_runtime() -> tuple[Any, Any]:
    from langsmith import Client, trace

    return Client, trace


def _build_langsmith_tags(state: AgentState) -> list[str]:
    observability = dict(state.get("observability", {}))
    flow_name = observability.get("flow_name", "runtime")
    source = observability.get("source") or state.get("source") or "unknown"
    tags = {
        "kwikledgers",
        "langgraph",
        f"flow:{flow_name}",
        f"source:{source}",
    }
    return sorted(tags)


def _build_langsmith_metadata(state: AgentState) -> dict[str, Any]:
    observability = dict(state.get("observability", {}))
    metadata: dict[str, Any] = {
        "kwikledgers_flow_name": observability.get("flow_name"),
        "kwikledgers_source": observability.get("source") or state.get("source"),
        "kwikledgers_correlation_id": observability.get("correlation_id"),
        "kwikledgers_active_run_id": observability.get("active_run_id"),
    }
    pull_request_request = dict(state.get("pull_request_request", {}))
    if pull_request_request:
        metadata["kwikledgers_story_id"] = pull_request_request.get("story_id")
        metadata["kwikledgers_repository_name"] = pull_request_request.get("repository_name")
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def _build_langsmith_inputs(state: AgentState) -> dict[str, Any]:
    observability = dict(state.get("observability", {}))
    inputs: dict[str, Any] = {
        "flow_name": observability.get("flow_name"),
        "source": observability.get("source") or state.get("source"),
    }
    pull_request_request = dict(state.get("pull_request_request", {}))
    if pull_request_request:
        inputs["pull_request_request"] = {
            key: pull_request_request[key]
            for key in ("story_id", "repository_name", "source_branch")
            if key in pull_request_request
        }
    return {key: value for key, value in inputs.items() if value not in (None, "")}


def build_langsmith_trace_outputs(state: AgentState) -> dict[str, Any]:
    outputs: dict[str, Any] = {
        "errors_count": len(state.get("errors", [])),
    }
    summary = dict(state.get("summary", {}))
    counts = dict(summary.get("counts", {}))
    if counts:
        outputs["summary_counts"] = counts
    pull_request_result = dict(state.get("pull_request_result", {}))
    if pull_request_result:
        outputs["pull_request_result"] = {
            key: pull_request_result[key]
            for key in ("status", "pull_request_id", "url")
            if key in pull_request_result
        }
    elif state.get("pending_approval"):
        outputs["pending_approval_action"] = dict(state.get("pending_approval", {})).get("action")
    return outputs


@contextmanager
def trace_runtime_execution(state: AgentState):
    settings = resolve_langsmith_tracing_settings()
    if not settings["enabled"]:
        yield None
        return

    try:
        client_class, trace = _load_langsmith_runtime()
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "LangSmith foi habilitado, mas a dependencia 'langsmith' nao esta instalada neste ambiente. "
            "Execute 'poetry install' no repositorio 'kwikledgers_langgraph'."
        ) from error

    client_kwargs: dict[str, Any] = {"api_key": settings["api_key"]}
    if settings["api_url"]:
        client_kwargs["api_url"] = settings["api_url"]
    client = client_class(**client_kwargs)

    observability = dict(state.get("observability", {}))
    flow_name = observability.get("flow_name", "runtime")
    with trace(
        name=f"kwikledgers_langgraph.{flow_name}",
        run_type="chain",
        inputs=_build_langsmith_inputs(state),
        project_name=settings["project_name"],
        tags=_build_langsmith_tags(state),
        metadata=_build_langsmith_metadata(state),
        client=client,
    ) as run:
        yield run


def build_observability_context(*, flow_name: str, source: str) -> dict[str, Any]:
    correlation_id = str(uuid.uuid4())
    active_run_id = str(uuid.uuid4())
    return {
        "correlation_id": correlation_id,
        "active_run_id": active_run_id,
        "flow_name": flow_name,
        "source": source,
        "external_tracing": _build_external_tracing_context(),
        "decisions": [],
        "timeline": [
            {
                "node": flow_name,
                "status": "initialized",
                "message": "Fluxo preparado para execução.",
                "payload": {"source": source},
            }
        ],
    }


def record_runtime_event(
    state: AgentState,
    *,
    node: str,
    status: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observability = dict(state.get("observability", {}))
    timeline = list(observability.get("timeline", []))
    timeline.append(
        {
            "node": node,
            "status": status,
            "message": message,
            "payload": payload or {},
        }
    )
    observability["timeline"] = timeline
    decision = append_decision(state, f"runtime.{node}.{status}")
    return {**decision, **merge_observability(state, observability)}