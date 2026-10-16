from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from langgraph.types import Command

from kwikledgers_langgraph.atomic_runtime import (
    MUTATION_SCHEMAS, PREVIEW_SCHEMAS, READ_SCHEMAS, build_atomic_runtime,
    read_capability, validate_arguments,
)
from kwikledgers_langgraph.fastmcp_client import FastMCPClient
from kwikledgers_langgraph.lifecycle_adapters import ExplicitLocalApproval, LifecycleJournal, build_lifecycle_ports
from kwikledgers_langgraph.lifecycle_cli import (
    checkpoint_lock, lifecycle_confirmation, lifecycle_exit_code, lifecycle_issuers, lifecycle_output, record_cli_metrics,
)
from kwikledgers_langgraph.lifecycle_runtime import SignedLifecycleCheckpointer


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Operational reads and explicitly approved atomic lifecycles.")
    commands = result.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read")
    read.add_argument("name", choices=tuple(READ_SCHEMAS) + tuple(PREVIEW_SCHEMAS))
    read.add_argument("--arguments-file", required=True, type=Path)
    mutation = commands.add_parser("mutation")
    mutation.add_argument("name", choices=tuple(MUTATION_SCHEMAS))
    mutation.add_argument("--arguments-file", type=Path)
    mutation.add_argument("--operator-config", required=True, type=Path)
    mutation.add_argument("--checkpoint", required=True, type=Path)
    mutation.add_argument("--thread-id", required=True)
    mutation.add_argument("--resume", action="store_true")
    approval = mutation.add_mutually_exclusive_group()
    mutation.set_defaults(receipt=None)
    approval.add_argument("--approval-request-id", help="Explicitly approve the current preview on a separate resume.")
    return result


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field, value in pairs:
        if field in result:
            raise ValueError("Duplicate JSON field")
        result[field] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError("Nonfinite JSON numbers are not allowed")


def load_json(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(result, dict):
        raise ValueError("JSON file must contain an object")
    return result


def run_atomic_cli(graph: Any, *, name: str, thread_id: str,
                   arguments: dict[str, Any], resume: bool = False, receipt: Any = None,
                   approval_request_id: str | None = None) -> dict[str, Any]:
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Nonempty atomic thread required")
    if receipt is not None:
        raise ValueError("Receipts were removed; use --approval-request-id")
    payload = validate_arguments(name, arguments)
    config = {"configurable": {"thread_id": thread_id}}
    saved = graph.get_state(config)
    if resume:
        if not saved.values or not saved.next:
            raise ValueError("Existing interrupted atomic lifecycle required")
        state = saved.values
        context = state.get("context", {})
        pending = state.get("pending_approval", {})
        action = f"atomic_{name}"
        if (state.get("observability", {}).get("flow_name") != action
                or context.get("atomic_capability") != name
                or context.get("atomic_arguments") != payload
                or pending.get("action") != action):
            raise ValueError("Resume cannot change atomic capability, arguments or flow")
        confirmation = lifecycle_confirmation(state, receipt=receipt, approval_request_id=approval_request_id)
        result = graph.invoke(Command(resume=confirmation), config)
    else:
        if receipt is not None or approval_request_id is not None:
            raise ValueError("Approval cannot be supplied in the preview invocation")
        if saved.values:
            raise ValueError("Thread already exists; resume without resetting evidence")
        result = graph.invoke({"request": {}}, config)
    return lifecycle_output(result)


def main(argv: list[str] | None = None) -> int:
    arguments_parser = parser()
    args = arguments_parser.parse_args(argv)
    if args.command == "mutation":
        if (args.receipt is not None or args.approval_request_id is not None) and not args.resume:
            arguments_parser.error("Approval requires a separate --resume invocation")
        if args.resume and args.receipt is None and args.approval_request_id is None:
            arguments_parser.error("--resume requires --approval-request-id")
        if not args.resume and not args.arguments_file:
            arguments_parser.error("preview requires --arguments-file")
    journal = None
    try:
        if args.command == "read":
            output = read_capability(FastMCPClient(), args.name, load_json(args.arguments_file))
            print(json.dumps(output, ensure_ascii=True, allow_nan=False))
            return 0
        execution_issuer, runtime_issuer = lifecycle_issuers()
        operator = load_json(args.operator_config)
        approvals = ExplicitLocalApproval()
        with checkpoint_lock(args.checkpoint):
            journal = LifecycleJournal(args.checkpoint)
            checkpoint = SignedLifecycleCheckpointer(journal)
            config = {"configurable": {"thread_id": args.thread_id}}
            previous = checkpoint.get(config)
            saved = previous.get("channel_values", {}) if previous else {}
            action = f"atomic_{args.name}"
            if saved and saved.get("observability", {}).get("flow_name") != action:
                raise ValueError("Thread belongs to another lifecycle")
            if args.resume and not saved:
                raise ValueError("Existing interrupted atomic lifecycle required")
            payload = (load_json(args.arguments_file) if args.arguments_file else
                       saved.get("context", {}).get("atomic_arguments"))
            payload = validate_arguments(args.name, payload)
            ports = build_lifecycle_ports(
                repositories={name: Path(path) for name, path in operator["repositories"].items()},
                project=operator["project"], area=operator["area"], journal=journal,
                approvals=approvals, execution_issuer=execution_issuer, runtime_issuer=runtime_issuer,
            )
            graph = build_atomic_runtime(args.name, ports, checkpointer=checkpoint, arguments=payload)
            output = run_atomic_cli(graph, name=args.name, thread_id=args.thread_id,
                                    arguments=payload, resume=args.resume,
                                    approval_request_id=args.approval_request_id)
            record_cli_metrics(graph.get_state(config).values, output)
            print(json.dumps(output, ensure_ascii=True, allow_nan=False))
            return lifecycle_exit_code(output)
    except Exception:
        reason = "cli_precondition_failed"
        print(json.dumps({"outcome": "blocked", "outcome_reason": reason, "errors": [reason]}), file=sys.stderr)
        return 1
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())