import json
from unittest import TestCase
from unittest.mock import patch

from kwikledgers_langgraph.graph_catalog import InspectionPort, graph_catalog


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