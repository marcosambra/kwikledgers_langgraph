from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kwikledgers_fastmcp.domains.local_tracking.request_metrics import RequestMetricsService
from kwikledgers_fastmcp.domains.local_tracking.services import TrackingService
from kwikledgers_langgraph.observability import resolve_ai_tracking_root


def _now() -> datetime:
    return datetime.now(timezone.utc)


def handle_event(
    event: str,
    payload: dict[str, Any],
    *,
    tracking_root: Path,
) -> dict[str, str] | None:
    """Record observable lifecycle events, never prompts, tool inputs or results."""
    if payload.get("stop_hook_active"):
        return None
    session_id = payload.get("session_id") or payload.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Hook de métricas sem session_id; solicitação não registrada.")
    service = RequestMetricsService(tracking_root.parent)
    if event == "UserPromptSubmit":
        request = service.start_request(session_id)
        return {
            "systemMessage": (
                f"Operational request ID: {request['request_id']}. "
                "Use KWIKLEDGERS_REQUEST_ID with this value for graph/CLI correlation. "
                "Record only actions, evidence and concise decision summaries, not private reasoning. "
                "A completed request lifecycle is not proof of task completion."
            )
        }
    request = service.current_request(session_id)
    if request is None:
        raise ValueError("Hook sem solicitação ativa; aguarde o próximo UserPromptSubmit.")
    if event == "Stop":
        service.finish_request(session_id)
        return None

    tool_name = payload.get("tool_name") or payload.get("toolName")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("Hook de ferramenta sem tool_name.")
    call_id = payload.get("tool_use_id") or payload.get("toolUseId") or payload.get("tool_call_id")
    now = _now()
    timestamp = now.isoformat()
    tracking = TrackingService(tracking_root.parent)
    state_dir = tracking_root / "Task_Control" / "request-tool-spans"
    duration: float | None = None
    status = "running" if event == "PreToolUse" else "completed"
    with tracking._filesystem_lock():
        if isinstance(call_id, str) and call_id:
            key = hashlib.sha256(f"{request['request_id']}:{call_id}".encode()).hexdigest()
            state_dir.mkdir(parents=True, exist_ok=True)
            state_path = state_dir / f"{key}.json"
            if event == "PreToolUse":
                tracking._atomic_write(state_path, json.dumps({"started_at": timestamp}))
            elif state_path.exists():
                started = datetime.fromisoformat(json.loads(state_path.read_text(encoding="utf-8"))["started_at"])
                duration = (now - started).total_seconds() * 1000
                if duration < 0:
                    raise ValueError("Relógio retrocedeu; duração da ferramenta não é confiável.")
                state_path.unlink()

    response = payload.get("tool_response")
    if event == "PostToolUse" and (
        payload.get("is_error") is True
        or payload.get("isError") is True
        or isinstance(response, dict) and response.get("isError") is True
    ):
        status = "failed"
    details: dict[str, Any] = {
        "tool_name": tool_name,
        "measurement": "elapsed_wall_time" if duration is not None else "duration_unavailable",
    }
    if isinstance(call_id, str) and call_id:
        details["tool_call_id"] = call_id
    service.append_event(
        request_id=request["request_id"],
        session_id=session_id,
        source="agent",
        kind="step",
        label=f"{event}: {tool_name}",
        status=status,
        duration_ms=duration,
        details=details,
    )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local operational metrics; no Azure mutations.")
    parser.add_argument("--event", required=True, choices=("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"))
    arguments = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Payload de hook deve ser um objeto JSON.")
        output = handle_event(arguments.event, payload, tracking_root=resolve_ai_tracking_root())
        if output:
            print(json.dumps(output, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({"systemMessage": f"Falha no registro de desempenho: {error}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
