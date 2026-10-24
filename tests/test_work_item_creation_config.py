from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from kwikledgers_langgraph.adapters import WorkItemCreationConfigPorts
from kwikledgers_langgraph.backend_factory import BackendSelectionError, build_work_item_creation_config_ports
from kwikledgers_langgraph.graphs.work_item_creation_config import (
    build_work_item_creation_config_graph, prepare_operator_configuration,
)
from kwikledgers_langgraph.work_item_creation import CreationValidationError, prepare_creation, format_creation_rich_text
from kwikledgers_langgraph.work_item_creation_config_cli import main


def native_field(reference, *, required=False, read_only=False, default=None, allowed=None):
    return {"reference_name": reference, "read_only": read_only, "always_required": required,
            "default_value": default, "allowed_values": [] if allowed is None else allowed}


def native_metadata():
    return {
        "version": 1, "project": "KL", "area": "Squad",
        "work_item_types": [
            {"name": kind, "fields": [
                native_field("System.Title", required=True),
                native_field("System.Description"),
                native_field("System.AreaPath", required=True),
                native_field("System.IterationPath", required=True, default="KL\\2026\\Sprint 20"),
                native_field("System.AreaId", required=True),
                native_field("System.IterationId", required=True),
                native_field("System.AssignedTo"),
                native_field("System.State", required=True),
                native_field("System.Reason", required=True),
                native_field("System.Id", required=True, read_only=True),
                native_field("Microsoft.VSTS.Common.Priority", required=True, default=2),
                native_field("Microsoft.VSTS.Scheduling.CompletedWork", default=0),
                native_field("Custom.Category", allowed=["Infrastructure"]),
            ] + ([native_field("Microsoft.VSTS.Scheduling.StoryPoints")] if kind == "User Story" else [])}
            for kind in ("User Story", "Task", "Bug")
        ],
    }


class WorkItemCreationConfigTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repositories = {"accountant_frontend": self.directory.name}
        self.metadata = native_metadata()
        self.client = Mock()
        self.client.call.return_value = self.metadata
        self.ports = build_work_item_creation_config_ports(backend_name="fastmcp", client=self.client)
        self.state = {"work_item_creation_config_request": {"repositories": self.repositories}}

    def test_native_configuration_is_accepted_without_changing_creation_schema(self):
        result = build_work_item_creation_config_graph(self.ports).invoke(self.state)
        self.client.call.assert_called_once_with("get_work_item_process_metadata", {})
        self.assertFalse(result.get("errors"))
        self.assertNotIn("pending_approval", result)
        operator = result["work_item_creation_config_result"]["operator"]
        self.assertEqual(set(operator), {"project", "area", "repositories", "process_constraints"})
        self.assertEqual(operator["repositories"], self.repositories)
        constraint = operator["process_constraints"]["Task"]
        self.assertEqual(constraint["native_type"], "Task")
        self.assertNotIn("System.State", constraint["required_fields"])
        self.assertNotIn("System.Id", constraint["supported_fields"])
        self.assertNotIn("System.AreaId", constraint["required_fields"])
        self.assertIn("System.AreaPath", constraint["required_fields"])
        self.assertNotIn("System.IterationId", constraint["required_fields"])
        self.assertIn("System.IterationPath", constraint["required_fields"])
        self.assertNotIn("Custom.Category", constraint["supported_fields"])
        self.assertEqual(constraint["defaults"]["Microsoft.VSTS.Scheduling.CompletedWork"], 0)
        arguments, kind = prepare_creation({
            "work_item_type": "Task", "title": "Configurar TLS", "repository_name": "accountant_frontend",
            "parent_work_item_id": 12924, "completed_work_hours": 5,
        }, operator, format_creation_rich_text)
        self.assertEqual(kind.value, "Task")
        self.assertEqual(arguments["area_path"], "KL\\Squad")
        self.assertEqual(arguments["iteration_path"], "KL\\2026\\Sprint 20")
        self.assertEqual(arguments["parent_work_item_id"], 12924)
        self.assertEqual(arguments["completed_work_hours"], 5)
        self.assertEqual(result["observability"]["timeline"][-1]["status"], "completed")
        self.assertEqual(result["tracking_events"][-1]["category"], "work_item_creation_config")

    def test_allowed_values_are_preserved_and_enforced_by_existing_creation(self):
        self.metadata["work_item_types"][1]["fields"].append(
            native_field("System.Tags", allowed=["Infrastructure", "TLS"]))
        operator = prepare_operator_configuration(self.metadata, self.repositories)
        constraint = operator["process_constraints"]["Task"]
        self.assertEqual(constraint["allowed_values"]["System.Tags"], ["Infrastructure", "TLS"])
        with self.assertRaisesRegex(CreationValidationError, "violates process constraint"):
            prepare_creation({"work_item_type": "Task", "title": "TLS", "repository_name": "accountant_frontend",
                              "parent_work_item_id": 12924, "tags": "Other"}, operator, format_creation_rich_text)

    def test_virtual_fields_do_not_invent_write_permissions_or_enumerated_restrictions(self):
        self.metadata["work_item_types"][0]["fields"].append(
            native_field("System.IterationLevel7", read_only=None, allowed=None))
        self.metadata["work_item_types"][1]["fields"][0]["allowed_values"] = None
        operator = prepare_operator_configuration(self.metadata, self.repositories)
        self.assertNotIn("System.IterationLevel7", operator["process_constraints"]["UserStory"]["supported_fields"])
        self.assertNotIn("System.Title", operator["process_constraints"]["Task"]["allowed_values"])
        self.metadata["work_item_types"][1]["fields"][0]["read_only"] = None
        with self.assertRaisesRegex(CreationValidationError, "writable field metadata is missing"):
            prepare_operator_configuration(self.metadata, self.repositories)

    def test_missing_or_invalid_repository_blocks_before_metadata_read(self):
        for repositories in ({}, {"frontend": "/missing/kwikledgers/repository"}, {"frontend": ""}):
            with self.subTest(repositories=repositories):
                result = build_work_item_creation_config_graph(self.ports).invoke(
                    {"work_item_creation_config_request": {"repositories": repositories}})
                self.assertEqual(result["work_item_creation_config_result"]["status"], "blocked")
                self.assertTrue(result["errors"])
        self.client.call.assert_not_called()

    def test_native_read_failure_is_visible_and_never_returns_a_configuration(self):
        self.client.call.side_effect = RuntimeError("Native metadata unavailable")
        result = build_work_item_creation_config_graph(self.ports).invoke(self.state)
        self.assertEqual(result["errors"], ["Native metadata unavailable"])
        self.assertEqual(result["work_item_creation_config_result"], {"status": "blocked"})
        self.assertEqual(result["observability"]["timeline"][-1]["status"], "failed")
        self.assertNotIn("operator", result["work_item_creation_config_result"])

    def test_required_custom_field_is_preserved_and_blocks_only_the_affected_creation_kind(self):
        self.metadata["work_item_types"][1]["fields"].append(native_field("Custom.Required", required=True))
        operator = prepare_operator_configuration(self.metadata, self.repositories)
        self.assertIn("Custom.Required", operator["process_constraints"]["Task"]["required_fields"])
        with self.assertRaisesRegex(CreationValidationError, "Custom.Required"):
            prepare_creation({"work_item_type": "Task", "title": "TLS", "repository_name": "accountant_frontend",
                              "parent_work_item_id": 12924}, operator, format_creation_rich_text)
        prepare_creation({"work_item_type": "UserStory", "title": "TLS", "repository_name": "accountant_frontend",
                          "story_points": 3}, operator, format_creation_rich_text)

    def test_incomplete_or_ambiguous_metadata_is_rejected(self):
        variants = []
        for version in (0, True, None):
            variants.append({**self.metadata, "version": version})
        variants.append({**self.metadata, "work_item_types": []})
        variants.append({**self.metadata, "area": ""})
        for field in ("always_required", "read_only", "allowed_values", "default_value"):
            variant = deepcopy(self.metadata)
            del variant["work_item_types"][1]["fields"][0][field]
            variants.append(variant)
        variant = deepcopy(self.metadata)
        variant["work_item_types"][1] = variant["work_item_types"][0]
        variants.append(variant)
        variant = deepcopy(self.metadata)
        variant["work_item_types"][1]["fields"].append(variant["work_item_types"][1]["fields"][0])
        variants.append(variant)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(CreationValidationError):
                prepare_operator_configuration(variant, self.repositories)

    def test_backend_factory_has_no_legacy_fallback(self):
        self.assertIsInstance(self.ports, WorkItemCreationConfigPorts)
        with self.assertRaises(BackendSelectionError):
            build_work_item_creation_config_ports(backend_name="legacy", client=self.client)

    def invoke_cli(self, output, *extra):
        stdout, stderr = StringIO(), StringIO()
        with patch("kwikledgers_langgraph.work_item_creation_config_cli.build_work_item_creation_config_ports",
                   return_value=self.ports), \
                patch("kwikledgers_langgraph.work_item_creation_config_cli.append_runtime_observability_artifacts") as metrics, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--repository", f"accountant_frontend={self.directory.name}",
                         "--output", str(output), *extra])
        return code, stdout.getvalue(), stderr.getvalue(), metrics

    def test_cli_persists_plain_operator_json_for_the_existing_creation_entry(self):
        output = Path(self.directory.name) / "operator.json"
        code, stdout, stderr, metrics = self.invoke_cli(output)
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["outcome"], "prepared")
        self.assertFalse(summary["azure_write"])
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved, prepare_operator_configuration(self.metadata, self.repositories))
        metrics.assert_called_once()
        self.assertEqual(metrics.call_args.args[0]["observability"]["timeline"][-1]["node"], "persist_configuration")

    def test_cli_never_writes_failed_metadata_or_overwrites_existing_configuration(self):
        output = Path(self.directory.name) / "operator.json"
        self.client.call.side_effect = RuntimeError("Metadata unavailable")
        code, stdout, stderr, _ = self.invoke_cli(output)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stderr)["outcome"], "blocked")
        self.assertEqual(stdout, "")
        self.assertFalse(output.exists())
        output.write_text("Existing operator config", encoding="utf-8")
        self.client.reset_mock()
        code, _, stderr, _ = self.invoke_cli(output)
        self.assertEqual(code, 1)
        self.assertIn("already exists", stderr)
        self.assertEqual(output.read_text(encoding="utf-8"), "Existing operator config")
        self.client.call.assert_not_called()

    def test_cli_replace_overwrites_only_after_metadata_is_read(self):
        output = Path(self.directory.name) / "operator.json"
        output.write_text("Existing operator config", encoding="utf-8")
        self.client.call.side_effect = RuntimeError("Metadata unavailable")
        code, _, _, _ = self.invoke_cli(output, "--replace")
        self.assertEqual(code, 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "Existing operator config")
        self.client.reset_mock(side_effect=True)
        self.client.call.side_effect = None
        code, stdout, stderr, _ = self.invoke_cli(output, "--replace")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["outcome"], "prepared")
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")),
                         prepare_operator_configuration(self.metadata, self.repositories))

    def test_cli_rejects_duplicate_repositories_before_reading_metadata(self):
        code, _, stderr, _ = self.invoke_cli(Path(self.directory.name) / "operator.json",
                                            "--repository", "accountant_frontend=/another")
        self.assertEqual(code, 1)
        self.assertIn("unique NAME=PATH", stderr)
        self.client.call.assert_not_called()
