from __future__ import annotations

import unittest

from kwikledgers_langgraph.story_status_policy import build_story_status_transition_preview


class StoryStatusPolicyTestCase(unittest.TestCase):
    def test_explicit_exception_still_requires_exact_current_status_and_canonical_target(self) -> None:
        request = {"story_id": 1, "target_status": "QA", "expected_current_status": "Desenvolvimento",
                   "transition_exception_reason": "Solicitação explícita de homologação já concluída."}
        snapshot = {"current_status": "Desenvolvimento", "work_item_type": "User Story"}
        self.assertTrue(build_story_status_transition_preview(request, snapshot)["transition_allowed"])
        for changes in ({"expected_current_status": ""}, {"expected_current_status": "New"},
                        {"transition_exception_reason": " "}, {"transition_exception_reason": True},
                        {"target_status": "Unknown"}):
            self.assertFalse(build_story_status_transition_preview({**request, **changes}, snapshot)["transition_allowed"])

    def test_allows_only_the_next_canonical_status(self) -> None:
        preview = build_story_status_transition_preview(
            {"story_id": 120204, "target_status": "Aguardando PR", "expected_current_status": "Desenvolvimento"},
            {"story_id": 120204, "work_item_type": "User Story", "current_status": "Desenvolvimento", "story_revision": "rev-1"},
        )

        self.assertTrue(preview["transition_allowed"])
        self.assertEqual(preview["patch_preview"], {"System.State": "Aguardando PR"})

    def test_blocks_status_jump(self) -> None:
        preview = build_story_status_transition_preview(
            {"story_id": 120204, "target_status": "QA"},
            {"story_id": 120204, "work_item_type": "User Story", "current_status": "Desenvolvimento"},
        )

        self.assertFalse(preview["transition_allowed"])
        self.assertIn("A transicao solicitada nao e a proxima etapa permitida no fluxo canonico.", preview["validation_messages"])