from __future__ import annotations

from dataclasses import fields
from importlib import import_module
import json
from typing import Any, Never

from langgraph.checkpoint.memory import MemorySaver

from kwikledgers_langgraph import adapters


class InspectionPort:
    def __getattr__(self, name: str) -> Any:
        return self.blocked

    def blocked(self, *arguments: Any, **keywords: Any) -> Never:
        raise RuntimeError("Graph inspection cannot execute integration ports")


def graph_catalog() -> list[dict[str, Any]]:
    specifications = (
        ("daily_summary", adapters.DailySummaryPorts),
        ("sprint_stories", adapters.SprintStoriesPorts),
        ("process_snapshot", adapters.ProcessSnapshotPorts),
        ("technical_debt_preparation", adapters.TechnicalDebtPreparationPorts),
        ("story_pull_request", adapters.StoryPullRequestPorts),
        ("story_branch_association", adapters.StoryBranchAssociationPorts),
        ("story_status_transition", adapters.StoryStatusTransitionPorts),
        ("task_closure", adapters.TaskClosurePorts),
        ("day_end_closure", adapters.DayEndClosurePorts),
        ("story_start", adapters.LifecyclePorts),
        ("story_completion", adapters.LifecyclePorts),
        ("task_cycle", adapters.LifecyclePorts),
    )
    catalog = []
    for name, ports_type in specifications:
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
        })
    return catalog


def main() -> None:
    print(json.dumps(graph_catalog(), ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()