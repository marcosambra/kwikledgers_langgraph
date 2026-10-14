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
    write_state_file,
)


class StatePersistenceTestCase(unittest.TestCase):
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