from contextlib import redirect_stdout
from importlib.util import find_spec
import io
import json
from unittest import TestCase
from unittest.mock import patch

from kwikledgers_langgraph.graph_catalog import InspectionPort, graph_catalog, main, operational_catalog


class GraphCatalogTests(TestCase):
    def test_catalog_contains_actual_topologies_without_executing_ports(self):
        with patch.object(InspectionPort, "blocked", side_effect=AssertionError("Unexpected IO")) as blocked:
            catalog = graph_catalog()
        blocked.assert_not_called()
        self.assertEqual(len(catalog), 12)
        json.dumps(catalog, allow_nan=False)
        for graph in catalog:
            with self.subTest(graph=graph["id"]):
                identifiers = {node["id"] for node in graph["nodes"]}
                self.assertIn("__start__", identifiers)
                self.assertIn("__end__", identifiers)
                self.assertTrue(graph["edges"])
                self.assertTrue(all(edge["source"] in identifiers and edge["target"] in identifiers for edge in graph["edges"]))
                self.assertIn("graph", graph["mermaid"])
                self.assertNotIn("invoke", graph)
        start = next(graph for graph in catalog if graph["id"] == "story_start")
        self.assertTrue(any("gate" in node["id"] for node in start["nodes"]))

    def test_inspection_ports_deny_effects(self):
        with self.assertRaisesRegex(RuntimeError, "cannot execute"):
            InspectionPort().create_story_pull_request({"story_id": 10})

    def test_operational_catalog_is_versioned_and_does_not_execute_integrations(self):
        with patch.object(InspectionPort, "blocked", side_effect=AssertionError("Unexpected IO")) as blocked, \
                patch("kwikledgers_langgraph.fastmcp_client.FastMCPClient.call",
                      side_effect=AssertionError("Unexpected MCP")) as call:
            catalog = operational_catalog()
        blocked.assert_not_called()
        call.assert_not_called()
        self.assertEqual(catalog["version"], 1)
        self.assertTrue(catalog["inspection_only"])
        self.assertEqual(catalog["integration_checks"], "not_performed")
        json.dumps(catalog, allow_nan=False)
        names = [flow["id"] for flow in catalog["flows"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), {item["id"] for item in graph_catalog()} |
                         {"technical_debt_execute", "work_item_creation"})

    def test_public_entries_resolve_and_inspection_only_graph_is_not_advertised_as_executable(self):
        flows = {flow["id"]: flow for flow in operational_catalog()["flows"]}
        snapshot = flows["process_snapshot"]
        self.assertIsNone(snapshot["entry_point"])
        self.assertEqual(snapshot["entry_status"], "not_exposed")
        for flow in flows.values():
            self.assertEqual(flow["runtime_status"], "not_checked")
            entry = flow["entry_point"]
            if entry is not None:
                self.assertEqual(entry[:2], ["python", "-m"])
                self.assertIsNotNone(find_spec(entry[2]))
                self.assertEqual(flow["entry_status"], "declared")
        self.assertEqual(flows["story_start"]["entry_point"][-1], "story_start")
        self.assertEqual(flows["task_closure"]["resume_option"], "--resume-state-file")
        self.assertEqual(flows["story_start"]["resume_option"], "--resume")
        self.assertEqual(flows["day_end_closure"]["approval"], "runtime_policy")

    def test_atomic_argument_fields_and_lifecycle_restrictions_come_from_existing_schemas(self):
        from kwikledgers_langgraph.atomic_runtime import (
            LIFECYCLE_ROUTES, MUTATION_SCHEMAS, PREVIEW_SCHEMAS, READ_SCHEMAS,
        )
        catalog = operational_catalog()
        capabilities = {entry["id"]: entry for entry in catalog["atomic_capabilities"]}
        expected = {**READ_SCHEMAS, **PREVIEW_SCHEMAS, **MUTATION_SCHEMAS}
        self.assertEqual(set(capabilities), set(expected))
        for name, schema in expected.items():
            entry = capabilities[name]
            self.assertEqual(entry["argument_fields"],
                             {"required": list(schema.required), "optional": list(schema.optional)})
            self.assertEqual(entry["approval"] == "explicit_after_preview", name in MUTATION_SCHEMAS)
            self.assertEqual(entry["runtime_status"], "not_checked")
        routes = {item["capability"]: item for item in catalog["lifecycle_required"]}
        self.assertEqual(set(routes), set(LIFECYCLE_ROUTES))
        self.assertTrue(all(item["direct_atomic_execution"] is False for item in routes.values()))
        self.assertTrue(set(routes).isdisjoint(capabilities))

    def test_cli_preserves_topology_output_and_exposes_operational_mode(self):
        for arguments in ([], ["--operational"]):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as output:
                main(arguments)
            value = json.loads(output.getvalue())
            if arguments:
                self.assertEqual(value["version"], 1)
            else:
                self.assertIsInstance(value, list)
                self.assertEqual(len(value), 12)