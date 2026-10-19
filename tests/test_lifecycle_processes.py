from unittest import TestCase
from unittest.mock import Mock

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from kwikledgers_langgraph.adapters import LifecyclePorts
from kwikledgers_langgraph.lifecycle_runtime import build_lifecycle_runtime


class LifecycleProcessTests(TestCase):
    def setUp(self):
        self.events = []
        self.context = Mock()
        self.scope = {"story_id": 10, "task_id": 11, "child_ids": [11, 12],
                      "repository": "payment", "project": "KL", "area": "Squad",
                      "user_id": "user", "title": "Story"}
        self.context.resolve.return_value = self.scope
        self.snapshot = {"revision": 1, "status": "Desenvolvimento", "task_status": "Active", "child_ids": [11, 12],
                         "implementation_complete": True, "tests_passed": True, "base_up_to_date": True}
        self.context.snapshot.side_effect = lambda context: dict(self.snapshot)
        self.tracking = Mock()
        self.tracking.record.side_effect = lambda event, key: self.events.append(event)
        self.tracking.started_at.return_value = "2026-09-29T09:00:00+00:00"
        self.tracking.story_start_complete.return_value = True
        self.clock = Mock()
        self.clock.now.return_value = "2026-09-29T10:01:00+00:00"
        self.approvals = Mock()
        self.approvals.verify.side_effect = lambda pending, receipt: receipt == pending["request_id"]
        self.branches = Mock()
        self.branch = {"name": "feature/KL-10-story", "base": "stage-pre-prod", "remote_head": "abc",
                       "dirty": False, "reuse": False, "repository": "payment", "story_id": 10}
        self.branches.preview.side_effect = lambda context, branch_type: dict(self.branch)
        self.branches.create_or_reuse.return_value = {"name": self.branch["name"]}
        self.branches.links.return_value = {"name": self.branch["name"], "associated_ids": [10, 11, 12],
                           "expected_revisions": {"10": 1, "11": 1, "12": 1}}
        self.branches.associate.return_value = {"associated_ids": [10, 11, 12]}
        self.work_items = Mock()
        for operation in ("activate", "awaiting_pr", "hours", "close"):
            getattr(self.work_items, operation).return_value = {"ok": True}
        self.prs = Mock()
        self.prs.preview.return_value = {"source_branch": self.branch["name"],
            "target_branch": "stage-pre-prod", "is_draft": True, "title": "Story",
            "template_path": "pull_request_template.md", "body": "Template\n```mermaid\ngraph TD; A-->B\n```"}
        self.prs.create_draft.return_value = {"is_draft": True, "url": "https://dev.azure.com/KL/_git/payment/pullrequest/1"}
        self.tasks = Mock()
        self.tasks.plan.return_value = {"plan_id": "trusted-plan", "files": ["owned.py"],
            "logic": "Change", "tests": ["unit"], "risks": []}
        self.tasks.implement.return_value = {"implemented": True}
        self.tasks.test.return_value = {"passed": True, "evidence": "unit output"}
        self.tasks.commit.return_value = {"commit": "abc123"}
        self.ports = LifecyclePorts(self.context, self.tracking, self.approvals, self.clock,
                                   self.branches, self.work_items, self.prs, self.tasks)
        self.config = {"configurable": {"thread_id": self.id()}}

    def start(self, name, request=None):
        self.graph = build_lifecycle_runtime(name, self.ports, checkpointer=MemorySaver())
        return self.graph.invoke({"request": request or {}}, self.config)

    def approve(self, result):
        return self.graph.invoke(Command(resume=result["pending_approval"]["request_id"]), self.config)

    def test_start_time_then_assignment_area_active_then_branch_and_links(self):
        result = self.start("story_start")
        recorded = next(event for event in self.events if event["action"] == "story_start")
        self.assertIn("started_at", recorded)
        self.assertEqual(result["pending_approval"]["preview"]["area_path"], "KL\\Squad")
        self.work_items.activate.assert_not_called()
        self.branches.preview.assert_not_called()
        result = self.approve(result)
        self.assertEqual(result["__interrupt__"][0].value["options"], ["feature", "hotfix"])
        result = self.graph.invoke(Command(resume="feature"), self.config)
        self.branches.create_or_reuse.assert_not_called()
        result = self.approve(result)
        self.assertEqual(result["pending_approval"]["preview"]["associated_ids"], [10, 11, 12])
        result = self.approve(result)
        self.assertEqual(result["outcome"], "completed")
        self.tasks.implement.assert_not_called()

    def test_missing_area_records_time_but_never_activates(self):
        self.scope["area"] = ""
        result = self.start("story_start")
        self.assertEqual(result["outcome"], "blocked")
        self.assertTrue(any(event["action"] == "story_start" for event in self.events))
        self.work_items.activate.assert_not_called()

    def test_changed_remote_head_blocks_branch_execution(self):
        result = self.approve(self.start("story_start"))
        result = self.graph.invoke(Command(resume="feature"), self.config)
        self.branch["remote_head"] = "changed"
        result = self.approve(result)
        self.assertEqual(result["outcome"], "blocked")
        self.branches.create_or_reuse.assert_not_called()

    def test_dirty_checkout_blocks_without_stash(self):
        self.branch["dirty"] = True
        self.approve(self.start("story_start"))
        result = self.graph.invoke(Command(resume="feature"), self.config)
        self.assertEqual(result["outcome"], "blocked")
        self.branches.create_or_reuse.assert_not_called()

    def test_completion_returns_url_before_independent_status_gate(self):
        result = self.approve(self.start("story_completion"))
        self.assertEqual(result["pending_approval"]["action"], "draft_pr")
        self.prs.create_draft.assert_not_called()
        result = self.approve(result)
        self.assertEqual(result["direct_pr_url"], self.prs.create_draft.return_value["url"])
        self.assertEqual(result["pending_approval"]["action"], "awaiting_pr")
        self.work_items.awaiting_pr.assert_not_called()
        denied = self.graph.invoke(Command(resume="old-receipt"), self.config)
        self.assertEqual(denied["outcome"], "denied")
        self.work_items.awaiting_pr.assert_not_called()

    def test_completion_updates_status_only_after_new_approval(self):
        result = self.approve(self.approve(self.start("story_completion")))
        result = self.approve(result)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(self.work_items.awaiting_pr.call_args.args[0]["target_status"], "Aguardando PR")

    def test_missing_child_link_blocks_pr_even_after_association(self):
        self.branches.links.return_value["associated_ids"] = [10, 11]
        result = self.approve(self.start("story_completion"))
        self.assertEqual(result["outcome"], "blocked")
        self.prs.create_draft.assert_not_called()

    def test_story_revision_change_blocks_approved_pr(self):
        result = self.approve(self.start("story_completion"))
        self.snapshot["revision"] = 2
        result = self.approve(result)
        self.assertEqual(result["outcome"], "blocked")
        self.prs.create_draft.assert_not_called()

    def test_task_cycle_orders_tests_commit_hours_close(self):
        result = self.start("task_cycle", {"code": "untrusted", "command": "git reset --hard"})
        self.tasks.implement.assert_called_once()
        self.tasks.test.assert_called_once()
        self.tasks.commit.assert_called_once()
        self.assertEqual(result["pending_approval"]["action"], "hours")
        self.assertEqual(result["pending_approval"]["preview"]["completed_work_hours"], 2)
        self.work_items.hours.assert_not_called()
        result = self.approve(result)
        self.work_items.close.assert_not_called()
        self.assertEqual(result["pending_approval"]["preview"]["status"], "Closed")
        self.assertNotIn("remaining_work_hours", result["pending_approval"]["preview"])
        result = self.approve(result)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(self.tasks.implement.call_args.args, ("trusted-plan",))
        self.assertEqual(self.tasks.commit.call_args.args, ("trusted-plan", 11))

    def test_fix_context_is_checked_before_implementation_and_bound_to_hours_preview(self):
        result = self.start("task_cycle", {"work_kind": "fix", "fix_summary": "Regression"})
        self.assertEqual(result["outcome"], "blocked")
        self.tasks.implement.assert_not_called()
        self.config = {"configurable": {"thread_id": self.id() + "-valid"}}
        result = self.start("task_cycle", {"work_kind": "fix", "fix_summary": "Regression",
                                         "origin_task_id": 9})
        self.assertEqual(result["pending_approval"]["action"], "hours")
        self.assertEqual(result["pending_approval"]["preview"]["origin_task_id"], 9)
        self.assertEqual(result["pending_approval"]["preview"]["work_kind"], "fix")
        result = self.approve(self.approve(result))
        self.assertEqual(result["outcome"], "completed")

    def test_failed_tests_prevent_commit_hours_and_close(self):
        self.tasks.test.return_value = {"passed": False, "evidence": "failure"}
        result = self.start("task_cycle")
        self.assertEqual(result["outcome"], "blocked")
        self.tasks.commit.assert_not_called()
        self.work_items.hours.assert_not_called()
        self.work_items.close.assert_not_called()

    def test_no_commit_receipt_prevents_hours(self):
        self.tasks.commit.return_value = {}
        result = self.start("task_cycle")
        self.assertEqual(result["outcome"], "blocked")
        self.work_items.hours.assert_not_called()

    def test_missing_tracking_start_blocks_implementation(self):
        self.tracking.started_at.side_effect = FileNotFoundError()
        result = self.start("task_cycle")
        self.assertEqual(result["outcome"], "blocked")
        self.tasks.implement.assert_not_called()

    def test_task_cycle_cannot_skip_incomplete_story_start(self):
        self.tracking.story_start_complete.return_value = False
        result = self.start("task_cycle")
        self.assertEqual(result["outcome"], "blocked")
        self.tasks.plan.assert_not_called()
        self.tasks.implement.assert_not_called()

    def test_hotfix_reuses_parent_branch_for_another_child(self):
        self.scope["task_id"] = 12
        self.branch.update({"name": "hotfix/KL-10-story", "reuse": True})
        self.branches.create_or_reuse.return_value = {"name": self.branch["name"], "reused": True}
        self.branches.links.return_value["name"] = self.branch["name"]
        self.approve(self.start("story_start"))
        result = self.graph.invoke(Command(resume="hotfix"), self.config)
        result = self.approve(result)
        result = self.approve(result)
        self.assertEqual(result["outcome"], "completed")
        self.assertTrue(self.branches.create_or_reuse.call_args.args[0]["reuse"])
        self.assertEqual(self.work_items.activate.call_args.args[0]["task_id"], 12)

    def test_hierarchy_change_blocks_association_after_preview(self):
        result = self.start("story_completion")
        self.snapshot["child_ids"] = [11, 12, 13]
        result = self.approve(result)
        self.assertEqual(result["outcome"], "blocked")
        self.branches.associate.assert_not_called()
        self.prs.create_draft.assert_not_called()

    def test_pr_failure_never_changes_story_status(self):
        self.prs.create_draft.side_effect = OSError()
        result = self.approve(self.approve(self.start("story_completion")))
        self.assertEqual(result["outcome"], "reconciliation_required")
        self.assertEqual(result["outcome_reason"], "execution_outcome_unknown")
        self.work_items.awaiting_pr.assert_not_called()

    def test_untrusted_code_and_commands_are_not_forwarded_to_context(self):
        self.start("task_cycle", {"story_id": 10, "task_id": 11, "repository_name": "payment",
                                  "command": "git reset --hard", "code": "raise SystemExit"})
        self.context.resolve.assert_called_once_with({"story_id": 10, "task_id": 11, "repository_name": "payment"})

    def test_association_requires_revision_for_every_child(self):
        self.branches.links.return_value["expected_revisions"].pop("12")
        result = self.start("story_completion")
        self.assertEqual(result["outcome"], "blocked")
        self.branches.associate.assert_not_called()
        self.prs.create_draft.assert_not_called()

    def test_child_revision_change_blocks_association(self):
        result = self.start("story_completion")
        self.branches.links.return_value["expected_revisions"]["12"] = 2
        result = self.approve(result)
        self.assertEqual(result["outcome"], "blocked")
        self.branches.associate.assert_not_called()