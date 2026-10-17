from __future__ import annotations

import argparse
from dataclasses import dataclass, fields
from importlib import import_module
import json
from typing import Any, Never

from langgraph.checkpoint.memory import MemorySaver

from kwikledgers_langgraph import adapters


@dataclass(frozen=True)
class GraphSpecification:
    name: str
    ports_type: type
    cli_module: str | None
    effects: tuple[str, ...]
    approval: str = "not_required"


GRAPH_SPECIFICATIONS = (
    GraphSpecification("daily_summary", adapters.DailySummaryPorts, "cli",
                       ("azure_read", "local_tracking_write", "optional_notification")),
    GraphSpecification("sprint_stories", adapters.SprintStoriesPorts, "sprint_stories_cli", ("azure_read",)),
    GraphSpecification("process_snapshot", adapters.ProcessSnapshotPorts, None,
                       ("provider_read", "local_tracking_write")),
    GraphSpecification("technical_debt_preparation", adapters.TechnicalDebtPreparationPorts,
                       "technical_debt_preparation_cli", ("azure_read", "local_tracking_write")),
    GraphSpecification("story_pull_request", adapters.StoryPullRequestPorts, "story_pr_cli",
                       ("azure_read", "azure_write"), "explicit_after_preview"),
    GraphSpecification("story_branch_association", adapters.StoryBranchAssociationPorts, "story_branch_cli",
                       ("azure_read", "azure_write"), "explicit_after_preview"),
    GraphSpecification("story_status_transition", adapters.StoryStatusTransitionPorts, "story_status_cli",
                       ("azure_read", "azure_write"), "explicit_after_preview"),
    GraphSpecification("task_closure", adapters.TaskClosurePorts, "task_closure_cli",
                       ("azure_read", "azure_write"), "explicit_after_preview"),
    GraphSpecification("day_end_closure", adapters.DayEndClosurePorts, "day_end_closure_cli",
                       ("azure_read", "policy_dependent_write"), "runtime_policy"),
    GraphSpecification("story_start", adapters.LifecyclePorts, "lifecycle_cli",
                       ("azure_read", "azure_write", "git_write", "local_tracking_write"),
                       "explicit_after_preview"),
    GraphSpecification("story_completion", adapters.LifecyclePorts, "lifecycle_cli",
                       ("azure_read", "azure_write", "local_tracking_write"), "explicit_after_preview"),
    GraphSpecification("task_cycle", adapters.LifecyclePorts, "lifecycle_cli",
                       ("azure_read", "azure_write", "git_write", "local_tracking_write", "operator_command"),
                       "explicit_after_preview"),
)


def _entry_point(module: str, *arguments: str) -> list[str]:
    return ["python", "-m", f"kwikledgers_langgraph.{module}", *arguments]


def _operational_metadata(specification: GraphSpecification) -> dict[str, Any]:
    module = specification.cli_module
    entry = (_entry_point(module, specification.name) if module == "lifecycle_cli"
             else _entry_point(module) if module is not None else None)
    resume_option = None
    if specification.approval == "explicit_after_preview":
        resume_option = "--resume" if module == "lifecycle_cli" else "--resume-state-file"
    return {
        "id": specification.name,
        "entry_point": entry,
        "help_command": [*entry, "--help"] if entry is not None else None,
        "entry_status": "declared" if entry is not None else "not_exposed",
        "runtime_status": "not_checked",
        "effects": list(specification.effects),
        "approval": specification.approval,
        "resume_option": resume_option,
        "approval_option": "--approval-request-id" if resume_option else None,
    }


class InspectionPort:
    def __getattr__(self, name: str) -> Any:
        return self.blocked

    def blocked(self, *arguments: Any, **keywords: Any) -> Never:
        raise RuntimeError("Graph inspection cannot execute integration ports")


def graph_catalog() -> list[dict[str, Any]]:
    catalog = []
    for specification in GRAPH_SPECIFICATIONS:
        name, ports_type = specification.name, specification.ports_type
        module_name = {"story_start": "story_lifecycle", "story_completion": "story_lifecycle",
                       "task_cycle": "task_lifecycle"}.get(name, name)
        module = import_module(f"kwikledgers_langgraph.graphs.{module_name}")
        factory = getattr(module, f"build_{name}_graph")
        ports = ports_type(**{field.name: InspectionPort() for field in fields(ports_type)})
        options = {"checkpointer": MemorySaver()} if ports_type is adapters.LifecyclePorts else {}
        drawable = factory(ports, **options).get_graph()
        catalog.append({
            "id": name,
            "source": f"kwikledgers_langgraph/graphs/{module_name}.py",
            "nodes": [{"id": identifier, "label": identifier} for identifier in drawable.nodes],
            "edges": [{"source": edge.source, "target": edge.target,
                       "conditional": edge.conditional,
                       "label": str(edge.data) if edge.data is not None else ""}
                      for edge in drawable.edges],
            "mermaid": drawable.draw_mermaid(),
            "operation": _operational_metadata(specification),
        })
    return catalog


def operational_catalog() -> dict[str, Any]:
    from kwikledgers_langgraph.atomic_runtime import (
        LIFECYCLE_ROUTES, MUTATION_SCHEMAS, PREVIEW_SCHEMAS, READ_SCHEMAS,
    )
    from kwikledgers_langgraph.lifecycle_runtime import LIFECYCLE_BUILDERS
    from kwikledgers_langgraph.work_item_creation import ACTION, NATIVE_FIELDS

    flows = [_operational_metadata(specification) for specification in GRAPH_SPECIFICATIONS]
    known = {flow["id"] for flow in flows}
    for name in LIFECYCLE_BUILDERS:
        if name not in known:
            flows.append({
                "id": name, "entry_point": _entry_point("lifecycle_cli", name),
                "help_command": _entry_point("lifecycle_cli", "--help"),
                "entry_status": "declared", "runtime_status": "not_checked",
                "effects": ["azure_read", "azure_write", "local_tracking_write"],
                "approval": "explicit_after_preview", "resume_option": "--resume",
                "approval_option": "--approval-request-id",
            })
    flows.append({
        "id": ACTION, "entry_point": _entry_point("work_item_creation_cli"),
        "help_command": _entry_point("work_item_creation_cli", "--help"),
        "entry_status": "declared", "runtime_status": "not_checked",
        "effects": ["azure_read", "azure_write", "local_tracking_write"],
        "approval": "explicit_after_preview", "resume_option": "--resume",
        "approval_option": "--approval-request-id",
        "argument_fields": sorted(NATIVE_FIELDS),
    })
    capabilities = []
    for kind, schemas in (("read", READ_SCHEMAS), ("preview", PREVIEW_SCHEMAS),
                          ("approved_atomic", MUTATION_SCHEMAS)):
        for name, schema in schemas.items():
            mutation = kind == "approved_atomic"
            capabilities.append({
                "id": name, "kind": kind,
                "entry_point": _entry_point("atomic_cli", "mutation" if mutation else "read", name),
                "help_command": _entry_point("atomic_cli", "mutation" if mutation else "read", "--help"),
                "argument_fields": {"required": list(schema.required), "optional": list(schema.optional)},
                "approval": "explicit_after_preview" if mutation else "not_required",
                "resume_option": "--resume" if mutation else None,
                "approval_option": "--approval-request-id" if mutation else None,
                "runtime_status": "not_checked",
            })
    return {
        "version": 1,
        "inspection_only": True,
        "integration_checks": "not_performed",
        "flows": flows,
        "atomic_capabilities": capabilities,
        "lifecycle_required": [
            {"capability": name, "route_description": route, "direct_atomic_execution": False}
            for name, route in LIFECYCLE_ROUTES.items()
        ],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Inspect graph topologies or declared public workflow entries.")
    parser.add_argument("--operational", action="store_true",
                        help="Return the versioned public-entry catalog without executing integrations.")
    arguments = parser.parse_args(argv)
    result = operational_catalog() if arguments.operational else graph_catalog()
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()