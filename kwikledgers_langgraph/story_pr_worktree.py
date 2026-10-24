from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kwikledgers_langgraph.story_pr_preflight import GitRunner, default_git_runner

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
WORKTREES_DIRECTORY = "worktrees"
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class WorktreeError(RuntimeError):
    """Raised when a local worktree operation cannot proceed safely."""


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-")


def local_directory_name(repository_name: str) -> str:
    return repository_name.strip().lower().replace(" ", "_")


def cherry_pick_branch_name(source_branch: str, target_branch: str) -> str:
    return f"{source_branch.strip()}-on-{target_branch.strip()}"


class GitWorktreeAdapter:
    """Cherry-pick fallback that only ever touches a dedicated worktree under ./worktrees.

    It never aborts, resets or forces anything: on conflict the worktree is preserved for the user.
    """

    def __init__(self, runner: GitRunner = default_git_runner, workspace_root: Path | None = None) -> None:
        self.runner = runner
        self.workspace_root = (workspace_root or WORKSPACE_ROOT).resolve()

    def repository_path(self, request: dict[str, Any]) -> Path:
        explicit = request.get("repository_path")
        return Path(explicit) if explicit else self.workspace_root / "projects" / local_directory_name(
            str(request["repository_name"]))

    def worktree_path(self, request: dict[str, Any]) -> Path:
        name = f"{local_directory_name(str(request['repository_name']))}-cherry-KL{int(request['story_id'])}" \
               f"-pr{int(request['pull_request_id'])}"
        return self.workspace_root / WORKTREES_DIRECTORY / name

    def _git(self, path: Path, *arguments: str) -> tuple[int, str]:
        code, output = self.runner(path, list(arguments))
        return code, output.strip()

    def _require(self, path: Path, *arguments: str) -> str:
        code, output = self._git(path, *arguments)
        if code != 0:
            raise WorktreeError(f"git {' '.join(arguments)} falhou: {output}")
        return output

    def _remote_sha(self, repository: Path, branch: str) -> str | None:
        code, output = self._git(repository, "ls-remote", "--heads", "origin", branch)
        if code != 0:
            raise WorktreeError(f"Não foi possível consultar o origin: {output}")
        for line in output.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == f"refs/heads/{branch}":
                return sha
        return None

    def _check_refs(self, *branches: str) -> None:
        for branch in branches:
            if not SAFE_REF.match(branch) or ".." in branch:
                raise WorktreeError(f"Nome de branch inválido: {branch}")

    def inspect(self, request: dict[str, Any]) -> dict[str, Any]:
        repository = self.repository_path(request)
        root = self._require(repository, "rev-parse", "--show-toplevel")
        if Path(root).resolve() != repository.resolve():
            raise WorktreeError(f"{repository} não é a raiz de um repositório Git.")
        self._check_refs(request["target_branch"], request["generated_branch"])
        worktree = self.worktree_path(request)
        listing = self._require(repository, "worktree", "list", "--porcelain")
        exists = any(line == f"worktree {worktree}" for line in listing.splitlines())
        state: dict[str, Any] = {
            "repository_path": str(repository), "worktree_path": str(worktree), "worktree_exists": exists,
            "target_exists": self._remote_sha(repository, request["target_branch"]) is not None,
            "remote_branch_sha": self._remote_sha(repository, request["generated_branch"]),
            "cherry_pick_in_progress": False, "unmerged_files": [],
        }
        if exists:
            state["cherry_pick_in_progress"] = self._git(worktree, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD")[0] == 0
            state["unmerged_files"] = self._unmerged(worktree)
        return state

    def remote_branch_sha(self, request: dict[str, Any]) -> str | None:
        return self._remote_sha(self.repository_path(request), request["generated_branch"])

    def _unmerged(self, worktree: Path) -> list[str]:
        code, output = self._git(worktree, "diff", "--name-only", "--diff-filter=U")
        return output.splitlines() if code == 0 and output else []

    def _expected_commits(self, repository: Path, request: dict[str, Any]) -> list[str]:
        self._check_refs(request["source_branch"], request["pr_target_branch"])
        output = self._require(
            repository, "rev-list", "--reverse", "--no-merges",
            f"origin/{request['pr_target_branch']}..origin/{request['source_branch']}",
        )
        commits = output.splitlines()
        if not commits:
            raise WorktreeError("A PR não possui commits próprios para aplicar no alvo.")
        return commits

    def _apply_commits(self, worktree: Path, commits: list[str], request: dict[str, Any]) -> dict[str, Any]:
        for commit in commits:
            code, output = self._git(worktree, "cherry-pick", commit)
            if code != 0:
                return {
                    "status": "conflict", "worktree_path": str(worktree), "stuck_commit": commit,
                    "conflicting_files": self._unmerged(worktree), "detail": output[:500],
                    "resume": f"Resolva em {worktree}, rode 'git cherry-pick --continue' e repita este comando.",
                }
        return {"status": "applied", "worktree_path": str(worktree), "commits": commits}

    def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        repository = self.repository_path(request)
        worktree = self.worktree_path(request)
        self._require(repository, "fetch", "origin")
        commits = self._expected_commits(repository, request)
        self._require(repository, "worktree", "add", "-b", request["generated_branch"], str(worktree),
                      f"origin/{request['target_branch']}")
        return self._apply_commits(worktree, commits, request)

    def finalize(self, request: dict[str, Any]) -> dict[str, Any]:
        """Resume after the user resolved a conflict: apply whatever is still missing."""
        repository = self.repository_path(request)
        worktree = self.worktree_path(request)
        self._require(repository, "fetch", "origin")
        if self._git(worktree, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD")[0] == 0 or self._unmerged(worktree):
            return {"status": "conflict", "worktree_path": str(worktree), "stuck_commit": None,
                    "conflicting_files": self._unmerged(worktree),
                    "detail": "Ainda há cherry-pick em andamento ou arquivos em conflito.",
                    "resume": f"Resolva em {worktree}, rode 'git cherry-pick --continue' e repita este comando."}
        commits = self._expected_commits(repository, request)
        ahead = int(self._require(worktree, "rev-list", "--count", f"origin/{request['target_branch']}..HEAD") or 0)
        if ahead > len(commits):
            raise WorktreeError("A branch local tem mais commits que a PR; revise o worktree manualmente.")
        return self._apply_commits(worktree, commits[ahead:], request)

    def push(self, request: dict[str, Any]) -> dict[str, Any]:
        worktree = self.worktree_path(request)
        self._check_refs(request["generated_branch"])
        self._require(worktree, "push", "-u", "origin", request["generated_branch"])
        return {"pushed_sha": self._require(worktree, "rev-parse", "HEAD"), "worktree_path": str(worktree)}
