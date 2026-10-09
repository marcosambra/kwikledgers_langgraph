from __future__ import annotations

from copy import deepcopy
from enum import Enum
from html import escape
from html.parser import HTMLParser
import math
from typing import Any, Callable, Literal, TypedDict
import unicodedata

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.graphs.lifecycle_engine import LifecycleStep, build_lifecycle_graph
from kwikledgers_langgraph.lifecycle_adapters import (
    LifecycleGateway, active_state, canonical, require_approved_preview,
)


ACTION = "work_item_creation"
CAPABILITY = "create_work_item"
FIBONACCI = frozenset({1, 2, 3, 5, 8, 13, 21})
DEBT_ALIASES = frozenset({"technicaldebt", "technicaldebts", "techdebt", "techdebts",
                          "debt", "debitotecnico", "debitostecnicos"})
FIELD_REFERENCES = {
    "title": "System.Title", "description": "System.Description",
    "iteration_path": "System.IterationPath", "area_path": "System.AreaPath",
    "assigned_to": "System.AssignedTo", "tags": "System.Tags",
    "acceptance_criteria": "Microsoft.VSTS.Common.AcceptanceCriteria",
    "story_points": "Microsoft.VSTS.Scheduling.StoryPoints",
    "remaining_work_hours": "Microsoft.VSTS.Scheduling.RemainingWork",
    "completed_work_hours": "Microsoft.VSTS.Scheduling.CompletedWork",
    "original_estimate_hours": "Microsoft.VSTS.Scheduling.OriginalEstimate",
}
NATIVE_FIELDS = frozenset(FIELD_REFERENCES) | {
    "work_item_type", "repository_name", "use_next_sprint", "parent_work_item_id",
}


class WorkItemKind(str, Enum):
    USER_STORY = "UserStory"
    TASK = "Task"
    BUG = "Bug"


class CreateOptions(TypedDict, total=False):
    description: str | None
    repository_name: str | None
    iteration_path: str | None
    use_next_sprint: bool
    area_path: str | None
    assigned_to: str | None
    tags: str | None
    acceptance_criteria: str | None
    story_points: float | None
    remaining_work_hours: float | None
    completed_work_hours: float | None
    original_estimate_hours: float | None


class OptionalParent(CreateOptions, total=False):
    parent_work_item_id: int | None


class NativeCreateArguments(OptionalParent):
    work_item_type: str
    title: str


class UserStoryArguments(OptionalParent):
    work_item_type: Literal["UserStory"]
    title: str


class TaskArguments(CreateOptions):
    work_item_type: Literal["Task"]
    title: str
    parent_work_item_id: int


class BugArguments(OptionalParent):
    work_item_type: Literal["Bug"]
    title: str


class ProcessConstraint(TypedDict):
    native_type: str
    aliases: list[str]
    supported_fields: list[str]
    required_fields: list[str]
    defaults: dict[str, Any]
    allowed_values: dict[str, list[Any]]


class CreationValidationError(ValueError):
    pass


class TechnicalDebtLifecycleRequired(CreationValidationError):
    pass


class _CreationHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.paragraph = False
        self.valid = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if attrs or (tag != "p" and not (tag == "br" and self.paragraph)) or (tag == "p" and self.paragraph):
            self.valid = False
        if tag == "p":
            self.paragraph = True
        self.parts.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag != "p" or not self.paragraph:
            self.valid = False
        self.paragraph = False
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if data and not self.paragraph:
            self.valid = False
        self.parts.append(escape(data, quote=False))


def format_creation_rich_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = _CreationHTML()
    parser.feed(value)
    parser.close()
    if parser.valid and not parser.paragraph and "".join(parser.parts) == value and value.startswith("<p>"):
        return value
    return "<p>" + escape(value, quote=False).replace("\n", "<br>") + "</p>"


def _lookup(value: str) -> str:
    return "".join(character for character in unicodedata.normalize("NFKD", value).casefold()
                   if character.isascii() and character.isalnum())


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def validate_native_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - NATIVE_FIELDS:
        raise CreationValidationError("Only the exact native create schema is accepted; revision guards are not supported")
    if not all(_text(arguments.get(field)) for field in ("work_item_type", "title")):
        raise CreationValidationError("work_item_type and title must contain text")
    for field, value in arguments.items():
        if field == "use_next_sprint":
            if type(value) is not bool:
                raise CreationValidationError("use_next_sprint must be boolean")
        elif field == "parent_work_item_id":
            if value is not None and not _positive(value):
                raise CreationValidationError("parent_work_item_id must be a positive integer")
        elif field in {"story_points", "remaining_work_hours", "completed_work_hours", "original_estimate_hours"}:
            if value is not None:
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise CreationValidationError(f"{field} must be finite and nonnegative")
                if field == "story_points" and value not in FIBONACCI:
                    raise CreationValidationError("story_points must use Fibonacci sizing: 1, 2, 3, 5, 8, 13, 21")
        elif value is not None and not isinstance(value, str):
            raise CreationValidationError(f"{field} must be text or null")
    canonical(arguments)
    return deepcopy(arguments)


def validate_operator(operator: Any) -> dict[str, Any]:
    if not isinstance(operator, dict) or set(operator) != {"project", "area", "repositories", "process_constraints"}:
        raise CreationValidationError("Operator config requires project, area, repositories and process_constraints")
    if not _text(operator["project"]) or not _text(operator["area"]):
        raise CreationValidationError("Operator project and area are required")
    repositories = operator["repositories"]
    if (not isinstance(repositories, dict) or not repositories
            or not all(_text(name) and _text(path) for name, path in repositories.items())):
        raise CreationValidationError("Operator repositories must map names to paths")
    constraints = operator["process_constraints"]
    if not isinstance(constraints, dict) or set(constraints) != {kind.value for kind in WorkItemKind}:
        raise CreationValidationError("Host must supply constraints for UserStory, Task and Bug")
    aliases: dict[str, str] = {}
    for kind, constraint in constraints.items():
        if not isinstance(constraint, dict) or set(constraint) != set(ProcessConstraint.__annotations__):
            raise CreationValidationError(f"Incomplete host process constraints for {kind}")
        if not _text(constraint["native_type"]):
            raise CreationValidationError("Native process type is required")
        for field in ("aliases", "supported_fields", "required_fields"):
            values = constraint[field]
            if not isinstance(values, list) or not all(_text(value) for value in values) or len(set(values)) != len(values):
                raise CreationValidationError(f"{field} must contain unique nonempty strings")
        if not isinstance(constraint["defaults"], dict) or set(constraint["defaults"]) - set(FIELD_REFERENCES.values()):
            raise CreationValidationError("Process defaults cannot express custom native fields")
        if (not isinstance(constraint["allowed_values"], dict)
                or any(not _text(field) or not isinstance(values, list) or not values
                       for field, values in constraint["allowed_values"].items())):
            raise CreationValidationError("Process allowed_values must map references to nonempty arrays")
        for alias in (kind, constraint["native_type"], *constraint["aliases"]):
            normalized = _lookup(alias)
            if normalized in DEBT_ALIASES:
                raise TechnicalDebtLifecycleRequired("TechnicalDebt requires technical_debt_execute and its signed prepared preview")
            if not normalized or aliases.get(normalized, kind) != kind:
                raise CreationValidationError("Ambiguous process aliases")
            aliases[normalized] = kind
    canonical(operator)
    return deepcopy(operator)


def prepare_creation(arguments: dict[str, Any], operator: dict[str, Any],
                     formatter: Callable[[str | None], str | None]) -> tuple[dict[str, Any], WorkItemKind]:
    payload = validate_native_arguments(arguments)
    policy = validate_operator(operator)
    requested = _lookup(payload["work_item_type"])
    if requested in DEBT_ALIASES:
        raise TechnicalDebtLifecycleRequired("TechnicalDebt must use the existing technical_debt_execute lifecycle")
    matches = [kind for kind in WorkItemKind if requested in {
        _lookup(alias) for alias in (kind.value, policy["process_constraints"][kind.value]["native_type"],
                                   *policy["process_constraints"][kind.value]["aliases"])}]
    if len(matches) != 1:
        raise CreationValidationError("Type is not an operator-authorized UserStory, Task or Bug alias")
    kind = matches[0]
    constraint = policy["process_constraints"][kind.value]
    payload["work_item_type"] = constraint["native_type"]
    payload["title"] = payload["title"].strip()
    if payload.get("repository_name") not in policy["repositories"]:
        raise CreationValidationError("Explicit operator-authorized repository_name is required")
    area = policy["area"]
    area = area if area.startswith(policy["project"] + "\\") else policy["project"] + "\\" + area
    if payload.get("area_path") not in (None, area):
        raise CreationValidationError("area_path differs from operator-authorized area")
    payload["area_path"] = area
    for argument, reference in FIELD_REFERENCES.items():
        if argument not in payload and reference in constraint["defaults"]:
            payload[argument] = deepcopy(constraint["defaults"][reference])
    if not _text(payload.get("iteration_path")):
        raise CreationValidationError("Explicit iteration_path or host default required; dynamic sprint resolution cannot be approved exactly")
    if not (payload["iteration_path"] == policy["project"] or payload["iteration_path"].startswith(policy["project"] + "\\")):
        raise CreationValidationError("iteration_path belongs to another project")
    if kind is WorkItemKind.TASK and not _positive(payload.get("parent_work_item_id")):
        raise CreationValidationError("Child Task requires a validated parent_work_item_id")
    if kind is WorkItemKind.USER_STORY and payload.get("story_points") is None:
        raise CreationValidationError("UserStory requires Fibonacci story_points")
    for field in ("description", "acceptance_criteria"):
        if field in payload and payload[field] is not None:
            rendered = formatter(payload[field])
            if rendered is not None and (not isinstance(rendered, str) or not rendered.startswith("<")):
                raise CreationValidationError("Native Azure HTML formatter required")
            if rendered is not None and formatter(rendered) != rendered:
                raise CreationValidationError("Native formatter is not HTML-idempotent; MAIN must expose a neutral formatter and preserve approved HTML on create")
            payload[field] = rendered
    supported = set(constraint["supported_fields"])
    references = {reference: payload.get(argument) for argument, reference in FIELD_REFERENCES.items()}
    for reference in constraint["required_fields"]:
        if reference not in references:
            raise CreationValidationError(f"Required process field cannot be expressed by native create: {reference}")
        if references[reference] is None or references[reference] == "":
            raise CreationValidationError(f"Missing required process field: {reference}")
    for reference, value in references.items():
        if value is None or value == "":
            continue
        if reference not in supported:
            raise CreationValidationError(f"Process does not support requested field: {reference}")
        allowed = constraint["allowed_values"].get(reference)
        if allowed is not None and value not in allowed:
            raise CreationValidationError(f"Value violates process constraint: {reference}")
    complete = {field: payload.get(field, False if field == "use_next_sprint" else None)
                for field in sorted(NATIVE_FIELDS)}
    return validate_native_arguments(complete), kind


def _parent(client: Any, identifier: int, operator: dict[str, Any]) -> dict[str, Any]:
    details = client.call("get_story_details", {"story_id": identifier})
    if not isinstance(details, dict):
        raise CreationValidationError("Structured parent details required")
    fields = details.get("fields", {})
    if not isinstance(fields, dict):
        raise CreationValidationError("Structured parent fields required")
    identity = details.get("id", details.get("story_id"))
    revision = details.get("revision", details.get("rev"))
    kind = details.get("work_item_type", fields.get("System.WorkItemType"))
    project = fields.get("System.TeamProject", details.get("project"))
    area = details.get("area_path", fields.get("System.AreaPath"))
    iteration = details.get("iteration_path", fields.get("System.IterationPath"))
    if project is None and _text(area):
        project = area.split("\\", 1)[0]
    if identity != identifier or not _positive(identity) or not _positive(revision):
        raise CreationValidationError("Authoritative parent identity and positive native revision required")
    if project != operator["project"]:
        raise CreationValidationError("Parent project differs from operator project")
    if kind != operator["process_constraints"][WorkItemKind.USER_STORY.value]["native_type"]:
        raise CreationValidationError("Parent must be an authorized native UserStory, not technical debt")
    return {"id": identity, "revision": revision, "work_item_type": kind, "project": project,
            "area_path": area, "iteration_path": iteration}


class WorkItemCreationGateway(LifecycleGateway):
    def create(self, preview: dict[str, Any], key: str) -> dict[str, Any]:
        require_approved_preview(ACTION, preview)
        payload = validate_native_arguments(preview)
        state = active_state(ACTION)
        context = state["context"]
        policy = validate_operator(context["creation_operator"])
        kind = context.get("creation_kind")
        if _lookup(payload["work_item_type"]) in DEBT_ALIASES:
            raise TechnicalDebtLifecycleRequired("TechnicalDebt requires the existing debt lifecycle")
        if (kind not in policy["process_constraints"]
                or payload["work_item_type"] != policy["process_constraints"][kind]["native_type"]):
            raise CreationValidationError("Created type differs from authorized process kind")
        if payload != context["creation_payload"]:
            raise CreationValidationError("Approved payload differs from signed creation context")
        if self.execution_issuer is None:
            raise RuntimeError("MAIN must authorize work_item_creation with the exact raw native preview")

        def invoke() -> dict[str, Any]:
            parent = context.get("creation_parent")
            if parent and _parent(self.client, parent["id"], context["creation_operator"]) != parent:
                raise CreationValidationError("Parent changed; obtain a new preview and signed receipt")
            trusted = active_state(ACTION)
            token = self.execution_issuer(trusted, action=ACTION, capability_name=CAPABILITY,
                                          arguments=deepcopy(payload))
            if not _text(token):
                raise CreationValidationError("Execution capability missing")
            result = self.client.call(CAPABILITY, {**payload, "execution_capability": token})
            if not isinstance(result, dict) or not _positive(result.get("id", result.get("work_item_id"))):
                raise CreationValidationError("Created work item ID missing; reconcile outcome before retry")
            if result.get("skipped_fields"):
                raise CreationValidationError("Native process skipped approved fields; reconcile outcome before retry")
            return result

        return self.journal.once(key, payload, invoke)


def build_work_item_creation_graph(ports: LifecyclePorts, *, checkpointer: Any,
                                   arguments: dict[str, Any], operator: dict[str, Any],
                                   formatter: Callable[[str | None], str | None],
                                   gateway: LifecycleGateway | None = None) -> Any:
    supplied = validate_native_arguments(arguments)
    policy = validate_operator(operator)
    payload, kind = prepare_creation(supplied, policy, formatter)
    source = gateway if gateway is not None else getattr(ports.work_items, "gateway", None)
    if not isinstance(source, LifecycleGateway):
        raise CreationValidationError("Operational lifecycle gateway required")
    creation = WorkItemCreationGateway(source.client, source.journal, source.execution_issuer, source.runtime_issuer)

    def resolve(request: dict[str, Any]) -> dict[str, Any]:
        if ports.branches is None:
            raise CreationValidationError("Authorized repository port required")
        ports.branches.path(payload["repository_name"])
        parent = (_parent(source.client, payload["parent_work_item_id"], policy)
                  if payload.get("parent_work_item_id") else None)
        return {"repository": payload["repository_name"], "creation_kind": kind.value,
                "creation_arguments": deepcopy(supplied), "creation_payload": deepcopy(payload),
                "creation_operator": deepcopy(policy), "creation_parent": parent}

    def prepare(state: dict[str, Any]) -> dict[str, Any]:
        context = state["context"]
        if (context.get("creation_arguments") != supplied or context.get("creation_operator") != policy
                or context.get("creation_payload") != payload):
            raise CreationValidationError("Creation checkpoint context changed")
        return deepcopy(payload)

    def execute(state: dict[str, Any], preview: dict[str, Any], key: str) -> dict[str, Any]:
        if prepare(state) != preview:
            raise CreationValidationError("Creation preview differs from authorized request")
        return creation.create(preview, key)

    return build_lifecycle_graph(ports, (LifecycleStep(ACTION, prepare, execute),), name=ACTION,
                                 checkpointer=checkpointer, context_resolver=resolve,
                                 required_context=("repository", "creation_kind", "creation_payload"))