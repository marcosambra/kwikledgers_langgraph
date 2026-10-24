from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from kwikledgers_langgraph.adapters import StoryPrLaunchPorts
from kwikledgers_langgraph.guardrails import load_resume_confirmation
from kwikledgers_langgraph.observability import build_observability_context
from kwikledgers_langgraph.story_pr_preflight import (
    GitPreflightAdapter,
    after_pull_request_checklist,
    run_preflight,
)

LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None
BRANCH = "hotfix/KL-900-ajustar-paginacao"


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}).stdout


class Fixture:
    """Local clone with a bare origin, mimicking a published story branch."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.repo = root / "repo"
        subprocess.run(["git", "init", "--bare", "-b", "stage-pre-prod", str(self.origin)], check=True,
                       capture_output=True)
        subprocess.run(["git", "clone", str(self.origin), str(self.repo)], check=True, capture_output=True)
        (self.repo / "pull_request_template.md").write_text("## Template\n")
        (self.repo / "app.py").write_text("print('base')\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "base")
        _git(self.repo, "push", "origin", "HEAD:stage-pre-prod")
        _git(self.repo, "checkout", "-b", BRANCH)
        (self.repo / "app.py").write_text("print('feature')\n")
        _git(self.repo, "commit", "-am", "KL-900: ajusta paginação")

    def push(self) -> None:
        _git(self.repo, "push", "-u", "origin", BRANCH)


def _request(repo: Path, **overrides: object) -> dict[str, object]:
    base = {
        "story_id": 900, "repository_name": "accountant_backend", "repository_path": str(repo),
        "source_branch": BRANCH, "what_was_changed": "Paginação repassada ao serviço.",
        "affected_processes": "Detalhe dos reports financeiros.", "expected_impacts": "Detalhe paginado.",
        "important_points": "Depende de outra PR.", "mermaid_diagram": "flowchart TD\n  A-->B",
        "tests_updated": True, "tests_evidence": "pytest: 15 passed",
    }
    base.update(overrides)
    return base


def _ids(report: dict[str, object], key: str) -> list[str]:
    return list(report[key])  # type: ignore[arg-type]


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.fixture = Fixture(self.root)
        self.history = self.root / "workspace"
        (self.history / "docs" / "history").mkdir(parents=True)
        self.today = date(2026, 10, 20)
        (self.history / "docs" / "history" / "kwikledgers-alteration-2026-10-20.md").write_text("KL-900 ok\n")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_report(self, **overrides: object) -> dict[str, object]:
        return run_preflight(_request(self.fixture.repo, **overrides), workspace_root=self.history, today=self.today)

    def test_unpublished_branch_blocks(self) -> None:
        report = self.run_report()
        self.assertIn("branch_published", _ids(report, "blocking"))
        self.assertIn("origin_azure", _ids(report, "blocking"))

    def test_published_branch_passes_without_blockers_except_origin_host(self) -> None:
        self.fixture.push()
        report = self.run_report()
        self.assertEqual(_ids(report, "blocking"), ["origin_azure"])
        self.assertEqual(_ids(report, "warnings"), [])

    def test_unpushed_commit_blocks(self) -> None:
        self.fixture.push()
        (self.fixture.repo / "app.py").write_text("print('later')\n")
        _git(self.fixture.repo, "commit", "-am", "KL-900: later")
        self.assertIn("branch_published", _ids(self.run_report(), "blocking"))

    def test_forbidden_content_and_trailers_block(self) -> None:
        (self.fixture.repo / ".env").write_text("SECRET=1\n")
        (self.fixture.repo / "svc.php").write_text("// TEMP-LOCAL patch\n")
        _git(self.fixture.repo, "add", "-f", ".env", "svc.php")
        _git(self.fixture.repo, "commit", "-m", "outro commit\n\nCo-authored-by: Bot <b@b>")
        self.fixture.push()
        blocking = _ids(self.run_report(), "blocking")
        for expected in ("no_forbidden_files", "no_temporary_code", "commits_reference_story",
                         "no_coauthored_trailer"):
            self.assertIn(expected, blocking)

    def test_request_gaps_block(self) -> None:
        self.fixture.push()
        report = self.run_report(tests_updated=False, mermaid_diagram="texto", what_was_changed="todo",
                                 source_branch="feature/sem-id")
        for expected in ("tests_declared", "mermaid_diagram", "pr_text_filled", "branch_name"):
            self.assertIn(expected, _ids(report, "blocking"))

    def test_frontend_requires_existing_screenshot(self) -> None:
        self.fixture.push()
        report = self.run_report(repository_name="accountant_frontend")
        self.assertIn("frontend_screenshot", _ids(report, "blocking"))
        shot = self.root / "print.png"
        shot.write_bytes(b"png")
        report = self.run_report(repository_name="accountant_frontend", evidence_files=[str(shot)])
        self.assertNotIn("frontend_screenshot", _ids(report, "blocking"))

    def test_warnings_and_post_pr_checklist(self) -> None:
        self.fixture.push()
        (self.fixture.repo / "app.py").write_text("print('dirty')\n")
        report = self.run_report(tests_evidence="", repository_name="accountant_frontend")
        self.assertIn("tracked_worktree_clean", _ids(report, "warnings"))
        self.assertIn("tests_evidence", _ids(report, "warnings"))
        steps = after_pull_request_checklist({"repository_name": "accountant_frontend"}, report)
        self.assertTrue(any("print" in step for step in steps))
        self.assertTrue(any("Aguardando PR" in step for step in steps))

    def test_not_a_repository_root_stops_early(self) -> None:
        report = run_preflight(_request(self.root / "nada"), workspace_root=self.history)
        self.assertEqual(_ids(report, "blocking"), ["repository_root"])


class FakePullRequests:
    def __init__(self, *, created: dict[str, object] | None = None) -> None:
        self.created = created
        self.calls: list[str] = []

    def preview_story_pull_request(self, request: dict[str, object]) -> dict[str, object]:
        self.calls.append("preview")
        return {"title": "PR", "expected_revisions": {"900": 3, "901": 4}}

    def create_story_pull_request(self, request: dict[str, object]) -> dict[str, object]:
        self.calls.append("create")
        return self.created or {}


class FakePreflight:
    def __init__(self, report: dict[str, object]) -> None:
        self.report = report

    def run_preflight(self, request: dict[str, object]) -> dict[str, object]:
        return self.report


PASSED = {"passed": True, "blocking": [], "warnings": [], "head_sha": "abc", "checks": []}
BLOCKED = {"passed": False, "blocking": ["branch_published"], "warnings": [], "head_sha": None,
           "checks": [{"id": "branch_published", "severity": "block", "ok": False, "detail": "não publicada"}]}


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph não instalado")
class LaunchGraphTests(unittest.TestCase):
    def invoke(self, report: dict[str, object], pull_requests: FakePullRequests) -> dict[str, object]:
        from kwikledgers_langgraph.graphs.story_pr_launch import build_story_pr_launch_graph

        graph = build_story_pr_launch_graph(
            StoryPrLaunchPorts(pull_requests=pull_requests, preflight=FakePreflight(report)))
        return graph.invoke({"source": "test", "tracking_events": [], "pull_request_request": _request(Path("/x"))})

    def test_blocked_preflight_never_reaches_preview_or_create(self) -> None:
        pull_requests = FakePullRequests()
        result = self.invoke(BLOCKED, pull_requests)
        self.assertEqual(pull_requests.calls, [])
        self.assertEqual(result["pull_request_result"]["status"], "preflight_blocked")
        self.assertTrue(any("branch_published" in error for error in result["errors"]))

    def test_passed_preflight_stops_for_approval_without_creating(self) -> None:
        pull_requests = FakePullRequests()
        result = self.invoke(PASSED, pull_requests)
        self.assertEqual(pull_requests.calls, ["preview"])
        self.assertEqual(result["pull_request_result"]["status"], "approval_required")
        self.assertEqual(result["pull_request_preview"]["preflight"]["head_sha"], "abc")

    def test_existing_active_pull_request_stops_before_approval(self) -> None:
        pull_requests = FakePullRequests()
        pull_requests.preview_story_pull_request = lambda request: {  # type: ignore[method-assign]
            "title": "PR", "existing_active_pull_request": {"pull_request_id": 7}}
        result = self.invoke(PASSED, pull_requests)
        self.assertTrue(any("PR ativa" in error for error in result["errors"]))
        self.assertNotIn("create", pull_requests.calls)

    def test_missing_repository_path_is_rejected(self) -> None:
        from kwikledgers_langgraph.graphs.story_pr_launch import build_story_pr_launch_graph

        graph = build_story_pr_launch_graph(
            StoryPrLaunchPorts(pull_requests=FakePullRequests(), preflight=FakePreflight(PASSED)))
        request = _request(Path("/x"))
        request.pop("repository_path")
        result = graph.invoke({"source": "test", "tracking_events": [], "pull_request_request": request})
        self.assertTrue(any("repository_path" in error for error in result["errors"]))

    def test_approved_run_creates_verifies_and_lists_pending_steps(self) -> None:
        from kwikledgers_langgraph.graphs.story_pr_launch import build_story_pr_launch_graph

        created = {"created": True, "title": "PR", "pull_request": {
            "is_draft": True, "target_ref_name": "refs/heads/stage-pre-prod",
            "source_ref_name": f"refs/heads/{BRANCH}", "web_url": "https://x/pr/7",
            "work_item_refs": [{"id": "900"}, {"id": "901"}]}}
        pull_requests = FakePullRequests(created=created)
        graph = build_story_pr_launch_graph(
            StoryPrLaunchPorts(pull_requests=pull_requests, preflight=FakePreflight(PASSED)))
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(Path(directory) / "caps.sqlite3")}):
            request = _request(Path("/x"), repository_name="accountant_frontend")
            first = graph.invoke({
                "source": "test", "tracking_events": [], "pull_request_request": request,
                "observability": build_observability_context(flow_name="story_pr_launch", source="test",
                                                             backend_name="fastmcp")})
            pending = first["pending_approval"]
            resumed = dict(first)
            resumed["approval_confirmation"] = load_resume_confirmation(
                first, backend_name="fastmcp", receipt_file=None, resume_state_file="x",
                approval_request_id=pending["request_id"], approval_source="test")
            final = graph.invoke(resumed)
        self.assertEqual(pull_requests.calls, ["preview", "preview", "create"])
        verification = final["pull_request_result"]["verification"]
        self.assertTrue(verification["verified"], verification)
        self.assertEqual(verification["url"], "https://x/pr/7")
        self.assertTrue(any("print" in step for step in verification["pending_after_pr"]))


class VerificationTests(unittest.TestCase):
    def test_verification_lists_every_divergence(self) -> None:
        from kwikledgers_langgraph.graphs.story_pr_launch import verify_created_pull_request

        request = {"source_branch": BRANCH}
        preview = {"expected_revisions": {"900": 3, "901": 4}}
        good = {"created": True, "pull_request": {
            "is_draft": True, "target_ref_name": "refs/heads/stage-pre-prod",
            "source_ref_name": f"refs/heads/{BRANCH}", "web_url": "https://x/pr/1",
            "work_item_refs": [{"id": "900"}, {"id": "901"}]}}
        self.assertEqual(verify_created_pull_request(request, preview, good), [])
        bad = {"created": True, "pull_request": {
            "is_draft": False, "target_ref_name": "refs/heads/main", "source_ref_name": "refs/heads/outra",
            "work_item_refs": [{"id": "900"}]}}
        self.assertEqual(len(verify_created_pull_request(request, preview, bad)), 5)


class AdapterTests(unittest.TestCase):
    def test_adapter_delegates_to_runner(self) -> None:
        calls: list[list[str]] = []

        def runner(path: Path, arguments: list[str]) -> tuple[int, str]:
            calls.append(arguments)
            return 1, "erro"

        report = GitPreflightAdapter(runner).run_preflight(_request(Path("/x")))
        self.assertEqual(report["blocking"], ["repository_root"])
        self.assertEqual(calls, [["rev-parse", "--show-toplevel"]])


if __name__ == "__main__":
    unittest.main()
