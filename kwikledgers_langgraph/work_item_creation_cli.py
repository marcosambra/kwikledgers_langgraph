from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from typing import Any

from langgraph.types import Command

from kwikledgers_langgraph.atomic_cli import load_json
from kwikledgers_langgraph.lifecycle_adapters import LifecycleJournal, SignedReceiptApproval, build_lifecycle_ports
from kwikledgers_langgraph.lifecycle_cli import checkpoint_lock, lifecycle_issuers, lifecycle_exit_code
from kwikledgers_langgraph.lifecycle_runtime import SignedLifecycleCheckpointer
from kwikledgers_langgraph.work_item_creation import (
    ACTION, CreationValidationError, build_work_item_creation_graph, format_creation_rich_text,
    validate_native_arguments, validate_operator,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Receipt-approved native UserStory, Task and Bug creation.")
    result.add_argument("--arguments-file", type=Path)
    result.add_argument("--operator-config", required=True, type=Path)
    result.add_argument("--checkpoint", required=True, type=Path)
    result.add_argument("--thread-id", required=True)
    result.add_argument("--resume", action="store_true")
    result.add_argument("--receipt", type=Path)
    return result


def native_formatter(value: str | None) -> str | None:
    return format_creation_rich_text(value)


def run_work_item_creation_cli(graph: Any, *, thread_id: str, arguments: dict[str, Any],
                               operator: dict[str, Any], resume: bool = False, receipt: Any = None) -> dict[str, Any]:
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise CreationValidationError("Nonempty creation thread required")
    payload = validate_native_arguments(arguments)
    policy = validate_operator(operator)
    config = {"configurable": {"thread_id": thread_id}}
    saved = graph.get_state(config)
    if resume:
        if not saved.values or not saved.next or receipt is None:
            raise CreationValidationError("Resume requires an interrupted checkpoint and separate signed receipt")
        state = saved.values
        context = state.get("context", {})
        if (state.get("observability", {}).get("flow_name") != ACTION
                or state.get("pending_approval", {}).get("action") != ACTION
                or context.get("creation_arguments") != payload or context.get("creation_operator") != policy):
            raise CreationValidationError("Resume cannot change creation arguments, operator constraints or flow")
        result = graph.invoke(Command(resume=deepcopy(receipt)), config)
    else:
        if receipt is not None or saved.values:
            raise CreationValidationError("Preview cannot include approval or reset an existing thread")
        result = graph.invoke({"request": {}}, config)
    prompt = result["__interrupt__"][0].value if result.get("__interrupt__") else None
    return {"outcome": result.get("outcome"), "run_id": result.get("run_id"),
            "pending_approval": result.get("pending_approval", {}), "prompt": prompt,
            "results": result.get("results", {}), "errors": result.get("errors", [])}


def _key(name: str) -> bytes:
    value = os.environ.get(name, "").encode()
    if len(value) < 32:
        raise CreationValidationError(f"{name} requires at least 32 bytes")
    return value


def main(argv: list[str] | None = None) -> int:
    arguments_parser = parser()
    args = arguments_parser.parse_args(argv)
    if args.receipt and not args.resume:
        arguments_parser.error("--receipt requires a separate --resume invocation")
    if args.resume and not args.receipt:
        arguments_parser.error("--resume requires --receipt")
    if not args.resume and not args.arguments_file:
        arguments_parser.error("preview requires --arguments-file")
    journal = None
    try:
        state_key = _key("KWIKLEDGERS_LIFECYCLE_STATE_KEY")
        approval_key = _key("KWIKLEDGERS_LIFECYCLE_APPROVAL_KEY")
        if state_key == approval_key:
            raise CreationValidationError("State and external approval keys must be distinct")
        operator = validate_operator(load_json(args.operator_config))
        execution_issuer, runtime_issuer = lifecycle_issuers()
        approvals = SignedReceiptApproval(approval_key)
        with checkpoint_lock(args.checkpoint):
            journal = LifecycleJournal(args.checkpoint, state_key)
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
                raise CreationValidationError("Resume cannot replace signed request or operator constraints")
            ports = build_lifecycle_ports(
                repositories={name: Path(path) for name, path in operator["repositories"].items()},
                project=operator["project"], area=operator["area"], journal=journal,
                approvals=approvals, execution_issuer=execution_issuer, runtime_issuer=runtime_issuer,
            )
            graph = build_work_item_creation_graph(ports, checkpointer=checkpoint, arguments=payload,
                                                   operator=operator, formatter=native_formatter)
            output = run_work_item_creation_cli(graph, thread_id=args.thread_id, arguments=payload,
                operator=operator, resume=args.resume, receipt=load_json(args.receipt) if args.receipt else None)
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