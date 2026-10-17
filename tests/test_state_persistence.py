from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kwikledgers_langgraph.state_persistence import (
    STATE_SIGNATURE_ALGORITHM,
    STATE_SIGNING_KEY_ENV_VAR,
    StateFileError,
    load_state_file,
    inspect_approval,
    write_state_file,
)


class StatePersistenceTestCase(unittest.TestCase):
    def test_failed_atomic_write_preserves_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_state_file({"value": "before"}, str(path))
            with self.assertRaises(ValueError):
                write_state_file({"value": float("nan")}, str(path))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": "before"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_public_inspection_checks_the_pending_binding_without_execution(self) -> None:
        from kwikledgers_langgraph.guardrails import build_bound_approval

        state = {"observability": {"active_run_id": "run", "backend_name": "fastmcp"}}
        state["pending_approval"] = build_bound_approval(
            state, action="task_closure.execute", preview={"task_id": 1},
            approved=False, approval_source="test")
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "preview.json")
            write_state_file(state, path)
            inspected = inspect_approval(path)
            self.assertEqual(inspected["version"], 1)
            self.assertEqual(inspected["pending"], state["pending_approval"])
            state["pending_approval"]["preview"]["task_id"] = 2
            write_state_file(state, path)
            with self.assertRaises(StateFileError):
                inspect_approval(path)

    def test_write_and_load_unsigned_read_only_state_without_signing_key(self) -> None:
        state = {"summary": {"counts": {"assigned_items": 1}}, "source": "test"}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            with patch.dict("os.environ", {}, clear=False):
                write_state_file(state, str(path))
                loaded_state = load_state_file(str(path))

        self.assertEqual(loaded_state, state)

    def test_write_and_load_unsigned_mutable_state_without_signing_key(self) -> None:
        state = {"pull_request_request": {"story_id": 120204}, "source": "test"}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            with patch.dict("os.environ", {STATE_SIGNING_KEY_ENV_VAR: ""}, clear=False):
                write_state_file(state, str(path))
                self.assertEqual(load_state_file(str(path)), state)

    def test_configured_key_is_ignored_and_state_remains_unsigned(self) -> None:
        state = {"branch_association_request": {"story_id": 120204}, "source": "test"}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            with patch.dict("os.environ", {STATE_SIGNING_KEY_ENV_VAR: "top-secret-key"}, clear=False):
                write_state_file(state, str(path))
                payload = json.loads(path.read_text(encoding="utf-8"))
                loaded_state = load_state_file(str(path))

        self.assertEqual(payload, state)
        self.assertNotIn("signature", payload)
        self.assertEqual(loaded_state, state)

    def test_load_state_file_rejects_tampered_signed_state(self) -> None:
        state = {"story_status_request": {"story_id": 120204}, "source": "test"}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            with patch.dict("os.environ", {STATE_SIGNING_KEY_ENV_VAR: "top-secret-key"}, clear=False):
                payload = {"state": state, "signature": "old-signature",
                           "signature_algorithm": STATE_SIGNATURE_ALGORITHM}
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

                with self.assertRaisesRegex(StateFileError, "nova prévia"):
                    load_state_file(str(path))

    def test_unsigned_resume_ignores_configured_signing_key(self) -> None:
        state = {"story_status_request": {"story_id": 120204}, "source": "test"}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with patch.dict("os.environ", {STATE_SIGNING_KEY_ENV_VAR: "top-secret-key"}, clear=False):
                self.assertEqual(load_state_file(str(path)), state)


if __name__ == "__main__":
    unittest.main()