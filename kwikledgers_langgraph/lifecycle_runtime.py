from __future__ import annotations

import base64
from copy import deepcopy
import json
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.graphs.story_lifecycle import build_story_completion_graph, build_story_start_graph
from kwikledgers_langgraph.graphs.task_lifecycle import build_task_cycle_graph
from kwikledgers_langgraph.graphs.lifecycle_engine import LifecycleStep, build_lifecycle_graph
from kwikledgers_langgraph.lifecycle_adapters import LifecycleJournal, canonical


def build_technical_debt_execution_graph(ports: LifecyclePorts, *, checkpointer: Any,
                                        preview_key: str) -> Any:
    gateway = getattr(ports.work_items, "gateway", None)
    if gateway is None:
        raise ValueError("Technical debt execution requires operational work-item adapter")
    saved = gateway.journal.get(preview_key)
    if not saved or not isinstance(saved.get("technical_debt_preview"), dict):
        raise ValueError("Durable prepared debt preview required")
    prepared = deepcopy(saved["technical_debt_preview"])
    repository = saved["repository"]
    ports.branches.path(repository)
    if prepared.get("creation_blocked") is not False or prepared.get("exact_match") is not False:
        raise ValueError("Exact-match technical debt creation blocked")
    if prepared.get("repository_name") != repository or not prepared.get("children"):
        raise ValueError("Prepared debt repository/children invalid")

    def resolve(request: dict[str, Any]) -> dict[str, Any]:
        if request.get("repository_name") != repository:
            raise ValueError("Prepared debt belongs to another repository")
        if gateway.journal.get(preview_key) != saved:
            raise ValueError("Prepared debt evidence changed")
        return {"repository": repository, "debt_preview_key": preview_key}

    def item_step(index: int | None) -> LifecycleStep:
        action = "debt_parent" if index is None else f"debt_child_{index}"

        def prepare(state: dict[str, Any]) -> dict[str, Any]:
            item = deepcopy(prepared["parent"] if index is None else prepared["children"][index])
            fields = ("work_item_type", "title", "description", "iteration_path", "acceptance_criteria",
                      "story_points", "original_estimate_hours", "parent_work_item_id",
                      "assigned_to", "area_path", "tags")
            arguments = {field: item[field] for field in fields if field in item}
            arguments["repository_name"] = repository
            if index is not None:
                arguments["parent_work_item_id"] = state["results"]["debt_parent"]["id"]
            return arguments

        def execute(state: dict[str, Any], preview: dict[str, Any], key: str) -> dict[str, Any]:
            if prepare(state) != preview:
                raise ValueError("Prepared debt differs from independent approved item preview")
            result = gateway.execute(action, "create_work_item", preview, key)
            identifier = result.get("id", result.get("work_item_id"))
            if type(identifier) is not int or identifier <= 0:
                raise ValueError("Structured created work item ID required; reconcile")
            ports.tracking.record({"action": action, "phase": "created", "work_item_id": identifier,
                                   "parent_work_item_id": preview.get("parent_work_item_id")}, key=key + ":created")
            return {**result, "id": identifier}

        return LifecycleStep(action, prepare, execute)

    def confirmation_step(index: int) -> LifecycleStep:
        action = f"debt_child_{index}"

        def confirm(state: dict[str, Any]) -> dict[str, Any]:
            from kwikledgers_langgraph.work_item_creation import confirm_created_task_snapshot

            return confirm_created_task_snapshot(gateway, state["results"][action],
                key=f'{state["run_id"]}:{action}:snapshot_sync', event_source="langgraph_technical_debt")

        return LifecycleStep(f"{action}_snapshot", confirm, post_execution=True)

    steps = (item_step(None), *(step for index in range(len(prepared["children"]))
                               for step in (item_step(index), confirmation_step(index))))
    return build_lifecycle_graph(ports, steps,
                                 name="technical_debt_execute", checkpointer=checkpointer,
                                 context_resolver=resolve, required_context=("repository",))


def _encode_checkpoint(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (tuple, list)):
        return {"type": "tuple" if isinstance(value, tuple) else "list",
                "value": [_encode_checkpoint(item) for item in value]}
    if isinstance(value, dict):
        return {"type": "dict", "value": [[_encode_checkpoint(key), _encode_checkpoint(item)]
                                          for key, item in value.items()]}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError("Unsupported checkpoint value")


def _decode_checkpoint(value: Any) -> Any:
    if not isinstance(value, dict):
        if value is None or type(value) in (str, int, float, bool):
            return value
        raise ValueError("Invalid checkpoint value")
    if set(value) != {"type", "value"}:
        raise ValueError("Invalid checkpoint encoding")
    kind, data = value["type"], value["value"]
    if kind == "bytes" and isinstance(data, str):
        return base64.b64decode(data, validate=True)
    if not isinstance(data, list):
        raise ValueError("Invalid checkpoint collection")
    if kind == "tuple":
        return tuple(_decode_checkpoint(item) for item in data)
    if kind == "list":
        return [_decode_checkpoint(item) for item in data]
    if kind == "dict":
        result = {}
        for pair in data:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("Invalid checkpoint mapping")
            key, item = pair
            decoded = _decode_checkpoint(key)
            if decoded in result:
                raise ValueError("Duplicate checkpoint key")
            result[decoded] = _decode_checkpoint(item)
        return result
    raise ValueError("Unknown checkpoint encoding")


class SignedLifecycleCheckpointer(MemorySaver):
    def __init__(self, journal: LifecycleJournal) -> None:
        super().__init__()
        self.journal = journal
        self.journal.db.execute("CREATE TABLE IF NOT EXISTS lifecycle_checkpoint (id INTEGER PRIMARY KEY CHECK(id = 1), payload TEXT NOT NULL, signature TEXT NOT NULL)")
        self.journal.db.commit()
        row = self.journal.db.execute("SELECT payload, signature FROM lifecycle_checkpoint WHERE id = 1").fetchone()
        if row is not None:
            if not row[0].startswith("json:"):
                raise ValueError("Checkpoint legado não é aceito. Gere uma nova prévia.")
            expected = journal._signature("checkpoint", row[0])
            if expected != row[1]:
                raise ValueError("Checkpoint antigo ou corrompido. Gere uma nova prévia.")
            restored = _decode_checkpoint(json.loads(row[0][5:]))
            if not isinstance(restored, tuple) or len(restored) != 3 or any(not isinstance(item, dict) for item in restored):
                raise ValueError("Invalid checkpoint state")
            storage, writes, blobs = restored
            for thread_id, namespaces in storage.items():
                self.storage[thread_id].update(namespaces)
            self.writes.update(writes)
            self.blobs.update(blobs)

    def _persist(self) -> None:
        storage = {thread_id: dict(namespaces) for thread_id, namespaces in self.storage.items()}
        payload = "json:" + canonical(_encode_checkpoint((storage, dict(self.writes), dict(self.blobs))))
        signature = self.journal._signature("checkpoint", payload)
        with self.journal.db:
            self.journal.db.execute("INSERT OR REPLACE INTO lifecycle_checkpoint VALUES (1, ?, ?)", (payload, signature))

    def put(self, *args: Any, **kwargs: Any) -> Any:
        with self.journal.lock:
            result = super().put(*args, **kwargs)
            self._persist()
            return result

    def put_writes(self, *args: Any, **kwargs: Any) -> Any:
        with self.journal.lock:
            result = super().put_writes(*args, **kwargs)
            self._persist()
            return result


LIFECYCLE_BUILDERS: dict[str, Callable[..., Any]] = {
    "story_start": build_story_start_graph,
    "story_completion": build_story_completion_graph,
    "task_cycle": build_task_cycle_graph,
    "technical_debt_execute": build_technical_debt_execution_graph,
}


def build_lifecycle_runtime(name: str, ports: LifecyclePorts, *, checkpointer: Any,
                            preview_key: str | None = None) -> Any:
    if name == "technical_debt_execute":
        if not preview_key:
            raise ValueError("Technical debt execution requires a durable prepared preview key")
        return build_technical_debt_execution_graph(ports, checkpointer=checkpointer, preview_key=preview_key)
    return LIFECYCLE_BUILDERS[name](ports, checkpointer=checkpointer)