"""Prepare a signed local debt cache, never create Azure work items.

Exit 0 means successful preparation (preview_only), not approved execution.
Exit 1 means blocked preparation. No human approval key or receipt is needed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from kwikledgers_langgraph.atomic_cli import load_json
from kwikledgers_langgraph.fastmcp_execution_capability import (
    EXECUTION_CAPABILITY_KEY_ENV_VAR, issue_runtime_execution_capability,
)
from kwikledgers_langgraph.graphs.technical_debt_preparation import build_technical_debt_preparation_graph
from kwikledgers_langgraph.lifecycle_adapters import LifecycleJournal, build_debt_preparation_ports
from kwikledgers_langgraph.lifecycle_cli import checkpoint_lock, lifecycle_output


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Prepare a signed local technical-debt preview; never create Azure work items.",
        epilog="Success exits 0 with outcome preview_only. Use kwikledgers-langgraph-lifecycle "
               "technical_debt_execute --preview-key '<runid>:preview_cache' with the same "
               "--checkpoint for a separate, receipt-approved execution.",
    )
    result.add_argument("--operator-config", required=True, type=Path,
                        help="JSON repositories map, authorized iteration_paths list, and optional "
                             "task_acceptance_supported boolean; existing lifecycle settings are allowed.")
    result.add_argument("--arguments-file", required=True, type=Path)
    result.add_argument("--checkpoint", required=True, type=Path, help="Shared signed lifecycle journal.")
    return result


def validate_operator(operator: dict[str, Any]) -> dict[str, Any]:
    repositories = operator.get("repositories")
    if not isinstance(repositories, dict) or not repositories:
        raise ValueError("Operator repositories must be a nonempty map")
    if any(not isinstance(name, str) or not name.strip()
           or not isinstance(path, str) or not path.strip() for name, path in repositories.items()):
        raise ValueError("Operator repository names and paths must be nonempty strings")
    iterations = operator.get("iteration_paths")
    if (not isinstance(iterations, list) or not iterations
            or any(not isinstance(path, str) or not path.strip() for path in iterations)):
        raise ValueError("Operator iteration_paths must be a nonempty list of authorized paths")
    supported = operator.get("task_acceptance_supported", False)
    if type(supported) is not bool:
        raise ValueError("task_acceptance_supported must be a boolean")
    return {"repositories": {name: Path(path) for name, path in repositories.items()},
            "iteration_paths": tuple(iterations), "task_acceptance_supported": supported}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    journal = None
    try:
        state_key = os.environ.get("KWIKLEDGERS_LIFECYCLE_STATE_KEY", "").encode()
        if len(state_key) < 32:
            raise ValueError("Lifecycle state key requires at least 32 bytes")
        if not os.environ.get(EXECUTION_CAPABILITY_KEY_ENV_VAR, "").strip():
            raise ValueError("Runtime execution key required for local tracking")
        operator = validate_operator(load_json(args.operator_config))
        arguments = load_json(args.arguments_file)
        with checkpoint_lock(args.checkpoint):
            journal = LifecycleJournal(args.checkpoint, state_key)
            ports = build_debt_preparation_ports(
                **operator, journal=journal, runtime_issuer=issue_runtime_execution_capability,
            )
            result = build_technical_debt_preparation_graph(ports).invoke({"request": arguments})
            output = lifecycle_output(result)
            if output["outcome"] == "preview_only" and not output["errors"]:
                run_id = result.get("run_id")
                if (not isinstance(run_id, str) or not run_id.strip()
                        or result.get("observability", {}).get("active_run_id") != run_id):
                    raise ValueError("Preparation requires a graph-generated active run")
                preview_key = f"{run_id}:preview_cache"
                if journal.get(preview_key) is None:
                    raise ValueError("Signed prepared preview cache required")
                output["preview_key"] = preview_key
                print(json.dumps(output, ensure_ascii=True, allow_nan=False))
                return 0
            output["outcome"] = "blocked"
            print(json.dumps(output, ensure_ascii=True, allow_nan=False))
            return 1
    except Exception:
        reason = "technical_debt_preparation_failed"
        print(json.dumps({"outcome": "blocked", "outcome_reason": reason, "errors": [reason]}),
              file=sys.stderr)
        return 1
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())