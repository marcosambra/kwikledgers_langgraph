from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import WorkItemCreationConfigPorts
from kwikledgers_langgraph.guardrails import OperationKind, allow_without_approval
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_error, append_tracking_event
from kwikledgers_langgraph.work_item_creation import (
    FIELD_REFERENCES, CreationValidationError, WorkItemKind, _lookup, validate_operator,
)


FLOW = "work_item_creation_config"
# Native create supplies the type and lets Azure initialize state and reason.
AZURE_INITIALIZED_FIELDS = frozenset({
    "System.WorkItemType", "System.TeamProject", "System.State", "System.Reason",
})
CLASSIFICATION_PATH_FIELDS = {
    "System.AreaId": "System.AreaPath",
    "System.IterationId": "System.IterationPath",
}


def validate_repositories(repositories: Any) -> dict[str, str]:
    if (not isinstance(repositories, dict) or not repositories
            or any(not isinstance(name, str) or not name.strip()
                   or not isinstance(path, str) or not path.strip()
                   for name, path in repositories.items())):
        raise CreationValidationError("Explicit repository names and local paths are required")
    result = {}
    for name, path in repositories.items():
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise CreationValidationError(f"Repository path is not a directory: {name}")
        result[name] = str(resolved)
    return result


def prepare_operator_configuration(metadata: Any, repositories: dict[str, str]) -> dict[str, Any]:
    if not isinstance(metadata, dict) or type(metadata.get("version")) is not int or metadata["version"] != 1:
        raise CreationValidationError("Native process metadata version 1 is required")
    for key in ("project", "area"):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise CreationValidationError(f"Native process metadata requires {key}")
    types = metadata.get("work_item_types")
    if not isinstance(types, list) or len(types) != len(WorkItemKind):
        raise CreationValidationError("Native constraints for UserStory, Task and Bug are required")
    constraints: dict[str, Any] = {}
    for item in types:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise CreationValidationError("Native type name is required")
        matches = [kind for kind in WorkItemKind if _lookup(item["name"]) in
                   ({_lookup(kind.value), "story"} if kind is WorkItemKind.USER_STORY else {_lookup(kind.value)})]
        if len(matches) != 1 or matches[0].value in constraints:
            raise CreationValidationError("Missing, duplicate or ambiguous native creation type")
        fields = item.get("fields")
        if not isinstance(fields, list) or not fields:
            raise CreationValidationError(f"Native field constraints are missing: {item['name']}")
        constraint: dict[str, Any] = {
            "native_type": item["name"], "aliases": [], "supported_fields": [],
            "required_fields": [], "defaults": {}, "allowed_values": {},
        }
        seen: set[str] = set()
        for field in fields:
            if (not isinstance(field, dict) or not isinstance(field.get("reference_name"), str)
                    or not field["reference_name"].strip()
                    or type(field.get("always_required")) is not bool
                    or "read_only" not in field
                    or "allowed_values" not in field
                    or (field["allowed_values"] is not None and not isinstance(field["allowed_values"], list))
                    or "default_value" not in field):
                raise CreationValidationError("Incomplete native field constraints")
            reference = field["reference_name"]
            if reference in seen:
                raise CreationValidationError(f"Duplicate native field: {reference}")
            seen.add(reference)
            if field["read_only"] is True or reference in AZURE_INITIALIZED_FIELDS:
                continue
            if reference in CLASSIFICATION_PATH_FIELDS:
                path_reference = CLASSIFICATION_PATH_FIELDS[reference]
                if field["always_required"] and path_reference not in constraint["required_fields"]:
                    constraint["required_fields"].append(path_reference)
                continue
            default = field["default_value"]
            if reference not in FIELD_REFERENCES.values():
                if field["always_required"] and (default is None or default == ""):
                    constraint["required_fields"].append(reference)
                continue
            if field["read_only"] is not False:
                raise CreationValidationError(f"Native writable field metadata is missing: {reference}")
            constraint["supported_fields"].append(reference)
            if field["always_required"] and reference not in constraint["required_fields"]:
                constraint["required_fields"].append(reference)
            if default is not None and default != "":
                constraint["defaults"][reference] = deepcopy(default)
            if field["allowed_values"]:
                constraint["allowed_values"][reference] = deepcopy(field["allowed_values"])
        if "System.Title" not in constraint["supported_fields"]:
            raise CreationValidationError("Native type does not support System.Title")
        constraints[matches[0].value] = constraint
    return validate_operator({
        "project": metadata["project"].strip(), "area": metadata["area"].strip(),
        "repositories": validate_repositories(repositories), "process_constraints": constraints,
    })


def build_work_item_creation_config_graph(ports: WorkItemCreationConfigPorts) -> Any:
    graph = StateGraph(AgentState)

    def failure(state: AgentState, node: str, error: Exception) -> dict[str, Any]:
        return {
            **append_error(state, str(error)),
            **record_runtime_event(state, node=node, status="failed",
                                   message="Falha ao preparar configuracao nativa de criacao.",
                                   payload={"error_type": type(error).__name__}),
            "work_item_creation_config_result": {"status": "blocked"},
        }

    def validate_request(state: AgentState) -> dict[str, Any]:
        guardrail = allow_without_approval(state, action=f"{FLOW}.prepare", operation_kind=OperationKind.READ_ONLY)
        try:
            repositories = validate_repositories(state.get("work_item_creation_config_request", {}).get("repositories"))
        except (ValueError, OSError) as error:
            return failure(state, "validate_request", error)
        guarded: AgentState = {**state, **guardrail}
        return {
            **guardrail,
            **record_runtime_event(guarded, node="validate_request", status="completed",
                                   message="Repositorios explicitamente selecionados pelo operador."),
            "work_item_creation_config_request": {"repositories": repositories},
        }

    def fetch_metadata(state: AgentState) -> dict[str, Any]:
        try:
            metadata = ports.metadata.fetch_work_item_process_metadata()
        except (RuntimeError, ValueError, OSError) as error:
            return failure(state, "fetch_metadata", error)
        return {
            **record_runtime_event(state, node="fetch_metadata", status="completed",
                                   message="Metadados reais do processo Azure consultados sem mutacoes."),
            "work_item_creation_config_metadata": metadata,
        }

    def prepare_configuration(state: AgentState) -> dict[str, Any]:
        try:
            operator = prepare_operator_configuration(
                state.get("work_item_creation_config_metadata"),
                state["work_item_creation_config_request"]["repositories"],
            )
        except (ValueError, OSError) as error:
            return failure(state, "prepare_configuration", error)
        return {
            **append_tracking_event(state, category=FLOW, message="Configuracao nativa validada; nenhuma escrita Azure.",
                                    payload={"type_count": len(operator["process_constraints"])}),
            **record_runtime_event(state, node="prepare_configuration", status="completed",
                                   message="Configuracao compativel com a criacao existente preparada."),
            "work_item_creation_config_result": {"status": "prepared", "operator": operator},
        }

    def route(state: AgentState) -> str:
        return "blocked" if state.get("errors") else "continue"

    graph.add_node("validate_request", validate_request)
    graph.add_node("fetch_metadata", fetch_metadata)
    graph.add_node("prepare_configuration", prepare_configuration)
    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges("validate_request", route, {"blocked": END, "continue": "fetch_metadata"})
    graph.add_conditional_edges("fetch_metadata", route, {"blocked": END, "continue": "prepare_configuration"})
    graph.add_edge("prepare_configuration", END)
    return graph.compile()
