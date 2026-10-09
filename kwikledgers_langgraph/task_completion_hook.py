from __future__ import annotations

import contextlib
import argparse
import hashlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from kwikledgers_langgraph.day_end_closure_cli import main as close_task
from kwikledgers_langgraph.day_end_closure_policy import parse_daily_log_entries
from kwikledgers_langgraph.observability import resolve_ai_tracking_root


def completed_task_ids(entries: list[dict[str, str]], now: datetime, session_id: str) -> list[int]:
    task_ids: set[int] = set()
    for entry in entries:
        if entry.get("source") != "task-completed":
            continue
        try:
            evidence: dict[str, Any] = json.loads(entry.get("details", ""))
            timestamp = datetime.fromisoformat(entry["timestamp"])
            task_id = evidence["task_id"]
            if (
                type(task_id) is int and task_id > 0
                and bool(session_id) and evidence.get("session_id") == session_id
                and evidence.get("completed") is True
                and evidence.get("tests_passed") is True
                and timestamp <= now
                and timestamp.astimezone(now.tzinfo).date() == now.date()
            ):
                task_ids.add(task_id)
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(task_ids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", choices=("SessionStart", "UserPromptSubmit", "Stop"), default="Stop")
    arguments = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("stop_hook_active"):
            return 0
        session_id = payload.get("session_id") or payload.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            return 0
        root = resolve_ai_tracking_root()
        sessions = root / "Task_Control" / "task-completion-sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        session_path = sessions / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")
        session = json.loads(session_path.read_text(encoding="utf-8")) if session_path.exists() else {"local_only": False}
        if arguments.event != "Stop":
            prompt = str(payload.get("prompt", "")).casefold()
            session["local_only"] = bool(session.get("local_only")) or any(marker in prompt for marker in ("[kwikledgers-local-only]", "somente local", "apenas local"))
            session_path.write_text(json.dumps(session), encoding="utf-8")
            message = f"Session ID for task-completed evidence: {session_id}."
            if session["local_only"]:
                message += " Azure task completion hook is disabled for this local-only session."
            print(json.dumps({"systemMessage": message}))
            return 0
        if not session_path.exists() or session.get("local_only"):
            return 0
        now = datetime.now(ZoneInfo("America/Sao_Paulo"))
        entries: list[dict[str, str]] = []
        for stamp in {(now + timedelta(days=offset)).astimezone(timezone.utc).date().isoformat() for offset in (-1, 0, 1)}:
            log = root / "Daily_Action_Logs" / f"{stamp}.md"
            if log.is_file():
                entries.extend(parse_daily_log_entries(log.read_text(encoding="utf-8")))
        results: list[str] = []
        for task_id in completed_task_ids(entries, now, session_id):
            journal = root / "Task_Control" / "day-end-closures" / f"{task_id}.json"
            if journal.exists():
                if json.loads(journal.read_text(encoding="utf-8")).get("phase") != "completed":
                    raise RuntimeError("Fechamento incompleto; reconcilie antes de continuar.")
                continue
            output = io.StringIO()
            completed_at = max(
                entry["timestamp"] for entry in entries
                if completed_task_ids([entry], now, session_id) == [task_id]
            )
            with contextlib.redirect_stdout(output):
                exit_code = close_task([
                    "--trigger", "completed", "--task-id", str(task_id),
                    "--completed-at", completed_at,
                    "--timezone", "America/Sao_Paulo", "--notify",
                ])
            results.append(f"Task {task_id}: {output.getvalue().strip()}")
            if exit_code:
                results.append("O fechamento exige verificacao; nao repita o lancamento automaticamente.")
        if results:
            print(json.dumps({"systemMessage": "\n".join(results)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"systemMessage": f"Hook de conclusao nao executado: {error}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())