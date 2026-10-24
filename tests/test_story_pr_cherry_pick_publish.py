from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kwikledgers_langgraph.adapters import StoryPrCherryPickPorts, StoryPrPublishPorts
from kwikledgers_langgraph.graphs.story_pr_cherry_pick import build_cherry_pick_links
from kwikledgers_langgraph.guardrails import load_resume_confirmation
from kwikledgers_langgraph.observability import build_observability_context
from kwikledgers_langgraph.story_pr_worktree import GitWorktreeAdapter, cherry_pick_branch_name
from test_story_pr_launch import BRANCH, Fixture, _git, _request

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_EDITOR": "true"}
GENERATED = cherry_pick_branch_name(BRANCH, "stage-homolog")


def _pull_request(commit: str, *, draft: bool = True) -> dict[str, Any]:
    return {"pull_request_id": 7, "repository_name": "repo", "title": "KL-900 PR", "is_draft": draft,
            "status": "active", "source_ref_name": f"refs/heads/{BRANCH}",
            "target_ref_name": "refs/heads/stage-pre-prod", "source_commit_id": commit,
            "merge_status": "succeeded", "web_url": "https://x/pr/7"}


class FakeAzure:
    def __init__(self, fixture: Fixture, outcome: dict[str, Any]) -> None:
        self.fixture, self.outcome, self.calls = fixture, outcome, []

    def list_candidate_pull_requests(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        return [_pull_request(_git(self.fixture.repo, "rev-parse", BRANCH).strip())]

    def cherry_pick(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("cherry_pick")
        if self.outcome.get("completed"):
            subprocess.run(["git", f"--git-dir={self.fixture.origin}", "branch", GENERATED, "stage-homolog"], check=True)
        return self.outcome

    def get_cherry_pick(self, repository_name: str, cherry_pick_id: int) -> dict[str, Any]:
        return self.outcome


class CherryPickFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        if not LANGGRAPH_AVAILABLE:
            self.skipTest("langgraph indisponível")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.fixture = Fixture(root)
        self.fixture.push()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        patcher = patch.dict(os.environ, {**IDENTITY, "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(root / "caps.sqlite3")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_homolog(self, *, conflicting: bool) -> None:
        repo = self.fixture.repo
        _git(repo, "checkout", "-q", "-b", "homolog", f"{BRANCH}~1")
        if conflicting:
            (repo / "app.py").write_text("print('homolog')\n")
            _git(repo, "commit", "-qam", "homolog diverge")
        _git(repo, "push", "-q", "origin", "homolog:stage-homolog")
        _git(repo, "checkout", "-q", BRANCH)

    def graph(self, azure: FakeAzure) -> Any:
        from kwikledgers_langgraph.graphs.story_pr_cherry_pick import build_story_pr_cherry_pick_graph

        worktree = GitWorktreeAdapter(workspace_root=self.workspace)
        return build_story_pr_cherry_pick_graph(StoryPrCherryPickPorts(pull_requests=azure, worktree=worktree))

    def request(self) -> dict[str, Any]:
        return {"story_id": 900, "repository_name": "repo", "repository_path": str(self.fixture.repo),
                "target_branch": "stage-homolog", "source_target_branch": "stage-pre-prod"}

    def run_flow(self, graph: Any, approve: bool) -> dict[str, Any]:
        first = graph.invoke({
            "source": "test", "tracking_events": [], "cherry_pick_request": self.request(),
            "observability": build_observability_context(flow_name="story_pr_cherry_pick", source="test",
                                                         backend_name="fastmcp")})
        if not approve:
            return first
        resumed = dict(first)
        resumed["approval_confirmation"] = load_resume_confirmation(
            first, backend_name="fastmcp", receipt_file=None, resume_state_file="x",
            approval_request_id=first["pending_approval"]["request_id"], approval_source="test")
        return graph.invoke(resumed)

    def test_without_approval_nothing_runs(self) -> None:
        self.make_homolog(conflicting=False)
        azure = FakeAzure(self.fixture, {"completed": True})
        result = self.run_flow(self.graph(azure), approve=False)
        self.assertEqual(azure.calls, [])
        self.assertEqual(result["cherry_pick_preview"]["action"], "azure_api")
        self.assertEqual(result["cherry_pick_preview"]["generated_branch"], GENERATED)
        self.assertEqual(result["cherry_pick_preview"]["execution_arguments"]["target_branch"], "stage-homolog")
        self.assertFalse((self.workspace / "worktrees").exists())

    def test_azure_success_is_verified_against_origin(self) -> None:
        self.make_homolog(conflicting=False)
        azure = FakeAzure(self.fixture, {"status": "completed", "completed": True, "conflict": False})
        result = self.run_flow(self.graph(azure), approve=True)
        outcome = result["cherry_pick_result"]
        self.assertEqual(outcome["status"], "cherry_picked", result.get("errors"))
        self.assertEqual(outcome["verification"]["via"], "azure_api")
        self.assertTrue(any("story_pr_launch" in step for step in outcome["verification"]["pending_after_cherry_pick"]))

    def test_azure_conflict_falls_back_to_worktree_and_pushes(self) -> None:
        self.make_homolog(conflicting=False)
        azure = FakeAzure(self.fixture, {"status": "failed", "completed": False, "conflict": True})
        result = self.run_flow(self.graph(azure), approve=True)
        outcome = result["cherry_pick_result"]
        self.assertEqual(outcome["status"], "cherry_picked", result.get("errors"))
        self.assertEqual(outcome["verification"]["via"], "worktree")
        self.assertTrue(any("worktree remove" in step for step in outcome["verification"]["pending_after_cherry_pick"]))
        remote = subprocess.run(["git", f"--git-dir={self.fixture.origin}", "rev-parse", GENERATED], check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(remote, outcome["verification"]["remote_sha"])

    def test_real_conflict_preserves_worktree_and_resumes_idempotently(self) -> None:
        self.make_homolog(conflicting=True)
        azure = FakeAzure(self.fixture, {"status": "failed", "completed": False, "conflict": True})
        graph = self.graph(azure)
        stopped = self.run_flow(graph, approve=True)
        outcome = stopped["cherry_pick_result"]
        self.assertEqual(outcome["status"], "conflict_local")
        self.assertEqual(outcome["report"]["conflicting_files"], ["app.py"])
        worktree = Path(outcome["report"]["worktree_path"])
        self.assertTrue(worktree.is_dir())
        self.assertEqual(subprocess.run(["git", f"--git-dir={self.fixture.origin}", "branch", "--list", GENERATED], check=True, capture_output=True, text=True).stdout.strip(), "")

        still = self.run_flow(graph, approve=True)
        self.assertEqual(still["cherry_pick_preview"]["action"], "finalize_local")
        self.assertEqual(still["cherry_pick_result"]["status"], "conflict_local")
        self.assertEqual(azure.calls, ["cherry_pick"])

        (worktree / "app.py").write_text("print('resolvido')\n")
        _git(worktree, "add", "app.py")
        _git(worktree, "cherry-pick", "--continue")
        done = self.run_flow(graph, approve=True)
        self.assertEqual(done["cherry_pick_result"]["status"], "cherry_picked", done.get("errors"))
        self.assertEqual(azure.calls, ["cherry_pick"])

    def test_existing_remote_branch_stops_as_already_done(self) -> None:
        self.make_homolog(conflicting=False)
        subprocess.run(["git", f"--git-dir={self.fixture.origin}", "branch", GENERATED, "stage-homolog"], check=True)
        azure = FakeAzure(self.fixture, {"completed": True})
        result = self.run_flow(self.graph(azure), approve=False)
        self.assertEqual(result["cherry_pick_result"]["status"], "already_done")
        self.assertEqual(azure.calls, [])

    def test_in_progress_is_not_retried_or_sent_to_worktree(self) -> None:
        self.make_homolog(conflicting=False)
        azure = FakeAzure(self.fixture, {"status": "inProgress", "completed": False, "conflict": False,
                                         "cherry_pick_id": 9})
        result = self.run_flow(self.graph(azure), approve=True)
        self.assertEqual(result["cherry_pick_result"]["status"], "in_progress")
        self.assertEqual(result["cherry_pick_result"]["report"]["cherry_pick_id"], 9)
        self.assertFalse((self.workspace / "worktrees").exists())

    def test_missing_target_branch_is_reported_before_approval(self) -> None:
        azure = FakeAzure(self.fixture, {"completed": True})
        result = self.run_flow(self.graph(azure), approve=False)
        self.assertTrue(any("não existe no origin" in error for error in result["errors"]))
        self.assertEqual(azure.calls, [])


class FakePublish:
    def __init__(self, *, draft: bool = True) -> None:
        self.draft, self.calls = draft, []

    def preview_publish(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.draft:
            raise ValueError("A PR 7 já está publicada.")
        return {"story_id": 900, "repository_name": "repo", "pull_request_id": 7, "title": "KL-900 PR",
                "target_ref_name": "refs/heads/stage-pre-prod", "expected_source_commit": "a" * 40,
                "web_url": "https://x/pr/7"}

    def publish_pull_request(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(request["preview"]["expected_source_commit"])
        return {"published": True, "pull_request": {"pull_request_id": 7, "is_draft": False,
                                                    "status": "active", "web_url": "https://x/pr/7",
                                                    "target_ref_name": "refs/heads/stage-pre-prod"}}


class PublishFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        if not LANGGRAPH_AVAILABLE:
            self.skipTest("langgraph indisponível")

    def invoke(self, port: FakePublish, approve: bool) -> dict[str, Any]:
        from kwikledgers_langgraph.graphs.story_pr_publish import build_story_pr_publish_graph

        graph = build_story_pr_publish_graph(StoryPrPublishPorts(pull_requests=port))
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(Path(directory) / "caps.sqlite3")}):
            first = graph.invoke({
                "source": "test", "tracking_events": [],
                "publish_request": {"story_id": 900, "repository_name": "repo"},
                "observability": build_observability_context(flow_name="story_pr_publish", source="test",
                                                             backend_name="fastmcp")})
            if not approve or not first.get("pending_approval"):
                return first
            resumed = dict(first)
            resumed["approval_confirmation"] = load_resume_confirmation(
                first, backend_name="fastmcp", receipt_file=None, resume_state_file="x",
                approval_request_id=first["pending_approval"]["request_id"], approval_source="test")
            return graph.invoke(resumed)

    def test_without_approval_does_not_publish(self) -> None:
        port = FakePublish()
        result = self.invoke(port, approve=False)
        self.assertEqual(port.calls, [])
        self.assertEqual(result["publish_preview"]["execution_arguments"]["pull_request_id"], 7)

    def test_approved_run_publishes_and_verifies(self) -> None:
        port = FakePublish()
        result = self.invoke(port, approve=True)
        self.assertEqual(port.calls, ["a" * 40])
        verification = result["publish_result"]["verification"]
        self.assertTrue(verification["verified"], verification)
        self.assertTrue(any("Aguardando PR" in step for step in verification["pending_after_publish"]))

    def test_already_published_stops_at_preview(self) -> None:
        port = FakePublish(draft=False)
        result = self.invoke(port, approve=True)
        self.assertEqual(port.calls, [])
        self.assertTrue(any("já está publicada" in error for error in result["errors"]))

    def test_verification_lists_divergences(self) -> None:
        from kwikledgers_langgraph.graphs.story_pr_publish import verify_published_pull_request

        problems = verify_published_pull_request(
            {"pull_request_id": 7, "target_ref_name": "refs/heads/a"},
            {"published": False, "pull_request": {"pull_request_id": 8, "is_draft": True, "status": "abandoned",
                                                  "target_ref_name": "refs/heads/b"}})
        self.assertEqual(len(problems), 5)


class AdapterArgumentTests(unittest.TestCase):
    def test_publish_and_cherry_pick_arguments_come_from_the_approved_preview(self) -> None:
        from kwikledgers_langgraph.fastmcp_adapters import technical_arguments_for_capability as arguments

        preview = {"repository_name": "repo", "pull_request_id": "7", "expected_source_commit": "a" * 40,
                   "target_branch": "stage-homolog", "generated_branch": GENERATED, "action": "azure_api"}
        self.assertEqual(arguments("publish_story_pull_request", {"preview": preview}),
                         {"repository_name": "repo", "pull_request_id": 7, "expected_source_commit": "a" * 40})
        self.assertEqual(arguments("cherry_pick_story_pull_request", {"preview": preview})["wait_seconds"], 90)
        self.assertEqual(arguments("cherry_pick_story_pull_request", {"preview": {**preview, "action": "finalize_local"}}), {})
        with self.assertRaises(ValueError):
            arguments("publish_story_pull_request", {"preview": {}})

    def test_branch_name_matches_the_server_pattern(self) -> None:
        import re

        name = cherry_pick_branch_name("hotfix/KL-5-fetch-bank", "stage-homolog")
        self.assertEqual(name, "hotfix/KL-5-fetch-bank-on-stage-homolog")
        self.assertTrue(re.match(r"^(feature|hotfix)/KL-\d+-[a-z0-9][a-z0-9-]*-on-[a-z0-9][a-z0-9._-]*$", name))


class LaunchTargetTests(unittest.TestCase):
    def test_cherry_pick_branch_and_custom_target_are_accepted(self) -> None:
        from kwikledgers_langgraph.graphs.story_pr_launch import verify_created_pull_request
        from kwikledgers_langgraph.story_pr_preflight import run_preflight

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, IDENTITY):
            fixture = Fixture(Path(directory))
            name = "hotfix/KL-900-repo-on-stage-homolog"
            _git(fixture.repo, "branch", name, BRANCH)
            _git(fixture.repo, "push", "-q", "origin", f"{name}:{name}")
            _git(fixture.repo, "push", "-q", "origin", "stage-pre-prod:stage-homolog")
            report = run_preflight(_request(fixture.repo, source_branch=name, target_branch="stage-homolog"),
                                   workspace_root=Path(directory))
            checks = {item["id"]: item for item in report["checks"]}
            self.assertTrue(checks["branch_name"]["ok"], checks["branch_name"])
            self.assertTrue(checks["ahead_of_base"]["ok"], checks["ahead_of_base"])
            wrong_name = run_preflight(_request(fixture.repo, source_branch="cherry-pick/KL-1-x"),
                                       workspace_root=Path(directory))
            self.assertFalse({item["id"]: item for item in wrong_name["checks"]}["branch_name"]["ok"])
        created = {"created": True, "pull_request": {
            "is_draft": True, "target_ref_name": "refs/heads/stage-homolog",
            "source_ref_name": f"refs/heads/{name}", "web_url": "https://x/pr/9"}}
        request = {"source_branch": name, "target_branch": "stage-homolog"}
        self.assertEqual(verify_created_pull_request(request, {}, created), [])
        self.assertEqual(len(verify_created_pull_request({**request, "target_branch": "stage-pre-prod"}, {}, created)), 1)


if __name__ == "__main__":
    unittest.main()


class CherryPickLinksTests(unittest.TestCase):
    def test_links_are_derived_from_pull_request_url(self) -> None:
        links = build_cherry_pick_links({
            "web_url": "https://dev.azure.com/o/p/_git/r/pullrequest/7",
            "generated_branch": "cherry-pick/KL-1-r-stage-homolog-pr7", "target_branch": "stage-homolog",
        })
        self.assertEqual(links["pull_request_origem"], "https://dev.azure.com/o/p/_git/r/pullrequest/7")
        self.assertIn("?version=GBcherry-pick%2FKL-1-r-stage-homolog-pr7", links["branch_cherry_pick"])
        self.assertIn("targetRef=stage-homolog", links["criar_pr_para_alvo"])
