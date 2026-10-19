from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sys
from typing import Any, Iterator

from langgraph.types import Command

from kwikledgers_langgraph import fastmcp_execution_capability as issuers
from kwikledgers_langgraph.lifecycle_adapters import (
    ExplicitLocalApproval, LifecycleJournal, OperatorTaskPlan, build_lifecycle_ports,
)
from kwikledgers_langgraph.lifecycle_runtime import (
    LIFECYCLE_BUILDERS, SignedLifecycleCheckpointer, build_lifecycle_runtime,
)
from kwikledgers_langgraph.observability import append_request_metrics, workflow_completion


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Preview-first lifecycle with separate explicit human approval.")
    result.add_argument("flow", choices=tuple(LIFECYCLE_BUILDERS))
    result.add_argument("--operator-config", required=True, type=Path)
    result.add_argument("--checkpoint", required=True, type=Path)
    result.add_argument("--thread-id", required=True)
    result.add_argument("--story-id", type=int)
    result.add_argument("--task-id", type=int)
    result.add_argument("--repository-name")
    result.add_argument("--work-kind", choices=("implementation", "fix"))
    result.add_argument("--fix-summary")
    result.add_argument("--origin-task-id", type=int)
    result.add_argument("--preview-key")
    result.add_argument("--resume", action="store_true")
    resume = result.add_mutually_exclusive_group()
    result.set_defaults(receipt=None)
    resume.add_argument("--approval-request-id", help="Explicitly approve the current preview request on a separate resume.")
    resume.add_argument("--branch-type", choices=("feature", "hotfix"))
    return result


@contextmanager
def checkpoint_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def lifecycle_output(result: dict[str, Any]) -> dict[str, Any]:
    prompt = result["__interrupt__"][0].value if result.get("__interrupt__") else None
    outcome = result.get("outcome", "waiting")
    if outcome == "running" or (prompt is not None and outcome not in ("blocked", "denied", "reconciliation_required")):
        outcome = "waiting"
    completion = workflow_completion(result, outcome=outcome)
    return {"outcome": outcome, "outcome_reason": result.get("outcome_reason", ""),
            "completion": completion,
            "run_id": result.get("run_id"), "pending_approval": result.get("pending_approval", {}),
            "prompt": prompt, "results": result.get("results", {}),
            "direct_pr_url": result.get("direct_pr_url", ""), "errors": result.get("errors", [])}


def lifecycle_exit_code(output: dict[str, Any]) -> int:
    outcome = output.get("outcome")
    if outcome == "reconciliation_required":
        return 3
    if outcome in ("blocked", "denied"):
        return 1
    if outcome in ("running", "waiting", "interrupted") or output.get("prompt") is not None or output.get("pending_approval"):
        return 2
    return 0 if outcome == "completed" else 1


def record_cli_metrics(state: dict[str, Any], output: dict[str, Any]) -> None:
    try:
        append_request_metrics(state)
    except (OSError, ValueError, RuntimeError) as error:
        warning = (
            "Falha ao persistir métricas após a execução do grafo: "
            f"{type(error).__name__}. O resultado da operação permanece válido; "
            "não repita escritas para corrigir a telemetria."
        )
        output["metrics_error"] = warning
        print(warning, file=sys.stderr)


def lifecycle_confirmation(state: dict[str, Any], *, receipt: Any = None,
                           approval_request_id: str | None = None) -> Any:
    if receipt is not None:
        raise ValueError("Receipts were removed; review the preview and use --approval-request-id")
    if approval_request_id is None:
        raise ValueError("Resume requires explicit approval of the current request")
    pending = state.get("pending_approval", {})
    run_id = state.get("run_id")
    if (not run_id or pending.get("run_id") != run_id
            or state.get("observability", {}).get("active_run_id") != run_id):
        raise ValueError("Explicit approval requires the current active lifecycle run")
    return ExplicitLocalApproval().confirm(pending, approval_request_id)


def run_cli(graph: Any, *, thread_id: str, request: dict[str, Any] | None = None,
            resume: bool = False, receipt: Any = None, branch_type: str | None = None,
            approval_request_id: str | None = None) -> dict[str, Any]:
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Nonempty lifecycle thread required")
    if receipt is not None:
        raise ValueError("Receipts were removed; use --approval-request-id")
    if sum(value is not None for value in (receipt, branch_type, approval_request_id)) > 1:
        raise ValueError("Approval and branch selection are separate process stages")
    config = {"configurable": {"thread_id": thread_id}}
    previous = graph.get_state(config)
    if resume:
        if not previous.values or not previous.next:
            raise ValueError("Existing interrupted lifecycle required")
        context = previous.values.get("context", {})
        expected = {"story_id": context.get("story_id"), "task_id": context.get("task_id"),
                    "repository_name": context.get("repository"),
                    **{field: context.get(field) for field in ("work_kind", "fix_summary", "origin_task_id")}}
        if request and any(value is not None and value != expected.get(field) for field, value in request.items()):
            raise ValueError("Resume cannot change authorized lifecycle context")
        pending = previous.values.get("pending_approval", {})
        if branch_type is not None:
            if pending or not any("branch_type" in node for node in previous.next):
                raise ValueError("Branch selection is not the current process stage")
            confirmation = branch_type
        elif receipt is not None or approval_request_id is not None:
            if not pending:
                raise ValueError("Approval does not target an approval gate")
            confirmation = lifecycle_confirmation(previous.values, receipt=receipt,
                                                  approval_request_id=approval_request_id)
        else:
            raise ValueError("Resume requires explicit approval or branch selection")
        result = graph.invoke(Command(resume=confirmation), config)
    else:
        if receipt is not None or branch_type is not None or approval_request_id is not None:
            raise ValueError("Approval cannot be supplied in the preview invocation")
        if previous.values:
            raise ValueError("Thread already exists; resume it instead of resetting evidence")
        result = graph.invoke({"request": request or {}}, config)
    return lifecycle_output(result)


def lifecycle_issuers() -> tuple[Any, Any]:
    execution = getattr(issuers, "issue_lifecycle_execution_capability", None)
    runtime = getattr(issuers, "issue_runtime_execution_capability", None)
    if not callable(execution) or not callable(runtime):
        raise RuntimeError("Lifecycle requires issue_lifecycle_execution_capability and issue_runtime_execution_capability")
    return execution, runtime


def main(argv: list[str] | None = None) -> int:
    arguments_parser = parser()
    args = arguments_parser.parse_args(argv)
    if (args.receipt is not None or args.branch_type is not None or args.approval_request_id is not None) and not args.resume:
        arguments_parser.error("Approval and branch selection require a separate --resume invocation")
    if args.flow == "technical_debt_execute" and not args.preview_key:
        arguments_parser.error("technical_debt_execute requires --preview-key")
    if args.flow != "technical_debt_execute" and args.preview_key:
        arguments_parser.error("--preview-key applies only to technical_debt_execute")
    if not args.resume and not args.repository_name:
        arguments_parser.error("preview requires --repository-name")
    if not args.resume and args.flow != "technical_debt_execute" and not all((args.story_id, args.task_id)):
        arguments_parser.error("preview requires --story-id, --task-id and --repository-name")
    journal = None
    issuers_available = False
    try:
        execution_issuer, runtime_issuer = lifecycle_issuers()
        issuers_available = True
        operator = json.loads(args.operator_config.read_text(encoding="utf-8"))
        approvals = ExplicitLocalApproval()
        with checkpoint_lock(args.checkpoint):
            journal = LifecycleJournal(args.checkpoint)
            plans = tuple(OperatorTaskPlan(
                plan_id=item["plan_id"], repository=item["repository"], story_id=item["story_id"],
                task_id=item["task_id"], files=tuple(item["files"]),
                implementation_command=tuple(item["implementation_command"]),
                test_commands=tuple(tuple(command) for command in item["test_commands"]),
                logic=item["logic"], risks=tuple(item.get("risks", [])),
            ) for item in operator.get("plans", []))
            ports = build_lifecycle_ports(
                repositories={name: Path(path) for name, path in operator["repositories"].items()},
                project=operator["project"], area=operator["area"], journal=journal, approvals=approvals,
                plans=plans,
                execution_issuer=execution_issuer, runtime_issuer=runtime_issuer,
            )
            checkpoint = SignedLifecycleCheckpointer(journal)
            graph = build_lifecycle_runtime(args.flow, ports, checkpointer=checkpoint, preview_key=args.preview_key)
            saved = graph.get_state({"configurable": {"thread_id": args.thread_id}})
            if saved.values and saved.values.get("observability", {}).get("flow_name") != args.flow:
                raise ValueError("Thread belongs to another lifecycle")
            output = run_cli(graph, thread_id=args.thread_id, resume=args.resume,
                             approval_request_id=args.approval_request_id,
                             branch_type=args.branch_type, request={"story_id": args.story_id,
                             "task_id": args.task_id, "repository_name": args.repository_name,
                             "work_kind": args.work_kind, "fix_summary": args.fix_summary,
                             "origin_task_id": args.origin_task_id})
            record_cli_metrics(graph.get_state({"configurable": {"thread_id": args.thread_id}}).values, output)
            print(json.dumps(output, ensure_ascii=True, allow_nan=False))
            return lifecycle_exit_code(output)
    except Exception:
        reason = "capability_issuers_unavailable" if not issuers_available else "cli_precondition_failed"
        print(json.dumps({"outcome": "blocked", "outcome_reason": reason, "errors": [reason]}), file=sys.stderr)
        return 1
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())