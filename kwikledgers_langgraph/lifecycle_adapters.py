from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
from threading import RLock
from typing import Any, Callable, Iterator, Mapping

from kwikledgers_langgraph.adapters import LifecyclePorts, TechnicalDebtPreparationPorts
from kwikledgers_langgraph.fastmcp_client import CapabilityClient, FastMCPClient
from kwikledgers_langgraph.fastmcp_execution_capability import INTERNAL_RUNTIME_CAPABILITIES


CapabilityIssuer = Callable[..., str]
_ACTIVE: ContextVar[dict[str, Any] | None] = ContextVar("lifecycle_execution", default=None)
_APPROVALS: ContextVar[Any] = ContextVar("lifecycle_approvals", default=None)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def approval_binding(pending: dict[str, Any]) -> dict[str, Any]:
    binding = {field: deepcopy(pending[field]) for field in
               ("action", "preview", "run_id", "request_id", "preview_digest")}
    binding["approved"] = False
    if "previewed_at" in pending:
        binding["previewed_at"] = pending["previewed_at"]
    return binding


@contextmanager
def lifecycle_scope(state: dict[str, Any], approvals: Any = None) -> Iterator[None]:
    trusted = deepcopy(state)
    run_id = trusted.get("run_id")
    if not run_id or trusted.get("observability", {}).get("active_run_id") != run_id:
        raise ValueError("Lifecycle requires an active run")
    if approvals is not None:
        pending = trusted.get("pending_approval", {})
        binding = approval_binding(pending)
        digest = sha256(json.dumps(binding["preview"], sort_keys=True, allow_nan=False).encode()).hexdigest()
        if (pending.get("approved") is not True or binding["run_id"] != run_id
                or binding["preview_digest"] != digest
                or "receipt" in pending
                or approvals.verify(binding, pending.get("confirmation")) is not True):
            raise ValueError("Verified explicit lifecycle approval required")
        trusted["verified_lifecycle_binding"] = binding
        confirmation = pending["confirmation"]
        information = confirmation if isinstance(confirmation, dict) else {}
        trusted["verified_lifecycle_approval"] = {
            "method": "local_explicit" if information.get("source") == "human" else "custom",
            "decision": information.get("decision"), "subject": information.get("subject"),
            "confirmed_at": information.get("confirmed_at", information.get("issued_at")),
        }
    else:
        trusted.pop("verified_lifecycle_binding", None)
        trusted.pop("verified_lifecycle_approval", None)
    token = _ACTIVE.set(trusted)
    approval_token = _APPROVALS.set(approvals)
    try:
        yield
    finally:
        _APPROVALS.reset(approval_token)
        _ACTIVE.reset(token)


def active_state(action: str | None = None) -> dict[str, Any]:
    state = _ACTIVE.get()
    if state is None:
        raise ValueError("Operation must run inside lifecycle engine")
    if action is not None:
        binding = state.get("verified_lifecycle_binding", {})
        approvals = _APPROVALS.get()
        pending = state.get("pending_approval", {})
        if (binding.get("action") != action or approvals is None
                or pending.get("approved") is not True
                or "receipt" in pending
                or approvals.verify(binding, pending.get("confirmation")) is not True):
            raise ValueError("Operation lacks an independently approved process stage")
    return deepcopy(state)


def require_approved_preview(action: str, preview: dict[str, Any]) -> None:
    state = active_state(action)
    approved = state["verified_lifecycle_binding"]["preview"]
    if canonical(approved) != canonical(preview):
        raise ValueError("Operation differs from the independently approved preview")


def validate_lifecycle_execution(state: dict[str, Any], *, action: str,
                                 capability_name: str, arguments: dict[str, Any]) -> None:
    binding = state.get("verified_lifecycle_binding", {})
    if binding.get("action") != action:
        raise ValueError("Operation differs from approved lifecycle action")
    preview = binding.get("preview", {})
    try:
        if action == "activate":
            name = "update_work_item_content"
            expected = {field: preview[field] for field in
                        ("assigned_to", "area_path", "status", "expected_revision")}
            expected["work_item_id"] = preview["task_id"]
        elif action == "awaiting_pr":
            name = "update_story_status"
            expected = {"story_id": preview["story_id"], "status": preview["target_status"],
                        "expected_revision": preview["expected_revision"]}
        elif action in ("hours", "close"):
            name = "update_work_item_effort"
            expected = {"work_item_id": preview["task_id"],
                        "completed_work_hours": preview["completed_work_hours"],
                        "expected_revision": preview["expected_revision"]}
            if action == "close":
                expected["status"] = "Closed"
        elif action == "branch_links":
            name = "associate_story_branch"
            expected = {"story_id": preview["story_id"], "repository_name": preview["repository"],
                        "source_branch": preview["name"], "related_work_item_ids": preview["associated_ids"],
                        "include_child_work_items": True, "expected_revisions": preview["expected_revisions"]}
        elif action == "draft_pr":
            name = "create_story_pull_request"
            if preview["expected_revisions"].get(str(preview["story_id"])) != preview["expected_revision"]:
                raise ValueError("Approved parent revision conflicts with native revisions")
            expected = {**preview["native_arguments"], "expected_revision": preview["expected_revision"],
                        "expected_revisions": preview["expected_revisions"]}
        elif action == "work_item_creation" or action == "debt_parent" or re.fullmatch(r"debt_child_(0|[1-9][0-9]*)", action):
            name = "create_work_item"
            expected = preview
        elif action.startswith("atomic_"):
            from kwikledgers_langgraph.atomic_runtime import ATOMIC_ACTION_CAPABILITIES, validate_arguments

            name = ATOMIC_ACTION_CAPABILITIES.get(action)
            if name is None:
                raise ValueError("Lifecycle action has no authorized FastMCP capability")
            expected = validate_arguments(name, preview, technical=True)
        else:
            raise ValueError("Lifecycle action has no authorized FastMCP capability")
        if capability_name != name or canonical(arguments) != canonical(expected):
            raise ValueError("Operation differs from approved technical arguments")
    except (KeyError, TypeError) as error:
        raise ValueError("Approved lifecycle technical arguments missing") from error


class SignedReceiptApproval:
    def __init__(self, key: bytes, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def verify(self, pending: dict[str, Any], confirmation: Any) -> bool:
        return False


class ExplicitLocalApproval:
    def __init__(self, key: bytes | None = None, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def confirm(self, pending: dict[str, Any], request_id: str, *, subject: str = "local-operator") -> dict[str, Any]:
        if (pending.get("approved") is not False or not isinstance(request_id, str)
                or not request_id.strip() or pending.get("request_id") != request_id):
            raise ValueError("Explicit approval must target the current unapproved request")
        confirmation = {**approval_binding(pending), "decision": "approve", "source": "human",
                        "subject": subject, "confirmed_at": self.clock().isoformat()}
        if not self.verify(pending, confirmation):
            raise ValueError("Explicit approval binding invalid")
        return confirmation

    def verify(self, pending: dict[str, Any], confirmation: Any) -> bool:
        if isinstance(confirmation, dict) and any(field in confirmation for field in ("claims", "signature", "receipt")):
            return False
        try:
            if (not isinstance(confirmation, dict) or confirmation.get("decision") != "approve"
                    or confirmation.get("source") != "human" or confirmation.get("approved") is not False
                    or not isinstance(confirmation.get("subject"), str) or not confirmation["subject"].strip()):
                return False
            binding = approval_binding(pending)
            if any(not isinstance(binding[field], str) or not binding[field].strip()
                   for field in ("action", "run_id", "request_id", "preview_digest")):
                return False
            digest = sha256(json.dumps(binding["preview"], sort_keys=True, allow_nan=False).encode()).hexdigest()
            if binding["preview_digest"] != digest or canonical(approval_binding(confirmation)) != canonical(binding):
                return False
            previewed = datetime.fromisoformat(binding["previewed_at"])
            confirmed = datetime.fromisoformat(confirmation["confirmed_at"])
            now = self.clock()
            return (previewed.tzinfo is not None and confirmed.tzinfo is not None and now.tzinfo is not None
                    and previewed <= confirmed <= now)
        except (KeyError, TypeError, ValueError, OverflowError):
            return False


class LifecycleJournal:
    def __init__(self, path: str | Path, key: bytes | None = None) -> None:
        self.key = None
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise ValueError("Lifecycle journal requires an owner-controlled local directory")
        handle = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(handle)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o022):
                raise ValueError("Lifecycle journal requires an owner-controlled local file")
            os.fchmod(handle, 0o600)
        finally:
            os.close(handle)
        self.lock = RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS lifecycle_evidence (key TEXT PRIMARY KEY, payload TEXT NOT NULL, signature TEXT NOT NULL)")
        self.db.commit()

    def _signature(self, key: str, payload: str) -> str:
        data = (key + "\n" + payload).encode()
        return "sha256:" + sha256(data).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            return self._get(key)

    def _get(self, key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT payload, signature FROM lifecycle_evidence WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        if row[1] != self._signature(key, row[0]):
            raise ValueError("Evidência antiga ou corrompida. Gere uma nova prévia.")
        return json.loads(row[0])

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self.lock:
            self._put(key, value)

    def _put(self, key: str, value: dict[str, Any]) -> None:
        payload = canonical(value)
        previous = self.get(key)
        if previous is not None:
            if canonical(previous) != payload:
                raise ValueError("Idempotency key reused with different evidence")
            return
        with self.db:
            self.db.execute("INSERT INTO lifecycle_evidence VALUES (?, ?, ?)",
                            (key, payload, self._signature(key, payload)))

    def entries(self) -> list[dict[str, Any]]:
        with self.lock:
            keys = self.db.execute("SELECT key FROM lifecycle_evidence ORDER BY rowid").fetchall()
            return [self.get(row[0]) for row in keys]

    def once(self, key: str, arguments: dict[str, Any], operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            return self._once(key, arguments, operation)

    def _once(self, key: str, arguments: dict[str, Any], operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        digest = sha256(canonical(arguments).encode()).hexdigest()
        result = self.get(key + ":result")
        if result is not None:
            if result["digest"] != digest:
                raise ValueError("Idempotency payload changed")
            return deepcopy(result["result"])
        if self.get(key + ":intent") is not None:
            raise RuntimeError("Execution outcome unknown; reconcile before retry")
        intent = canonical({"digest": digest})
        try:
            with self.db:
                self.db.execute("INSERT INTO lifecycle_evidence VALUES (?, ?, ?)",
                                (key + ":intent", intent, self._signature(key + ":intent", intent)))
        except sqlite3.IntegrityError:
            raise RuntimeError("Execution already reserved; reconcile before retry") from None
        value = operation()
        if not isinstance(value, dict):
            raise ValueError("Operation requires structured result")
        self.put(key + ":result", {"digest": digest, "result": value})
        return value

    def close(self) -> None:
        self.db.close()


class LifecycleGateway:
    def __init__(self, client: CapabilityClient, journal: LifecycleJournal,
                 execution_issuer: CapabilityIssuer | None, runtime_issuer: CapabilityIssuer | None) -> None:
        self.client, self.journal = client, journal
        self.execution_issuer, self.runtime_issuer = execution_issuer, runtime_issuer

    def execute(self, action: str, name: str, arguments: dict[str, Any], key: str) -> dict[str, Any]:
        state = active_state(action)
        arguments = deepcopy(arguments)
        validate_lifecycle_execution(state, action=action, capability_name=name, arguments=arguments)
        if self.execution_issuer is None:
            raise RuntimeError("MAIN must provide issue_lifecycle_execution_capability")
        def invoke() -> dict[str, Any]:
            state = active_state(action)
            validate_lifecycle_execution(state, action=action, capability_name=name, arguments=arguments)
            token = self.execution_issuer(state, action=action, capability_name=name, arguments=deepcopy(arguments))
            if not isinstance(token, str) or not token.strip():
                raise ValueError("Execution capability missing")
            active_state(action)
            return self.client.call(name, {**arguments, "execution_capability": token})
        return self.journal.once(key, arguments, invoke)

    def internal(self, name: str, arguments: dict[str, Any], key: str) -> dict[str, Any]:
        if name not in INTERNAL_RUNTIME_CAPABILITIES:
            raise ValueError("Internal lifecycle capability is not allowed")
        state = active_state()
        state.pop("verified_lifecycle_binding", None)
        state.pop("verified_lifecycle_approval", None)
        state.pop("pending_approval", None)
        state.pop("approval_confirmation", None)
        arguments = deepcopy(arguments)
        if self.runtime_issuer is None:
            raise RuntimeError("MAIN must provide issue_runtime_execution_capability")
        def invoke() -> dict[str, Any]:
            token = self.runtime_issuer(state, action="lifecycle_tracking", capability_name=name,
                                        arguments=deepcopy(arguments))
            if not isinstance(token, str) or not token.strip():
                raise ValueError("Runtime capability missing")
            return self.client.call(name, {**arguments, "execution_capability": token})
        return self.journal.once(key, arguments, invoke)


class UTCClock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class MCPTracking:
    def __init__(self, gateway: LifecycleGateway) -> None:
        self.gateway = gateway

    def record(self, event: dict[str, Any], *, key: str) -> None:
        state = active_state()
        context = state.get("context", {})
        payload = {**context, **event, "run_id": state["run_id"]}
        previous = self.gateway.journal.get(key + ":event")
        if previous is not None:
            stable = lambda value: {field: data for field, data in value.items() if field not in ("timestamp", "started_at")}
            if stable(previous) != stable(payload):
                raise ValueError("Tracking idempotency payload changed")
            return
        self.gateway.internal("append_daily_action_log", {
            "title": str(event.get("action", "lifecycle")), "details": canonical(payload),
            "source": "langgraph_lifecycle",
        }, key + ":native_tracking")
        self.gateway.journal.put(key + ":event", payload)

    def started_at(self, repository: str, task_id: int) -> str:
        events = [item for item in self.gateway.journal.entries()
                  if item.get("repository") == repository and item.get("task_id") == task_id
                  and item.get("action") == "story_start" and item.get("phase") == "started"]
        if not events:
            raise ValueError("Durable story start timestamp missing")
        return events[-1]["started_at"]

    def story_start_complete(self, context: dict[str, Any]) -> bool:
        events = [item for item in self.gateway.journal.entries()
                  if item.get("repository") == context["repository"]
                  and item.get("story_id") == context["story_id"] and item.get("task_id") == context["task_id"]]
        starts = [item for item in events if item.get("action") == "story_start" and item.get("phase") == "started"]
        if not starts:
            return False
        run_id = starts[-1]["run_id"]
        return all(any(item.get("run_id") == run_id and item.get("action") == action
                       and item.get("phase") == phase for item in events)
                   for action, phase in (("activate", "executed"), ("branch", "executed"),
                                         ("branch_links", "executed"), ("story_start", "completed")))


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(("git", "-C", str(repository), *arguments), check=True,
                            capture_output=True, text=True, timeout=60)
    return result.stdout.strip()


class GitBranches:
    def __init__(self, repositories: Mapping[str, Path], gateway: LifecycleGateway) -> None:
        self.repositories = {name: Path(path).resolve() for name, path in repositories.items()}
        self.gateway = gateway

    def path(self, name: str) -> Path:
        if name not in self.repositories:
            raise ValueError("Repository not authorized by operator")
        return self.repositories[name]

    def _owned(self, repository: Path, story_id: int) -> list[str]:
        names = git(repository, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
        return [name for name in names if re.fullmatch(rf"(feature|hotfix)/KL-{story_id}-[a-z0-9]+(?:-[a-z0-9]+)*", name)]

    def preview(self, context: dict[str, Any], branch_type: str) -> dict[str, Any]:
        if branch_type not in ("feature", "hotfix"):
            raise ValueError("Invalid branch type")
        repository = self.path(context["repository"])
        remote = git(repository, "ls-remote", "--exit-code", "origin", "refs/heads/stage-pre-prod").split()
        if len(remote) != 2 or remote[1] != "refs/heads/stage-pre-prod":
            raise ValueError("Exact origin/stage-pre-prod missing")
        owned = self._owned(repository, context["story_id"])
        if len(owned) > 1:
            raise ValueError("Ambiguous parent story branch")
        slug = re.sub(r"[^a-z0-9]+", "-", context["title"].lower()).strip("-") or "story"
        name = owned[0] if owned else f'{branch_type}/KL-{context["story_id"]}-{slug}'
        provenance = self.gateway.journal.get(f'branch:{context["repository"]}:{context["story_id"]}')
        if owned and (not provenance or provenance.get("name") != name):
            raise ValueError("Existing branch has no authenticated parent provenance")
        return {"name": name, "base": "stage-pre-prod", "remote_head": remote[0],
                "reuse": bool(owned), "dirty": bool(git(repository, "status", "--porcelain")),
                "repository": context["repository"], "story_id": context["story_id"],
                "checkout_head": git(repository, "rev-parse", "HEAD"),
                "branch_head": git(repository, "rev-parse", name) if owned else None}

    def create_or_reuse(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        state = active_state("branch")
        if state["verified_lifecycle_binding"]["preview"] != preview:
            raise ValueError("Git operation differs from approved preview")
        repository = self.path(preview["repository"])
        context = state["context"]
        if self.preview(context, preview["name"].split("/")[0]) != preview or preview["dirty"]:
            raise ValueError("Checkout or remote changed; no automatic stash")
        def execute() -> dict[str, Any]:
            active_state("branch")
            git(repository, "fetch", "--no-tags", "origin", "+refs/heads/stage-pre-prod:refs/remotes/origin/stage-pre-prod")
            if git(repository, "rev-parse", "refs/remotes/origin/stage-pre-prod") != preview["remote_head"]:
                raise ValueError("Fetched base differs from approved origin HEAD")
            active_state("branch")
            if preview["reuse"]:
                git(repository, "merge-base", "--is-ancestor", preview["remote_head"], preview["name"])
                git(repository, "switch", preview["name"])
            else:
                git(repository, "switch", "-c", preview["name"], preview["remote_head"])
                self.gateway.journal.put(f'branch:{preview["repository"]}:{preview["story_id"]}', preview)
            return {"name": preview["name"], "remote_head": preview["remote_head"], "reuse": preview["reuse"]}
        return self.gateway.journal.once(key, preview, execute)

    def links(self, context: dict[str, Any]) -> dict[str, Any]:
        owned = self._owned(self.path(context["repository"]), context["story_id"])
        if len(owned) != 1:
            raise ValueError("Exactly one parent story branch required")
        provenance = self.gateway.journal.get(f'branch:{context["repository"]}:{context["story_id"]}')
        if not provenance or provenance.get("name") != owned[0]:
            raise ValueError("Existing branch has no authenticated parent provenance")
        result = self.gateway.client.call("preview_story_branch_association", {
            "story_id": context["story_id"], "repository_name": context["repository"],
            "source_branch": owned[0], "related_work_item_ids": context["child_ids"],
            "include_child_work_items": True,
        })
        identifiers = result.get("associated_ids")
        if identifiers is None and isinstance(result.get("work_items"), list):
            identifiers = [item["id"] for item in result["work_items"] if item.get("branch_linked") is True]
        if not isinstance(identifiers, list) or any(type(item) is not int for item in identifiers):
            raise ValueError("Structured existing associated_ids required")
        return {"name": owned[0], "associated_ids": identifiers,
            "expected_revisions": result.get("expected_revisions", {})}

    def associate(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        arguments = {"story_id": preview["story_id"], "repository_name": preview["repository"],
                     "source_branch": preview["name"], "related_work_item_ids": preview["associated_ids"],
                     "include_child_work_items": True}
        revisions = preview.get("expected_revisions")
        if not isinstance(revisions, dict) or set(revisions) != {str(item) for item in preview["associated_ids"]}:
            raise ValueError("Approved native revisions for every associated item required")
        arguments["expected_revisions"] = revisions
        return self.gateway.execute("branch_links", "associate_story_branch", arguments, key)


class MCPWorkItems:
    def __init__(self, gateway: LifecycleGateway) -> None:
        self.gateway = gateway

    def activate(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        arguments = {field: preview[field] for field in ("assigned_to", "area_path", "status", "expected_revision")}
        arguments["work_item_id"] = preview["task_id"]
        return self.gateway.execute("activate", "update_work_item_content", arguments, key)

    def awaiting_pr(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        arguments = {"story_id": preview["story_id"], "status": preview["target_status"],
                     "expected_revision": preview["expected_revision"]}
        return self.gateway.execute("awaiting_pr", "update_story_status", arguments, key)

    def hours(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        arguments = {"work_item_id": preview["task_id"], "completed_work_hours": preview["completed_work_hours"],
                     "expected_revision": preview["expected_revision"]}
        return self.gateway.execute("hours", "update_work_item_effort", arguments, key)

    def close(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        arguments = {"work_item_id": preview["task_id"], "completed_work_hours": preview["completed_work_hours"],
                     "status": "Closed", "expected_revision": preview["expected_revision"]}
        return self.gateway.execute("close", "update_work_item_effort", arguments, key)


def native_active_user_identity(user: dict[str, Any]) -> str:
    identity = user.get("id") or user.get("email")
    if not identity:
        match = re.fullmatch(r"Usuario ativo:\s*(?:[^<>]*<([^<>\s]+@[^<>\s]+)>|([^<>\s]+@[^<>\s]+))",
                            str(user.get("message", "")))
        identity = (match.group(1) or match.group(2)) if match else None
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("Authoritative active user identity required")
    return identity


class MCPContext:
    def __init__(self, gateway: LifecycleGateway, branches: GitBranches, *, project: str, area: str) -> None:
        self.gateway, self.branches = gateway, branches
        self.project, self.area = project, area

    def _details(self, identifier: int) -> dict[str, Any]:
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("Positive work item ID required")
        data = self.gateway.client.call("get_story_details", {"story_id": identifier})
        data = {**data, "revision": data.get("revision", data.get("rev")),
            "status": data.get("status", data.get("state", data.get("fields", {}).get("System.State")))}
        if "child_ids" not in data and isinstance(data.get("child_work_items"), list):
            data["child_ids"] = [child["id"] for child in data["child_work_items"]]
        if data.get("id", data.get("story_id")) != identifier:
            raise ValueError("Native details belong to a different item")
        if type(data.get("revision")) is not int or data["revision"] <= 0 or not data.get("status"):
            raise ValueError("Structured native revision and status required")
        return data

    def _authorized_details(self, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[int]]:
        repository = request["repository_name"]
        self.branches.path(repository)
        story = self._details(request["story_id"])
        task = self._details(request["task_id"])
        association = self.gateway.client.call("preview_story_branch_association", {
            "story_id": request["story_id"], "repository_name": repository,
            "source_branch": f"feature/KL-{request['story_id']}-context-validation",
            "include_child_work_items": True,
        })
        if (association.get("story_id") != request["story_id"]
                or association.get("repository_name") != repository or association.get("project") != self.project
                or story.get("repository_name", repository) != repository
                or story.get("project", self.project) != self.project):
            raise ValueError("Story repository/project authorization missing")
        items = association.get("work_items")
        revisions = association.get("expected_revisions")
        if not isinstance(items, list) or not isinstance(revisions, dict):
            raise ValueError("Authoritative relationship and revision evidence required")
        children = [item["id"] for item in items if item.get("relationship_to_story") == "child"]
        parents = [item["id"] for item in items if item.get("relationship_to_story") == "parent"]
        if (parents != [request["story_id"]]
                or any(type(identifier) is not int or identifier <= 0 for identifier in children)
                or len(children) != len(set(children)) or request["task_id"] not in children
                or task.get("parent_id", request["story_id"]) not in (None, request["story_id"])
                or set(story.get("child_ids", children)) != set(children)):
            raise ValueError("Task must be an authoritative child of this story")
        for details in (story, task):
            identifier = details.get("id", details.get("story_id"))
            revision = revisions.get(str(identifier))
            if type(revision) is not int or revision != details["revision"]:
                raise ValueError("Work item changed during native authorization")
        return story, task, children

    def resolve(self, request: dict[str, Any]) -> dict[str, Any]:
        repository = request["repository_name"]
        story, task, children = self._authorized_details(request)
        identity = native_active_user_identity(self.gateway.client.call("get_active_user", {}))
        if not self.project or not story.get("title"):
            raise ValueError("Active user/project/title required")
        return {"story_id": request["story_id"], "task_id": request["task_id"], "child_ids": children,
                "repository": repository, "project": self.project, "area": self.area,
                "user_id": identity, "title": story["title"], "revision": story["revision"], "status": story["status"],
                "story_details": story, "task_details": task,
                "hierarchy_context": task.get("hierarchy_context", story.get("hierarchy_context", {
                    "ancestors": [], "missing_levels": ["Feature", "Epic"],
                    "issues": [{"code": "hierarchy_context_unavailable"}], "complete": False,
                }))}

    def snapshot(self, context: dict[str, Any]) -> dict[str, Any]:
        for field, current in (("project", self.project), ("area", self.area)):
            if field in context and context[field] != current:
                raise ValueError("Operator configuration changed; obtain a new preview and receipt")
        story, task, children = self._authorized_details({
            "story_id": context["story_id"], "task_id": context["task_id"], "repository_name": context["repository"],
        })
        state = active_state()
        completion = state.get("observability", {}).get("flow_name") == "story_completion"
        evidence = self.gateway.journal.get(f'task:{context["repository"]}:{context["task_id"]}:commit')
        ready = bool(evidence and evidence.get("tests_passed") and evidence.get("commit"))
        up_to_date = False
        if ready:
            repository = self.branches.path(context["repository"])
            try:
                head = git(repository, "rev-parse", "HEAD")
                remote = git(repository, "ls-remote", "--exit-code", "origin", "refs/heads/stage-pre-prod").split()[0]
                git(repository, "merge-base", "--is-ancestor", remote, head)
                up_to_date = head == evidence["commit"] and not git(repository, "status", "--porcelain")
            except (subprocess.SubprocessError, IndexError):
                up_to_date = False
        return {"revision": story["revision"] if completion else task["revision"],
                "status": story["status"], "task_status": task["status"], "child_ids": children,
                "implementation_complete": ready, "tests_passed": ready, "base_up_to_date": up_to_date}


class MCPPullRequests:
    def __init__(self, gateway: LifecycleGateway, branches: GitBranches) -> None:
        self.gateway, self.branches = gateway, branches

    def preview(self, context: dict[str, Any], branch: str) -> dict[str, Any]:
        repository = self.branches.path(context["repository"])
        paths = [repository / "pull_request_template.md", repository / ".github" / "pull_request_template.md"]
        template = next((path for path in paths if path.is_file() and path.resolve().is_relative_to(repository)), None)
        if template is None:
            raise ValueError("Repository pull_request_template.md missing")
        body = template.read_text(encoding="utf-8") + "\n```mermaid\nflowchart LR\n  Story --> DraftPR\n  DraftPR --> Review\n```\n"
        arguments = {"story_id": context["story_id"], "repository_name": context["repository"],
                 "source_branch": branch, "target_branch": "stage-pre-prod", "title": context["title"],
                 "related_work_item_ids": context["child_ids"], "include_child_work_items": True,
                 "what_was_changed": body, "affected_processes": context["title"],
                 "expected_impacts": "See approved repository template.",
                 "important_points": "Draft; review required.", "tests_updated": True,
                 "mermaid_diagram": "flowchart LR\n  Story --> DraftPR\n  DraftPR --> Review"}
        native = self.gateway.client.call("preview_story_pull_request", arguments)
        if native.get("existing_active_pull_request") is not None or native.get("can_open_pull_request") is not True:
            raise ValueError("Existing PR requires reconciliation before a new preview")
        association = native.get("branch_association_preview", {})
        targets = {context["story_id"], *context["child_ids"]}
        linked = {item["id"] for item in association.get("work_items", []) if item.get("branch_linked") is True}
        if (association.get("project") != context["project"]
                or association.get("repository_name") != context["repository"]
                or association.get("story_id") != context["story_id"] or linked != targets):
            raise ValueError("Parent and children must already be linked to the approved branch")
        native_body = native.get("body_markdown")
        if not isinstance(native_body, str) or not native_body:
            raise ValueError("Structured native PR body required")
        return {"repository": context["repository"], "story_id": context["story_id"],
                "source_branch": branch, "target_branch": "stage-pre-prod", "is_draft": True,
            "template_path": str(template.relative_to(repository)), "title": context["title"],
                "body": native_body, "native_arguments": arguments,
                "expected_revisions": native.get("branch_association_preview", {}).get("expected_revisions", {})}

    def create_draft(self, preview: dict[str, Any], *, key: str) -> dict[str, Any]:
        arguments = {**preview["native_arguments"], "expected_revision": preview["expected_revision"]}
        arguments["expected_revisions"] = preview["expected_revisions"]
        result = self.gateway.execute("draft_pr", "create_story_pull_request", arguments, key)
        if result.get("created") is not True:
            raise ValueError("A racing existing PR requires reconcile before reuse")
        if result.get("body_markdown") != preview["body"]:
            raise ValueError("Created PR body differs from approved native preview; reconcile")
        pull_request = result.get("pull_request", result)
        return {**result, "url": pull_request.get("web_url", pull_request.get("url")), "is_draft": pull_request.get("is_draft")}


@dataclass(frozen=True)
class OperatorTaskPlan:
    plan_id: str
    repository: str
    story_id: int
    task_id: int
    files: tuple[str, ...]
    implementation_command: tuple[str, ...]
    test_commands: tuple[tuple[str, ...], ...]
    logic: str
    risks: tuple[str, ...] = ()


class OperatorTasks:
    def __init__(self, plans: tuple[OperatorTaskPlan, ...], branches: GitBranches,
                 journal: LifecycleJournal) -> None:
        self.plans = {plan.plan_id: plan for plan in plans}
        if len(self.plans) != len(plans):
            raise ValueError("Duplicate operator plan ID")
        self.branches, self.journal = branches, journal
        for plan in plans:
            if not plan.plan_id or not plan.files or not plan.logic or not plan.implementation_command or not plan.test_commands:
                raise ValueError("Incomplete operator plan")
            repository = branches.path(plan.repository)
            for filename in plan.files:
                path = Path(filename)
                if path.is_absolute() or ".." in path.parts or not (repository / path).resolve().is_relative_to(repository):
                    raise ValueError("Plan files must be contained by authorized repository")
            if any(not command or any(not isinstance(part, str) or not part for part in command)
                   for command in (plan.implementation_command, *plan.test_commands)):
                raise ValueError("Operator commands must be explicit argument vectors")

    def _plan(self, plan_id: str) -> OperatorTaskPlan:
        plan = self.plans[plan_id]
        context = active_state()["context"]
        if (context["repository"], context["story_id"], context["task_id"]) != (plan.repository, plan.story_id, plan.task_id):
            raise ValueError("Operator plan does not own active task")
        return plan

    def _tree(self, plan: OperatorTaskPlan) -> str:
        repository = self.branches.path(plan.repository)
        payload = {"head": git(repository, "rev-parse", "HEAD"),
                   "diff": git(repository, "diff", "--binary", "HEAD"), "files": {}}
        untracked = git(repository, "ls-files", "--others", "--exclude-standard").splitlines()
        for filename in sorted(set(plan.files).union(untracked)):
            path = repository / filename
            if path.is_symlink() or not path.resolve().is_relative_to(repository):
                raise ValueError("Task evidence cannot follow symlinks")
            payload["files"][filename] = sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        return sha256(canonical(payload).encode()).hexdigest()

    def _run(self, plan: OperatorTaskPlan, command: tuple[str, ...]) -> dict[str, Any]:
        context = active_state()["context"]
        execution_context = {field: context.get(field) for field in
                             ("story_id", "task_id", "story_details", "task_details", "hierarchy_context")}
        result = subprocess.run(command, cwd=self.branches.path(plan.repository), shell=False,
                                capture_output=True, timeout=600,
                                env={**os.environ, "KWIKLEDGERS_TASK_CONTEXT": canonical(execution_context)})
        return {"returncode": result.returncode, "command": list(command),
                "stdout_sha256": sha256(result.stdout).hexdigest(), "stderr_sha256": sha256(result.stderr).hexdigest()}

    def plan(self, context: dict[str, Any]) -> dict[str, Any]:
        matches = [plan for plan in self.plans.values() if
                   (plan.repository, plan.story_id, plan.task_id) ==
                   (context["repository"], context["story_id"], context["task_id"])]
        if len(matches) != 1:
            raise RuntimeError("Dry-run: no unique operator-configured task executor available")
        plan = matches[0]
        return {"plan_id": plan.plan_id, "files": list(plan.files), "logic": plan.logic,
            "tests": [list(command) for command in plan.test_commands], "risks": list(plan.risks),
            "hierarchy_context": context.get("hierarchy_context")}

    def implement(self, plan_id: str, *, key: str) -> dict[str, Any]:
        state = active_state()
        plan = self._plan(plan_id)
        if state.get("results", {}).get("plan") != self.plan(state["context"]):
            raise ValueError("Operator plan differs from approved plan")
        def execute() -> dict[str, Any]:
            active_state()
            evidence = self._run(plan, plan.implementation_command)
            if evidence["returncode"] != 0:
                raise RuntimeError("Trusted implementation command failed")
            return {"implemented": True, "plan_id": plan_id, "evidence": evidence}
        return self.journal.once(key, {"plan_id": plan_id}, execute)

    def test(self, plan_id: str, *, key: str) -> dict[str, Any]:
        state = active_state()
        plan = self._plan(plan_id)
        implemented = self.journal.get(f'{state["run_id"]}:implement:result')
        if not implemented or implemented["result"].get("plan_id") != plan_id:
            raise ValueError("Authenticated implementation evidence missing")
        def execute() -> dict[str, Any]:
            evidence = [self._run(plan, command) for command in plan.test_commands]
            return {"passed": all(item["returncode"] == 0 for item in evidence),
                    "evidence": evidence, "tree_digest": self._tree(plan), "plan_id": plan_id}
        return self.journal.once(key, {"plan_id": plan_id}, execute)

    def commit(self, plan_id: str, task_id: int, *, key: str) -> dict[str, Any]:
        state = active_state()
        plan = self._plan(plan_id)
        tests = self.journal.get(f'{state["run_id"]}:test:result')
        if (task_id != plan.task_id or not tests or tests["result"].get("passed") is not True
                or tests["result"].get("tree_digest") != self._tree(plan)
                or state.get("results", {}).get("plan") != self.plan(state["context"])
                or state.get("results", {}).get("test", {}).get("evidence") != tests["result"]["evidence"]):
            raise ValueError("Commit requires unchanged authenticated green test evidence")
        repository = self.branches.path(plan.repository)
        staged = git(repository, "diff", "--cached", "--name-only").splitlines()
        if not set(staged) <= set(plan.files):
            raise ValueError("Unrelated staged files block commit")
        def execute() -> dict[str, Any]:
            active_state()
            git(repository, "add", "--", *plan.files)
            if not git(repository, "diff", "--cached", "--name-only"):
                raise ValueError("No authorized changes to commit")
            active_state()
            git(repository, "commit", "-m", f"KL-{task_id}: {plan.logic}")
            result = {"commit": git(repository, "rev-parse", "HEAD"), "tests_passed": True, "plan_id": plan_id}
            self.journal.put(f'task:{plan.repository}:{task_id}:commit', result)
            return result
        return self.journal.once(key, {"plan_id": plan_id, "task_id": task_id}, execute)


class MCPDebtPreparation:
    def __init__(self, gateway: LifecycleGateway, repositories: Mapping[str, Path],
                 iteration_paths: tuple[str, ...], *, task_acceptance_supported: bool = False) -> None:
        self.gateway, self.repositories = gateway, dict(repositories)
        self.iterations = iteration_paths
        self.task_acceptance_supported = task_acceptance_supported

    def resolve(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("repository_name") not in self.repositories:
            raise ValueError("Debt repository not authorized by operator")
        normalized = deepcopy(request)
        if request.get("target_iteration") == "next_sprint":
            if request.get("iteration_path"):
                raise ValueError("Choose explicit iteration or next_sprint, not both")
            summary = self.gateway.client.call("get_my_daily_summary", {})
            path = summary.get("sprint_context", {}).get("path")
            match = re.fullmatch(r"(.+)\\(\d{4})\\Sprint (\d+)", path or "")
            year = datetime.fromisoformat(UTCClock().now()).year
            if match is None or int(match[2]) != year or int(match[3]) < 1:
                raise ValueError("Authoritative current-year main sprint required")
            normalized["iteration_path"] = f"{match[1]}\\{year}\\Sprint {int(match[3]) + 1}"
        if normalized.get("iteration_path") not in self.iterations:
            raise ValueError("Explicit operator-authorized debt iteration required")
        normalized = {field: deepcopy(normalized[field]) for field in
                      ("repository_name", "iteration_path", "parent", "children", "allow_grave") if field in normalized}
        if any(not child.get("assigned_to") for child in normalized.get("children", [])):
            identity = native_active_user_identity(self.gateway.client.call("get_active_user", {}))
            for child in normalized["children"]:
                if not child.get("assigned_to"):
                    child["assigned_to"] = identity
        normalized["task_acceptance_supported"] = self.task_acceptance_supported
        normalized["implementation_indicated"] = True
        return normalized

    def find_similar(self, repository_name: str, title: str, context: str) -> dict[str, Any]:
        result = self.gateway.client.call("find_similar_technical_debts", {
            "repository_name": repository_name, "title": title, "description": context,
        })
        if (result.get("repository_name") != repository_name or result.get("query_title") != title
                or type(result.get("duplicate_found")) is not bool
                or not isinstance(result.get("suggested_matches"), list)):
            raise ValueError("Structured native duplicate evidence required")
        return {**result, "exact_match": result["duplicate_found"], "similar_items": result["suggested_matches"]}

    def save_repository_context(self, repository: str, similarity: dict[str, Any], *, key: str) -> str:
        self.gateway.journal.put(key, {"repository": repository, "similarity": similarity})
        return self._artifact(repository, similarity, "technical_debts")

    def save_preview_cache(self, repository: str, preview: dict[str, Any], *, key: str) -> str:
        self.gateway.journal.put(key, {"repository": repository, "technical_debt_preview": preview})
        return self._artifact(repository, preview, "technical_debt_previews")

    def _artifact(self, repository: str, payload: dict[str, Any], directory: str) -> str:
        from kwikledgers_langgraph.observability import resolve_ai_tracking_root
        from kwikledgers_langgraph.state_persistence import write_state_file

        if repository not in self.repositories or re.fullmatch(r"[a-z][a-z0-9_]*", repository) is None:
            raise ValueError("Invalid repository artifact name")
        path = resolve_ai_tracking_root() / "Repo_Work_Item_Context" / directory / f"{repository}.json"
        write_state_file(payload, str(path))
        return str(path)


def build_debt_preparation_ports(*, repositories: Mapping[str, Path], iteration_paths: tuple[str, ...],
                                 journal: LifecycleJournal, client: CapabilityClient | None = None,
                                 runtime_issuer: CapabilityIssuer | None = None,
                                 task_acceptance_supported: bool = False) -> TechnicalDebtPreparationPorts:
    gateway = LifecycleGateway(client if client is not None else FastMCPClient(), journal, None, runtime_issuer)
    return TechnicalDebtPreparationPorts(
        MCPDebtPreparation(gateway, repositories, iteration_paths,
                           task_acceptance_supported=task_acceptance_supported), MCPTracking(gateway))


def build_lifecycle_ports(*, repositories: Mapping[str, Path], project: str, area: str,
                          journal: LifecycleJournal, approvals: Any,
                          client: CapabilityClient | None = None,
                          execution_issuer: CapabilityIssuer | None = None,
                          runtime_issuer: CapabilityIssuer | None = None,
                          tasks: Any = None, plans: tuple[OperatorTaskPlan, ...] = ()) -> LifecyclePorts:
    gateway = LifecycleGateway(client if client is not None else FastMCPClient(), journal,
                               execution_issuer, runtime_issuer)
    branches = GitBranches(repositories, gateway)
    executor = tasks if tasks is not None else OperatorTasks(plans, branches, journal)
    return LifecyclePorts(MCPContext(gateway, branches, project=project, area=area), MCPTracking(gateway),
                          approvals, UTCClock(), branches, MCPWorkItems(gateway),
                          MCPPullRequests(gateway, branches), executor)