from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any

from langgraph.types import Command

from kwikledgers_langgraph.atomic_cli import load_json
from kwikledgers_langgraph.lifecycle_adapters import ExplicitLocalApproval, LifecycleJournal, build_lifecycle_ports
from kwikledgers_langgraph.lifecycle_cli import checkpoint_lock, lifecycle_confirmation, lifecycle_issuers, lifecycle_exit_code
from kwikledgers_langgraph.lifecycle_runtime import SignedLifecycleCheckpointer
from kwikledgers_langgraph.work_item_creation import (
    ACTION, CreationValidationError, build_work_item_creation_graph, format_creation_rich_text,
    validate_native_arguments, validate_operator,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Explicitly approved native UserStory, Task and Bug creation.")
    result.add_argument("--arguments-file", type=Path)
    result.add_argument("--operator-config", required=True, type=Path)
    result.add_argument("--checkpoint", required=True, type=Path)
    result.add_argument("--thread-id", required=True)
    result.add_argument("--resume", action="store_true")
    approval = result.add_mutually_exclusive_group()
    result.set_defaults(receipt=None)
    approval.add_argument("--approval-request-id", help="Explicitly approve the current preview on a separate resume.")
    return result


def native_formatter(value: str | None) -> str | None:
    return format_creation_rich_text(value)


def run_work_item_creation_cli(graph: Any, *, thread_id: str, arguments: dict[str, Any],
                               operator: dict[str, Any], resume: bool = False, receipt: Any = None,
                               approval_request_id: str | None = None) -> dict[str, Any]:
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise CreationValidationError("Nonempty creation thread required")
    if receipt is not None:
        raise CreationValidationError("Receipts were removed; use --approval-request-id")
    payload = validate_native_arguments(arguments)
    policy = validate_operator(operator)
    config = {"configurable": {"thread_id": thread_id}}
    saved = graph.get_state(config)
    if resume:
        if not saved.values or not saved.next:
            raise CreationValidationError("Resume requires an interrupted checkpoint and separate explicit approval")
        state = saved.values
        context = state.get("context", {})
        if (state.get("observability", {}).get("flow_name") != ACTION
                or state.get("pending_approval", {}).get("action") != ACTION
                or context.get("creation_arguments") != payload or context.get("creation_operator") != policy):
            raise CreationValidationError("Resume cannot change creation arguments, operator constraints or flow")
        try:
            confirmation = lifecycle_confirmation(state, receipt=receipt, approval_request_id=approval_request_id)
        except ValueError as error:
            raise CreationValidationError(str(error)) from error
        result = graph.invoke(Command(resume=deepcopy(confirmation)), config)
    else:
        if receipt is not None or approval_request_id is not None or saved.values:
            raise CreationValidationError("Preview cannot include approval or reset an existing thread")
        result = graph.invoke({"request": {}}, config)
    prompt = result["__interrupt__"][0].value if result.get("__interrupt__") else None
    return {"outcome": result.get("outcome"), "run_id": result.get("run_id"),
            "pending_approval": result.get("pending_approval", {}), "prompt": prompt,
            "results": result.get("results", {}), "errors": result.get("errors", [])}


def _key(name: str) -> bytes:
    return b""


def main(argv: list[str] | None = None) -> int:
    arguments_parser = parser()
    args = arguments_parser.parse_args(argv)
    if (args.receipt is not None or args.approval_request_id is not None) and not args.resume:
        arguments_parser.error("Approval requires a separate --resume invocation")
    if args.resume and args.receipt is None and args.approval_request_id is None:
        arguments_parser.error("--resume requires --approval-request-id")
    if not args.resume and not args.arguments_file:
        arguments_parser.error("preview requires --arguments-file")
    journal = None
    try:
        operator = validate_operator(load_json(args.operator_config))
        execution_issuer, runtime_issuer = lifecycle_issuers()
        approvals = ExplicitLocalApproval()
        with checkpoint_lock(args.checkpoint):
            journal = LifecycleJournal(args.checkpoint)
            checkpoint = SignedLifecycleCheckpointer(journal)
            config = {"configurable": {"thread_id": args.thread_id}}
            previous = checkpoint.get(config)
            saved = previous.get("channel_values", {}) if previous else {}
            if args.resume and not saved:
                raise CreationValidationError("Existing creation checkpoint required")
            if saved and saved.get("observability", {}).get("flow_name") != ACTION:
                raise CreationValidationError("Thread belongs to another lifecycle")
            context = saved.get("context", {})
            payload = validate_native_arguments(load_json(args.arguments_file) if args.arguments_file
                                                else context.get("creation_arguments"))
            if args.resume and (context.get("creation_arguments") != payload
                                or context.get("creation_operator") != operator):
                raise CreationValidationError("Resume cannot replace prepared request or operator constraints")
            ports = build_lifecycle_ports(
                repositories={name: Path(path) for name, path in operator["repositories"].items()},
                project=operator["project"], area=operator["area"], journal=journal,
                approvals=approvals, execution_issuer=execution_issuer, runtime_issuer=runtime_issuer,
            )
            graph = build_work_item_creation_graph(ports, checkpointer=checkpoint, arguments=payload,
                                                   operator=operator, formatter=native_formatter)
            output = run_work_item_creation_cli(graph, thread_id=args.thread_id, arguments=payload,
                operator=operator, resume=args.resume,
                approval_request_id=args.approval_request_id)
            print(json.dumps(output, ensure_ascii=True, allow_nan=False))
            return lifecycle_exit_code(output)
    except CreationValidationError as error:
        print(f"Creation blocked: {error}.", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Creation blocked: {type(error).__name__}.", file=sys.stderr)
        return 1
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())