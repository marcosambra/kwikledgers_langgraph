from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from kwikledgers_langgraph.fastmcp_execution_capability import (
    ExecutionCapabilityError,
    issue_lifecycle_execution_capability,
)
from kwikledgers_langgraph.lifecycle_adapters import (
    ExplicitLocalApproval,
    active_state,
    approval_binding,
    canonical,
    lifecycle_scope,
)


class AuthorizationFixture:
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime.now(timezone.utc)
        self.approval_key = secrets.token_bytes(32)
        self.execution_key = secrets.token_hex(32)
        self.approvals = ExplicitLocalApproval(self.approval_key, clock=lambda: self.now)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger = Path(temporary.name) / "capabilities.sqlite3"
        self.arguments = {"work_item_id": 11, "assigned_to": "fixture-human", "area_path": "Project",
                  "status": "Active", "expected_revision": 2}
        self.environment = patch.dict(
            os.environ, {"KWIKLEDGERS_FASTMCP_EXECUTION_KEY": self.execution_key,
                         "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(self.ledger)},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def approved_state(
        self, action: str = "activate", *, run_id: str = "test-run", request_id: str = "test-request",
    ) -> dict:
        preview = deepcopy(self.arguments)
        if action == "activate":
            preview["task_id"] = preview.pop("work_item_id")
        pending = {
            "action": action,
            "preview": preview,
            "preview_digest": hashlib.sha256(json.dumps(preview, sort_keys=True, allow_nan=False).encode()).hexdigest(),
            "previewed_at": (self.now - timedelta(seconds=2)).isoformat(),
            "run_id": run_id,
            "request_id": request_id,
            "approved": True,
        }
        pending["confirmation"] = {**approval_binding(pending), "decision": "approve", "source": "human",
                                   "subject": "fixture-human",
                                   "confirmed_at": (self.now - timedelta(seconds=1)).isoformat()}
        return {
            "run_id": run_id, "observability": {"active_run_id": run_id},
            "context": {"repository": "fixture-repository"}, "pending_approval": pending,
        }

    def issue(
        self, *, action: str = "activate", capability: str = "update_work_item_content",
        now: datetime | None = None,
    ) -> str:
        with lifecycle_scope(self.approved_state(action), self.approvals):
            return issue_lifecycle_execution_capability(
                active_state(action), action=action, capability_name=capability,
                arguments=self.arguments, now=now or self.now,
            )

    def decode(self, token: str) -> dict:
        encoded, _signature = token.split(".", 1)
        return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))

    def sign_execution_payload(self, payload: dict) -> str:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(serialized).decode().rstrip("=")
        return f"{encoded}.local-v1"


class LifecycleIssuerAuthorizationTests(AuthorizationFixture, unittest.TestCase):
    def test_genuine_independently_signed_receipt_binds_active_action(self) -> None:
        self.assertGreaterEqual(len(self.approval_key), 32)
        self.assertNotEqual(self.approval_key, self.execution_key.encode())
        state = self.approved_state()
        binding = approval_binding(state["pending_approval"])
        self.assertTrue(self.approvals.verify(binding, state["pending_approval"]["confirmation"]))
        with lifecycle_scope(state, self.approvals):
            trusted = active_state("activate")
            self.assertEqual(trusted["verified_lifecycle_binding"], binding)
            token = issue_lifecycle_execution_capability(
                trusted, action="activate", capability_name="update_work_item_content",
                arguments=self.arguments, now=self.now,
            )
        payload = self.decode(token)
        self.assertEqual(payload["run_id"], binding["run_id"])
        self.assertEqual(payload["request_id"], binding["request_id"])
        self.assertEqual(payload["capability"], "update_work_item_content")

    def test_catalog_allows_only_the_capability_for_each_authenticated_action(self) -> None:
        cases = (("activate", "update_work_item_content"), ("debt_parent", "create_work_item"),
                 ("debt_child_0", "create_work_item"), ("debt_child_12", "create_work_item"))
        for action, capability in cases:
            with self.subTest(action=action), lifecycle_scope(self.approved_state(action), self.approvals):
                trusted = active_state(action)
                token = issue_lifecycle_execution_capability(
                    trusted, action=action, capability_name=capability, arguments=self.arguments,
                )
                self.assertEqual(self.decode(token)["capability"], capability)
                with self.assertRaises(ExecutionCapabilityError):
                    issue_lifecycle_execution_capability(
                        trusted, action=action, capability_name="send_notification", arguments=self.arguments,
                    )

    def test_unknown_and_noncanonical_debt_actions_are_rejected(self) -> None:
        for action in ("unknown", "debt_child_01", "debt_child_-1", "debt_child_1x"):
            with self.subTest(action=action), lifecycle_scope(self.approved_state(action), self.approvals):
                with self.assertRaises(ExecutionCapabilityError):
                    issue_lifecycle_execution_capability(
                        active_state(action), action=action, capability_name="create_work_item", arguments={},
                    )

    def test_authenticated_state_cannot_be_reused_outside_scope(self) -> None:
        with lifecycle_scope(self.approved_state(), self.approvals):
            trusted = active_state("activate")
        with self.assertRaises(ValueError):
            issue_lifecycle_execution_capability(
                trusted, action="activate", capability_name="update_work_item_content", arguments=self.arguments,
            )

    def test_changed_technical_arguments_cannot_be_authorized(self) -> None:
        with lifecycle_scope(self.approved_state(), self.approvals):
            for field, value in (("work_item_id", 12), ("status", "Closed"), ("expected_revision", 3)):
                with self.subTest(field=field), self.assertRaises(ExecutionCapabilityError):
                    issue_lifecycle_execution_capability(
                        active_state("activate"), action="activate", capability_name="update_work_item_content",
                        arguments={**self.arguments, field: value},
                    )

    def test_tracking_only_scope_and_foreign_action_cannot_issue(self) -> None:
        state = self.approved_state()
        state["verified_lifecycle_binding"] = approval_binding(state["pending_approval"])
        with lifecycle_scope(state):
            self.assertNotIn("verified_lifecycle_binding", active_state())
            with self.assertRaises(ValueError):
                issue_lifecycle_execution_capability(
                    active_state(), action="activate", capability_name="update_work_item_content", arguments={},
                )
        with lifecycle_scope(state, self.approvals):
            with self.assertRaises(ValueError):
                issue_lifecycle_execution_capability(
                    active_state("activate"), action="close", capability_name="update_work_item_effort", arguments={},
                )

    def test_total_state_equality_rejects_changed_preview_binding_and_context(self) -> None:
        with lifecycle_scope(self.approved_state(), self.approvals):
            for field in ("pending_approval", "verified_lifecycle_binding", "context", "extra"):
                with self.subTest(field=field):
                    changed = active_state("activate")
                    if field in ("pending_approval", "verified_lifecycle_binding"):
                        changed[field]["preview"]["work_item_id"] = 12
                    elif field == "context":
                        changed[field]["repository"] = "foreign-repository"
                    else:
                        changed[field] = True
                    with self.assertRaises(ExecutionCapabilityError):
                        issue_lifecycle_execution_capability(
                            changed, action="activate", capability_name="update_work_item_content",
                            arguments=self.arguments,
                        )

    def test_changed_preview_and_tampered_receipt_cannot_open_authenticated_scope(self) -> None:
        for mutation in ("preview", "signature", "independent_key", "action"):
            with self.subTest(mutation=mutation):
                state = self.approved_state()
                pending = state["pending_approval"]
                receipt = pending["confirmation"]
                if mutation == "preview":
                    pending["preview"]["work_item_id"] = 12
                    pending["preview_digest"] = hashlib.sha256(
                        json.dumps(pending["preview"], sort_keys=True, allow_nan=False).encode(),
                    ).hexdigest()
                elif mutation == "signature":
                    receipt["signature"] = "0" * 64
                elif mutation == "independent_key":
                    receipt["claims"] = {"decision": "approve"}
                else:
                    pending["action"] = "close"
                with self.assertRaises(ValueError):
                    with lifecycle_scope(state, self.approvals):
                        self.fail("Invalid approval entered authenticated scope")

    def test_receipt_expiration_is_rechecked_at_issuance(self) -> None:
        with lifecycle_scope(self.approved_state(), self.approvals):
            trusted = active_state("activate")
            self.now -= timedelta(seconds=2)
            with self.assertRaises(ValueError):
                issue_lifecycle_execution_capability(
                    trusted, action="activate", capability_name="update_work_item_content", arguments=self.arguments,
                )

    def test_missing_identifiers_and_naive_issuance_time_are_rejected(self) -> None:
        for run_id, request_id in (("", "test-request"), ("test-run", ""), ("test-run", " ")):
            with self.subTest(run_id=run_id, request_id=request_id):
                with self.assertRaises((ValueError, ExecutionCapabilityError)):
                    with lifecycle_scope(self.approved_state(run_id=run_id, request_id=request_id), self.approvals):
                        issue_lifecycle_execution_capability(
                            active_state("activate"), action="activate", capability_name="update_work_item_content",
                            arguments=self.arguments,
                        )
        with self.assertRaises(ExecutionCapabilityError):
            self.issue(now=self.now.replace(tzinfo=None))


class FastMcpAuthorizationIntegrationTests(AuthorizationFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger = Path(temporary.name) / "execution-capabilities.sqlite3"
        environment = patch.dict(os.environ, {"KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(self.ledger)})
        environment.start()
        self.addCleanup(environment.stop)
        self.fastmcp_root = Path(__file__).resolve().parents[2] / "kwikledgers_fastmcp"
        self.python = self.fastmcp_root / ".venv/bin/python"
        self.verifier_source = self.fastmcp_root / "src/kwikledgers_fastmcp/shared/execution_capability.py"
        if not self.python.is_file():
            self.skipTest("Separate FastMCP .venv/bin/python is required")
        self.assertGreaterEqual(sys.version_info[:2], (3, 11), "LangGraph requires Python 3.11 or newer")
        self.assertTrue(self.verifier_source.is_file())

    def issue_local(self, *, without_receipt: bool = False) -> str:
        self.execution_key = ""
        with patch.dict(os.environ, {
            "KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "",
            "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(self.ledger),
        }):
            if not without_receipt:
                return self.issue()
            trusted = self.approved_state()
            trusted["pending_approval"].pop("confirmation")
            trusted["verified_lifecycle_binding"] = approval_binding(trusted["pending_approval"])
            with patch("kwikledgers_langgraph.lifecycle_adapters.active_state", return_value=deepcopy(trusted)):
                return issue_lifecycle_execution_capability(
                    trusted, action="activate", capability_name="update_work_item_content", arguments=self.arguments,
                    now=self.now,
                )

    def verify(
        self, token: str, *, arguments: dict | None = None,
        capability: str = "update_work_item_content", consume: bool = True,
        rejected: str | None = None,
    ) -> dict:
        script = """
import importlib.util
import json
from pathlib import Path
import sys

assert sys.version_info[:2] >= (3, 10), "FastMCP requires Python 3.10 or newer"
assert Path(sys.prefix).resolve() == Path(sys.argv[2]).resolve(), "Separate FastMCP venv required"
source = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location("real_fastmcp_execution_capability", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert Path(module.__file__).resolve() == source
request = json.load(sys.stdin)
try:
    module.verify_execution_capability(**request)
except module.ExecutionCapabilityError as error:
    print(json.dumps({"accepted": False, "error": str(error), "source": str(source)}))
    sys.exit(3)
print(json.dumps({"accepted": True, "source": str(source)}))
"""
        result = subprocess.run(
            [str(self.python), "-I", "-B", "-c", script, str(self.verifier_source), str(self.fastmcp_root / ".venv")],
            input=json.dumps({
                "token": token, "capability_name": capability,
                "arguments": self.arguments if arguments is None else arguments, "consume": consume,
            }),
            text=True, capture_output=True, timeout=15, cwd=self.ledger.parent,
            env={
                "KWIKLEDGERS_FASTMCP_EXECUTION_KEY": self.execution_key,
                "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(self.ledger),
            },
        )
        self.assertEqual(result.returncode, 0 if rejected is None else 3, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["source"], str(self.verifier_source.resolve()))
        self.assertEqual(response["accepted"], rejected is None)
        if rejected is not None:
            self.assertIn(rejected, response["error"])
        return response

    def test_valid_token_consumes_and_shared_ledger_blocks_process_replay(self) -> None:
        token = self.issue()
        self.verify(token)
        payload = self.decode(token)
        with sqlite3.connect(self.ledger) as connection:
            rows = connection.execute("SELECT nonce_digest FROM execution_capabilities").fetchall()
        self.assertEqual(rows, [(hashlib.sha256(payload["nonce"].encode("ascii")).hexdigest(),)])
        self.verify(token, rejected="consumida")

    def test_validation_only_does_not_consume_authorization(self) -> None:
        token = self.issue()
        self.verify(token, consume=False)
        self.assertTrue(self.ledger.exists())
        self.verify(token)
        self.verify(token, rejected="consumida")

    def test_changed_payload_and_wrong_capability_do_not_burn_valid_token(self) -> None:
        token = self.issue()
        self.verify(token, arguments={**self.arguments, "work_item_id": 12}, rejected="payload")
        self.verify(token, capability="update_work_item_effort", rejected="operação")
        self.assertTrue(self.ledger.exists())
        self.verify(token)

    def test_expired_issuance_and_future_issuance_are_rejected(self) -> None:
        for issued, rejection in ((self.now - timedelta(minutes=6), "expirou"),
                                  (self.now + timedelta(minutes=2), "futuro")):
            with self.subTest(issued=issued):
                with self.assertRaises((ValueError, ExecutionCapabilityError)):
                    self.issue(now=issued)
                payload = self.decode(self.issue())
                payload["issued_at"] = issued.isoformat()
                payload["expires_at"] = (issued + timedelta(minutes=5)).isoformat()
                self.verify(self.sign_execution_payload(payload), rejected=rejection)
        self.assertTrue(self.ledger.exists())

    def test_tampered_signature_is_rejected_without_consuming(self) -> None:
        token = self.issue()
        replacement = "0" if token[-1] != "0" else "1"
        self.verify(token[:-1] + replacement, rejected="inválida")
        self.assertTrue(self.ledger.exists())
        self.verify(token)

    def test_distinct_signed_tokens_with_repeated_nonce_are_rejected(self) -> None:
        with patch("kwikledgers_langgraph.fastmcp_execution_capability.uuid4", return_value=UUID(int=1)):
            first = self.issue(now=self.now - timedelta(seconds=1))
            with self.assertRaises(ExecutionCapabilityError):
                self.issue(now=self.now)
        self.verify(first)
        self.verify(first, rejected="consumida")

    def test_genuinely_signed_tokens_missing_ids_or_timezone_are_rejected(self) -> None:
        token = self.issue()
        for field in ("run_id", "request_id"):
            for value in (None, "", " "):
                with self.subTest(field=field, value=value):
                    payload = self.decode(token)
                    if value is None:
                        del payload[field]
                    else:
                        payload[field] = value
                    self.verify(self.sign_execution_payload(payload), rejected="campos válidos")
        for field in ("issued_at", "expires_at"):
            with self.subTest(field=field):
                payload = self.decode(token)
                payload[field] = datetime.fromisoformat(payload[field]).replace(tzinfo=None).isoformat()
                self.verify(self.sign_execution_payload(payload), rejected="fuso")
        self.assertTrue(self.ledger.exists())
        self.verify(token)

    def test_local_token_is_interoperable_and_replay_is_blocked_across_processes(self) -> None:
        token = self.issue_local()
        self.assertTrue(token.endswith(".local-v1"))
        self.verify(token)
        self.verify(token, rejected="consumida")

    def test_local_receiptless_verified_state_issues_interoperable_token(self) -> None:
        token = self.issue_local(without_receipt=True)
        payload = self.decode(token)
        self.assertEqual(payload["action"], "activate")
        self.assertEqual(payload["run_id"], "test-run")
        self.assertEqual(payload["request_id"], "test-request")
        self.verify(token, consume=False)
        self.verify(token)
        self.verify(token, rejected="consumida")

    def test_local_payload_operation_and_revision_changes_are_rejected_before_consumption(self) -> None:
        token = self.issue_local()
        self.verify(token, arguments={**self.arguments, "work_item_id": 12}, rejected="payload")
        self.verify(token, arguments={**self.arguments, "expected_revision": 3}, rejected="payload")
        self.verify(token, capability="update_work_item_effort", rejected="operação")
        self.verify(token)

    def test_local_metadata_and_nonce_rewrites_cannot_bypass_the_shared_registry(self) -> None:
        token = self.issue_local()
        payload = self.decode(token)
        self.verify(token)
        for field, value in (("run_id", "foreign-run"), ("request_id", "foreign-request"),
                             ("nonce", "0" * 32), ("action", "close")):
            serialized = json.dumps({**payload, field: value}, ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")).encode()
            changed = base64.urlsafe_b64encode(serialized).decode().rstrip("=") + ".local-v1"
            with self.subTest(field=field):
                self.verify(changed, rejected="registro")

    def test_local_token_cannot_be_verified_without_the_issuance_ledger(self) -> None:
        token = self.issue_local()
        self.ledger = self.ledger.parent / "another-ledger.sqlite3"
        self.verify(token, rejected="registro")
        self.assertFalse(self.ledger.exists())