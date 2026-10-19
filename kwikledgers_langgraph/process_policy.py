from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from html import escape
import re
from typing import Any
import unicodedata


def _normalized(value: str) -> str:
    return "".join(character for character in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(character))


def classify_scope(task: dict[str, Any]) -> str:
    text = _normalized(f'{task.get("title", "")} {task.get("nature", "")} {task.get("repository", "").replace("_", " ")}')
    if re.search(r"\b(implantacao|deployment|deploy|rollout|publicacao)\b", text):
        return "operational"
    if re.search(r"\b(frontend|front-end|front end|angular|ionic)\b", text):
        return "frontend"
    return "technical"


def analyze_snapshot(items: list[dict[str, Any]]) -> dict[str, Any]:
    unique: dict[int, dict[str, Any]] = {}

    def collect(item: dict[str, Any]) -> None:
        identifier = item["id"]
        if identifier in unique:
            return
        unique[identifier] = item
        for child in item.get("children", []):
            collect(child)

    for item in items:
        collect(item)
    scopes: dict[str, list[int]] = {"technical": [], "frontend": [], "operational": []}
    blocked, returned, tasks = [], [], []
    for item in unique.values():
        identifier = item["id"]
        status = _normalized(item.get("status", ""))
        tags = item.get("tags", [])
        if isinstance(tags, str):
            tags = tags.split(";")
        tags = {_normalized(tag.strip()) for tag in tags}
        if item.get("blocked") is True or status in {"blocked", "bloqueado", "impedido"} or tags & {"blocked", "bloqueado", "impedido"}:
            blocked.append(identifier)
        if item.get("returned") is True or status in {"returned", "devolvido"} or tags & {"returned", "devolvido"}:
            returned.append(identifier)
        if item.get("type") == "Task":
            tasks.append(item)
            scopes[classify_scope(item)].append(identifier)
    return {"blocked_ids": blocked, "returned_ids": returned, "scope": scopes,
            "type_counts": dict(Counter(item.get("type", "Unknown") for item in unique.values())),
            "task_status_counts": dict(Counter(item.get("status", "Unknown") for item in tasks)),
            "task_ids": [item["id"] for item in tasks]}


def coordinate_deadlines(snapshot: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("Deadline clock must be timezone-aware")
    reminders = []
    sprint_end = snapshot.get("sprint_end")
    if sprint_end:
        end = datetime.fromisoformat(sprint_end.replace("Z", "+00:00"))
        if end.tzinfo is None:
            raise ValueError("Sprint end must be timezone-aware")
        days = (end.astimezone(now.tzinfo).date() - now.date()).days
        if 0 <= days <= 3:
            reminders.append({"kind": "sprint_end", "days": days,
                              "cadence_hours": 2 if days == 1 else 24})
    for pull_request in snapshot.get("pull_requests", []):
        created = datetime.fromisoformat(pull_request["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("PR timestamp must be timezone-aware")
        if now - created > timedelta(days=2) and pull_request.get("reviewed") is False:
            reminders.append({"kind": "unreviewed_pr", "pr_id": pull_request["id"],
                              "author": pull_request["author"]})
    return reminders


def _fibonacci(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Effort must be a positive Fibonacci integer")
    previous, current = 1, 1
    while current < value:
        previous, current = current, previous + current
    if current != value:
        raise ValueError("Effort must be Fibonacci")
    return value


def _text(item: dict[str, Any], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Required text: {field}")
    return value.strip()


def _section(title: str, value: str) -> str:
    return f"<h3>{escape(title)}</h3><p>{escape(value).replace(chr(10), '<br>')}</p>"


def _debt_item(item: dict[str, Any], iteration: str, *, child: bool, task_acceptance_supported: bool) -> dict[str, Any]:
    description = "".join(_section(title, _text(item, field)) for title, field in (
        ("Contexto", "context"), ("Problema", "problem"), ("Impacto", "impact"), ("Abordagem", "approach"),
    ))
    acceptance = _section("Critérios de aceite", _text(item, "acceptance_criteria"))
    if child:
        description += _section("Sugestão ao dev", _text(item, "developer_suggestion"))
    if child and not task_acceptance_supported:
        description += acceptance
    result = {"work_item_type": "Task" if child else "Technical Debt", "title": _text(item, "title"),
              "description": description, "iteration_path": iteration,
              "observations": item.get("observations", ""),
              "effort_is_suggestion": "original_estimate_hours" not in item if child else "story_points" not in item}
    if not child or task_acceptance_supported:
        result["acceptance_criteria"] = acceptance
    for field in ("assigned_to", "area_path", "tags"):
        if field in item:
            result[field] = _text(item, field)
    if child:
        result.update({"original_estimate_hours": _fibonacci(item.get("original_estimate_hours", 1)),
                       "parent_work_item_id": "$created_parent"})
    else:
        result["story_points"] = _fibonacci(item.get("story_points", 1))
        if "parent_work_item_id" in item:
            if type(item["parent_work_item_id"]) is not int or item["parent_work_item_id"] <= 0:
                raise ValueError("Invalid parent work item")
            result["parent_work_item_id"] = item["parent_work_item_id"]
    return result


def normalize_debt_title(repository: str, title: str, *, allow_grave: bool = False) -> str:
    if not allow_grave and re.search(r"\bgrave\b", _normalized(title)):
        raise ValueError("Severity 'grave' requires an explicit user request")
    if repository == "kl_store" and not title.startswith("[KL STORE] "):
        return f"[KL STORE] {title}"
    return title


def prepare_technical_debt(request: dict[str, Any], similarity: dict[str, Any], context_path: str) -> dict[str, Any]:
    repository = _text(request, "repository_name")
    if request.get("target_iteration") == "next_sprint":
        number, year = request.get("current_sprint_number"), request.get("current_year")
        if type(number) is not int or number <= 0 or type(year) is not int or year < 2000:
            raise ValueError("Authoritative current sprint/year required")
        iteration = f'{_text(request, "iteration_root")}\\{year}\\Sprint {number + 1}'
    else:
        iteration = _text(request, "iteration_path")
    if type(similarity.get("exact_match")) is not bool or not isinstance(similarity.get("similar_items"), list):
        raise ValueError("Similarity lookup must include exact_match and similar_items")
    if not context_path or not request.get("children"):
        raise ValueError("Repository context and planned children required")
    parent_input = dict(request["parent"])
    parent_input["title"] = normalize_debt_title(repository, _text(parent_input, "title"),
                                                allow_grave=request.get("allow_grave") is True)
    parent = _debt_item(parent_input, iteration, child=False, task_acceptance_supported=True)
    children = [_debt_item(child, iteration, child=True,
                          task_acceptance_supported=request.get("task_acceptance_supported") is True)
                for child in request["children"]]
    return {"repository_name": repository, "parent": parent, "children": children,
            "exact_match": similarity["exact_match"], "similar_items": similarity["similar_items"],
            "repository_context_path": context_path, "creation_blocked": similarity["exact_match"],
            "approval_required": True}