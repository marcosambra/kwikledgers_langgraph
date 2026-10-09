from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from kwikledgers_langgraph.lifecycle_adapters import MCPDebtPreparation


class DebtNativeContractTests(TestCase):
    def test_similarity_uses_native_arguments_and_normalizes_duplicate_evidence(self):
        gateway = Mock()
        gateway.client.call.return_value = {"repository_name": "payment", "query_title": "Debt",
                                            "duplicate_found": True, "suggested_matches": [{"id": 10}]}
        preparation = MCPDebtPreparation(gateway, {"payment": Path(".")}, ("KL\\Sprint 1",))
        result = preparation.find_similar("payment", "Debt", "Context")
        gateway.client.call.assert_called_once_with("find_similar_technical_debts", {
            "repository_name": "payment", "title": "Debt", "description": "Context",
        })
        self.assertIs(result["exact_match"], True)
        self.assertEqual(result["similar_items"], [{"id": 10}])

    def test_incomplete_duplicate_evidence_fails_closed(self):
        gateway = Mock()
        gateway.client.call.return_value = {"repository_name": "payment", "query_title": "Debt"}
        preparation = MCPDebtPreparation(gateway, {"payment": Path(".")}, ())
        with self.assertRaises(ValueError):
            preparation.find_similar("payment", "Debt", "Context")