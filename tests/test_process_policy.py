from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock

from kwikledgers_langgraph.adapters import ProcessSnapshotPorts, TechnicalDebtPreparationPorts
from kwikledgers_langgraph.graphs.process_snapshot import build_process_snapshot_graph
from kwikledgers_langgraph.graphs.technical_debt_preparation import build_technical_debt_preparation_graph
from kwikledgers_langgraph.process_policy import analyze_snapshot, classify_scope, coordinate_deadlines, prepare_technical_debt


def debt_request():
    item = {"title": "Corrigir contrato", "context": "Contexto <script>unsafe</script>",
            "problem": "Problema", "impact": "Impacto", "approach": "Abordagem",
            "acceptance_criteria": "Testes aprovados.", "developer_suggestion": "Validar regressões."}
    return {"repository_name": "kl_store", "iteration_path": "KL\\2026\\Sprint 20",
            "parent": dict(item), "children": [dict(item)], "task_acceptance_supported": False}


class ProcessPolicyTests(TestCase):
    def test_scope_deployment_never_blocks_technical_classification(self):
        self.assertEqual(classify_scope({"title": "Implantação do backend"}), "operational")
        self.assertEqual(classify_scope({"title": "Frontend Angular"}), "frontend")
        self.assertEqual(classify_scope({"title": "API"}), "technical")

    def test_snapshot_preserves_all_types_closed_tasks_and_children(self):
        result = analyze_snapshot([
            {"id": 1, "type": "Technical Debt", "status": "Active", "children": [
                {"id": 2, "type": "Task", "status": "Closed", "title": "Implantação"},
                {"id": 3, "type": "Task", "status": "Active", "title": "API", "tags": "Bloqueado;Devolvido"},
            ]}, {"id": 3, "type": "Task", "status": "Active"},
            {"id": 4, "type": "Spike", "status": "Returned"},
        ])
        self.assertEqual(result["type_counts"], {"Technical Debt": 1, "Task": 2, "Spike": 1})
        self.assertEqual(result["task_status_counts"], {"Closed": 1, "Active": 1})
        self.assertEqual(result["blocked_ids"], [3])
        self.assertEqual(result["returned_ids"], [3, 4])
        self.assertEqual(result["scope"]["operational"], [2])

    def test_deadline_cadence_and_old_unreviewed_pr(self):
        now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
        result = coordinate_deadlines({"sprint_end": "2026-09-30T20:00:00+00:00",
            "pull_requests": [{"id": 1, "author": "dev", "created_at": "2026-09-26T10:00:00+00:00", "reviewed": False}]}, now)
        self.assertEqual(result[0]["cadence_hours"], 2)
        self.assertEqual(result[1]["kind"], "unreviewed_pr")
        self.assertEqual(coordinate_deadlines({"sprint_end": "2026-10-02T20:00:00+00:00"}, now)[0]["cadence_hours"], 24)

    def test_deadline_rejects_naive_clock(self):
        with self.assertRaises(ValueError):
            coordinate_deadlines({}, datetime(2026, 9, 29))

    def test_debt_html_title_full_family_fibonacci_and_task_acceptance_fallback(self):
        result = prepare_technical_debt(debt_request(), {"exact_match": False, "similar_items": []}, "AI_Tracking/context.json")
        self.assertEqual(result["parent"]["title"], "[KL STORE] Corrigir contrato")
        self.assertEqual(result["parent"]["story_points"], 1)
        self.assertIn("&lt;script&gt;", result["parent"]["description"])
        self.assertNotIn("<script>", result["parent"]["description"])
        child = result["children"][0]
        self.assertEqual(child["parent_work_item_id"], "$created_parent")
        self.assertIn("Critérios de aceite", child["description"])
        self.assertIn("Sugestão ao dev", child["description"])
        self.assertNotIn("acceptance_criteria", child)

    def test_non_fibonacci_effort_and_unrequested_severity_rejected(self):
        request = debt_request()
        request["parent"]["story_points"] = 4
        with self.assertRaises(ValueError):
            prepare_technical_debt(request, {"exact_match": False, "similar_items": []}, "context")
        request = debt_request()
        request["parent"]["title"] = "Problema grave"
        with self.assertRaises(ValueError):
            prepare_technical_debt(request, {"exact_match": False, "similar_items": []}, "context")

    def test_debt_graph_similarity_context_then_full_cache_without_board_write(self):
        preparation = Mock()
        request = debt_request()
        request["implementation_indicated"] = True
        preparation.resolve.return_value = request
        preparation.find_similar.return_value = {"exact_match": True, "similar_items": [{"id": 3}]}
        preparation.save_repository_context.return_value = "AI_Tracking/Repo_Work_Item_Context/technical_debts/kl_store.json"
        preparation.save_preview_cache.return_value = "AI_Tracking/Repo_Work_Item_Context/technical_debt_previews/kl_store.json"
        tracking = Mock()
        graph = build_technical_debt_preparation_graph(TechnicalDebtPreparationPorts(preparation, tracking))
        result = graph.invoke({"request": request})
        self.assertEqual(result["outcome"], "preview_only")
        preview = result["results"]["technical_debt_preview"]
        self.assertTrue(preview["creation_blocked"])
        self.assertTrue(preview["preview_cache_path"])
        preparation.find_similar.assert_called_once_with("kl_store", "[KL STORE] Corrigir contrato", request["parent"]["context"])
        cached = preparation.save_preview_cache.call_args.args[1]
        self.assertIn("parent", cached)
        self.assertEqual(len(cached["children"]), 1)
        self.assertEqual([call[0] for call in preparation.method_calls],
                         ["resolve", "find_similar", "save_repository_context", "save_preview_cache"])

    def test_similarity_failure_blocks_preparation(self):
        preparation = Mock()
        preparation.resolve.return_value = debt_request()
        preparation.find_similar.side_effect = OSError()
        graph = build_technical_debt_preparation_graph(TechnicalDebtPreparationPorts(preparation, Mock()))
        self.assertEqual(graph.invoke({"request": {}})["outcome"], "blocked")
        preparation.save_preview_cache.assert_not_called()

    def test_snapshot_graph_reads_provider_not_untrusted_state(self):
        provider, tracking, clock = Mock(), Mock(), Mock()
        provider.fetch_snapshot.return_value = {"items": [{"id": 1, "type": "Task", "status": "Closed", "title": "Implantação"}]}
        clock.now.return_value = "2026-09-29T12:00:00+00:00"
        graph = build_process_snapshot_graph(ProcessSnapshotPorts(provider, tracking, clock))
        result = graph.invoke({"summary": {"task_ids": [999]}})
        self.assertEqual(result["summary"]["task_ids"], [1])
        tracking.record.assert_called_once()

    def test_snapshot_tracking_failure_returns_no_success_summary(self):
        provider, tracking, clock = Mock(), Mock(), Mock()
        provider.fetch_snapshot.return_value = {"items": []}
        clock.now.return_value = "2026-09-29T12:00:00+00:00"
        tracking.record.side_effect = OSError()
        graph = build_process_snapshot_graph(ProcessSnapshotPorts(provider, tracking, clock))
        result = graph.invoke({})
        self.assertEqual(result["summary"], {})
        self.assertTrue(result["errors"])

    def test_next_sprint_uses_current_year_and_main_number(self):
        request = debt_request()
        request.update({"target_iteration": "next_sprint", "current_sprint_number": 20,
                        "current_year": 2026, "iteration_root": "KL"})
        result = prepare_technical_debt(request, {"exact_match": False, "similar_items": []}, "context")
        self.assertEqual(result["parent"]["iteration_path"], "KL\\2026\\Sprint 21")
        self.assertEqual(result["children"][0]["iteration_path"], "KL\\2026\\Sprint 21")