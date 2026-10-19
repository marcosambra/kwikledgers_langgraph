import base64
import json
import tempfile
import unittest
from pathlib import Path

from kwikledgers_fastmcp.domains.local_tracking.handoffs import HandoffStore
from kwikledgers_langgraph.handoff import create_handoff, parse_handoff_marker, transition_handoff
from kwikledgers_langgraph.request_metrics_hook import handle_event


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.root = self.workspace / "AI_Tracking"

    def event(self, event, **fields):
        return handle_event(event, {"session_id": "session-one", **fields}, tracking_root=self.root)

    def create(self, root=None):
        return create_handoff(root or self.root, story_id=42, task_ids=[43, 44], sprint_path="Sprint fixture")

    def test_pending_accepted_running_and_explicit_completion_are_distinct(self):
        handoff = self.create()
        store = HandoffStore(self.root)
        self.assertEqual(store.read(handoff["handoff_id"])["status"], "pending")
        self.event("UserPromptSubmit", prompt=f"Continue {handoff['marker']}")
        self.assertEqual(store.read(handoff["handoff_id"])["status"], "accepted")
        self.event("PreToolUse", tool_name="runTests", tool_use_id="call-1")
        self.assertEqual(store.read(handoff["handoff_id"])["status"], "running")
        for fields in ({}, {"tests_passed": True}):
            with self.assertRaisesRegex(ValueError, "testes aprovados"):
                transition_handoff(self.root, handoff["handoff_id"], "completed", session_id="session-one", **fields)
        transition_handoff(self.root, handoff["handoff_id"], "completed",
                           session_id="session-one", tests_passed=True, evidence="Testes relevantes aprovados.")
        self.event("Stop")
        self.assertEqual(store.read(handoff["handoff_id"])["status"], "completed")
        events = [json.loads(line) for line in (self.root / "Metrics/request-events.jsonl").read_text().splitlines()]
        self.assertEqual({event["request_id"] for event in events}, {handoff["handoff_id"]})
        self.assertEqual(events[-1]["status"], "completed")

    def test_stop_does_not_claim_task_completion_and_session_cannot_be_replayed(self):
        handoff = self.create()
        self.event("UserPromptSubmit", prompt=handoff["marker"])
        self.event("Stop")
        record = HandoffStore(self.root).read(handoff["handoff_id"])
        self.assertEqual(record["status"], "blocked")
        with self.assertRaises(ValueError):
            transition_handoff(self.root, handoff["handoff_id"], "running", session_id="another-session")
        with self.assertRaises(ValueError):
            self.event("UserPromptSubmit", prompt=handoff["marker"])

    def test_custom_tracking_is_routed_by_session_and_new_prompt_detaches(self):
        custom = self.workspace / "custom-tracking"
        handoff = self.create(custom)
        self.event("UserPromptSubmit", prompt=handoff["marker"])
        self.event("PreToolUse", tool_name="runTests")
        self.assertEqual(HandoffStore(custom).read(handoff["handoff_id"])["status"], "running")
        self.event("UserPromptSubmit", prompt="New unrelated request")
        self.assertEqual(HandoffStore(custom).read(handoff["handoff_id"])["status"], "blocked")
        self.event("Stop")
        custom_events = [json.loads(line) for line in (custom / "Metrics/request-events.jsonl").read_text().splitlines()]
        self.assertEqual(custom_events[-1]["status"], "interrupted")
        default_events = [json.loads(line) for line in (self.root / "Metrics/request-events.jsonl").read_text().splitlines()]
        self.assertNotEqual(default_events[0]["request_id"], handoff["handoff_id"])

    def test_marker_cannot_read_an_arbitrary_external_root(self):
        descriptor = {"tracking_root": "/outside/workspace", "handoff_id": "00000000-0000-4000-8000-000000000000"}
        token = base64.urlsafe_b64encode(json.dumps(descriptor).encode()).decode().rstrip("=")
        with self.assertRaisesRegex(ValueError, "fora do workspace"):
            parse_handoff_marker(f"[kwikledgers-handoff:{token}]",
                                 workspace_root=self.workspace, default_tracking_root=self.root)

    def test_dispatch_failure_stays_failed_and_does_not_authorize_execution(self):
        handoff = self.create()
        record = transition_handoff(self.root, handoff["handoff_id"], "failed", evidence="Chat indisponível.")
        self.assertEqual(record["mode"], "local-only")
        self.assertEqual(record["status"], "failed")
        with self.assertRaises(ValueError):
            transition_handoff(self.root, handoff["handoff_id"], "accepted", session_id="session-one")
