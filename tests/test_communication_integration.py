import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CommunicationIntegrationTests(unittest.TestCase):
    def test_real_handoff_cli_hook_acceptance_and_explicit_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            tracking = Path(temporary) / "AI_Tracking"
            env = {**os.environ, "KWIKLEDGERS_ROOT_DIR": temporary, "KWIKLEDGERS_AI_TRACKING_ROOT": str(tracking)}

            def invoke(module, args=(), payload=None):
                result = subprocess.run([sys.executable, "-m", module, *args], env=env,
                                        input=json.dumps(payload) if payload is not None else None,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout) if result.stdout.strip() else None

            handoff = invoke("kwikledgers_langgraph.handoff_cli", ["--create", "--story-id", "42", "--task-ids", "43"])
            record_path = Path(handoff["path"])
            self.assertEqual(json.loads(record_path.read_text())["status"], "pending")
            invoke("kwikledgers_langgraph.request_metrics_hook", ["--event", "UserPromptSubmit"],
                   {"session_id": "integration-session", "prompt": handoff["marker"]})
            self.assertEqual(json.loads(record_path.read_text())["status"], "accepted")
            invoke("kwikledgers_langgraph.request_metrics_hook", ["--event", "PreToolUse"],
                   {"session_id": "integration-session", "tool_name": "runTests", "tool_use_id": "tests"})
            self.assertEqual(json.loads(record_path.read_text())["status"], "running")
            invoke("kwikledgers_langgraph.handoff_cli", [
                "--handoff-id", handoff["handoff_id"], "--session-id", "integration-session",
                "--status", "completed", "--tests-passed", "--evidence", "Fixture validada.",
            ])
            invoke("kwikledgers_langgraph.request_metrics_hook", ["--event", "Stop"],
                   {"session_id": "integration-session"})
            record = json.loads(record_path.read_text())
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["mode"], "local-only")

    def test_real_cli_graph_stdio_server_and_ledger_share_one_context_and_process(self):
        workspace = Path(__file__).resolve().parents[2]
        server_python = workspace / "kwikledgers_fastmcp" / ".venv" / "bin" / "python"
        if not server_python.is_file():
            self.skipTest("Integração exige o Python do pacote FastMCP no workspace.")
        fixture = Path(__file__).parent / "fixtures" / "communication_server.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracking = root / "custom-tracking"
            marker = root / "processes.txt"
            env = {
                **os.environ,
                "KWIKLEDGERS_ROOT_DIR": str(root),
                "KWIKLEDGERS_AI_TRACKING_ROOT": str(tracking),
                "KWIKLEDGERS_REQUEST_ID": "integration-request",
                "KWIKLEDGERS_RUN_ID": "integration-run",
                "KWIKLEDGERS_WORKSPACE_ID": "integration-workspace",
                "KWIKLEDGERS_FASTMCP_TRANSPORT": "stdio",
                "KWIKLEDGERS_FASTMCP_COMMAND": str(server_python),
                "KWIKLEDGERS_FASTMCP_ARGS": json.dumps([str(fixture)]),
                "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(root / "capabilities.sqlite3"),
                "COMMUNICATION_SMOKE_MARKER": str(marker),
            }
            env.pop("KWIKLEDGERS_FASTMCP_URL", None)
            completed = subprocess.run([
                sys.executable, "-m", "kwikledgers_langgraph.cli", "--json", "--disable-notifications",
                "--standalone-request", "--sprint-path", "fixture-sprint", "--story-id", "42",
            ], env=env, text=True, capture_output=True, timeout=45)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["context"]["request_id"], "integration-request")
            self.assertEqual(result["context"]["run_id"], "integration-run")
            self.assertEqual(result["result"]["summary"]["sprint_context"]["path"], "fixture-sprint")
            self.assertEqual(result["result"]["summary"]["branch_story_id"], 42)
            self.assertEqual(len(marker.read_text().splitlines()), 1)
            events = [json.loads(line) for line in (tracking / "Metrics" / "request-events.jsonl").read_text().splitlines()]
            steps = [event for event in events if event["source"] == "fastmcp"]
            self.assertEqual(len(steps), 2)
            self.assertEqual({event["request_id"] for event in events}, {"integration-request"})
            self.assertEqual({event["details"]["run_id"] for event in steps}, {"integration-run"})
            self.assertEqual(len({event["details"]["span_id"] for event in steps}), 2)
            self.assertEqual(sum(event["kind"] == "request_finished" for event in events), 1)
            self.assertTrue((tracking / "Task_Control" / "current-sprint.json").is_file())
