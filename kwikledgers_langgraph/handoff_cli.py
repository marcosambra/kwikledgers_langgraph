from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from kwikledgers_langgraph.handoff import create_handoff, transition_handoff
from kwikledgers_langgraph.observability import resolve_ai_tracking_root
from kwikledgers_fastmcp.domains.local_tracking.services import resolve_workspace_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Handoff local; nunca autoriza commits ou operações Azure.")
    parser.add_argument("--tracking-root", type=Path)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--story-id", type=int)
    parser.add_argument("--task-ids", type=int, nargs="*", default=[])
    parser.add_argument("--sprint-path", default="")
    parser.add_argument("--handoff-id")
    parser.add_argument("--status", choices=["running", "blocked", "failed", "completed"])
    parser.add_argument("--session-id", default="")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--tests-passed", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.create:
            if args.handoff_id or args.status or args.session_id or args.tests_passed:
                raise ValueError("Criação e transição são operações separadas.")
            root = args.tracking_root or Path(os.environ.get("KWIKLEDGERS_AI_TRACKING_ROOT") or
                                              resolve_workspace_root() / "AI_Tracking")
            root = root.expanduser().resolve()
            result = create_handoff(root, story_id=args.story_id, task_ids=args.task_ids, sprint_path=args.sprint_path)
        else:
            root = resolve_ai_tracking_root(args.tracking_root)
            if not args.handoff_id or not args.status:
                raise ValueError("Informe o handoff e o estado desejado.")
            result = transition_handoff(root, args.handoff_id, args.status, session_id=args.session_id,
                                        evidence=args.evidence, tests_passed=args.tests_passed)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Falha no handoff: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
