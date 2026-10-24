from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import StoryPrPublishPorts
from kwikledgers_langgraph.graphs.mutable_flow import (
    route_after_errors,
    run_execute_step,
    run_mutation_gate,
    run_preview_step,
    stop_without_execution,
    validate_required_request,
)
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_error

ACTION = "story_pr_publish.execute"


def verify_published_pull_request(preview: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Compare the published PR with the approved preview; return every divergence."""
    problems: list[str] = []
    pull_request = result.get("pull_request") if isinstance(result.get("pull_request"), dict) else {}
    if result.get("published") is not True:
        problems.append("O Azure não confirmou a publicação (published != true).")
    if pull_request.get("is_draft") is not False:
        problems.append("A PR continua como Draft.")
    if str(pull_request.get("status", "")).lower() != "active":
        problems.append("A PR não está ativa após a publicação.")
    if pull_request.get("pull_request_id") != preview.get("pull_request_id"):
        problems.append("A PR publicada difere da aprovada no preview.")
    if pull_request.get("target_ref_name") != preview.get("target_ref_name"):
        problems.append("O alvo da PR mudou durante a publicação.")
    return problems


def pending_after_publish(preview: dict[str, Any]) -> list[str]:
    steps = ["Conferir se o print de evidência está anexado à PR (obrigatório em frontend)."]
    steps.append("Quando todas as PRs da história estiverem publicadas, mover a história de Desenvolvimento para "
                 "Aguardando PR (grafo story_status_transition, com preview e aprovação).")
    if str(preview.get("target_ref_name", "")).endswith("stage-pre-prod"):
        steps.append("Se ainda não foi feito, rodar story_pr_cherry_pick para levar a PR ao stage-homolog.")
    return steps


def build_story_pr_publish_graph(ports: StoryPrPublishPorts) -> Any:
    graph = StateGraph(AgentState)

    def validate_request(state: AgentState) -> dict[str, Any]:
        return validate_required_request(
            state, request_key="publish_request", required_fields=("story_id", "repository_name"),
            node="validate_request", started_message="Validando payload de publicação da PR.",
            failed_message="Payload de publicação da PR inválido.",
            completed_message="Payload de publicação da PR validado.",
            missing_error_prefix="Payload de publicação da PR incompleto: ",
        )

    def preview_publish(state: AgentState) -> dict[str, Any]:
        return run_preview_step(
            state, request_key="publish_request", preview_key="publish_preview", result_key="publish_result",
            action="story_pr_publish.preview", node="preview_publish",
            started_message="Gerando preview da publicação da PR.",
            failed_message="Falha ao gerar o preview da publicação.",
            completed_message="Preview da publicação gerado.",
            tracking_category="pull_request_publish_preview",
            tracking_message="Preview de publicação da PR gerado.",
            preview_operation=ports.pull_requests.preview_publish,
            tracking_payload_builder=lambda request, preview: {
                "story_id": request.get("story_id"), "pull_request_id": preview.get("pull_request_id"),
            },
        )

    def gate_publish(state: AgentState) -> dict[str, Any]:
        return run_mutation_gate(
            state, preview_key="publish_preview", result_key="publish_result", action=ACTION,
            node="gate_publish",
            approval_required_message="Fluxo aguardando aprovação explícita para publicar a PR.",
            approved_message="Aprovação explícita detectada para publicar a PR.",
        )

    def publish(state: AgentState) -> dict[str, Any]:
        return run_execute_step(
            state, request_key="publish_request", result_key="publish_result", node="publish",
            started_message="Publicando a PR após aprovação explícita.",
            failed_message="Falha ao publicar a PR.", completed_message="PR publicada.",
            tracking_category="pull_request_publish", tracking_message="PR publicada pelo grafo.",
            execute_operation=ports.pull_requests.publish_pull_request,
            request_builder=lambda request, current: {
                **request, "preview": dict(current.get("publish_preview", {})),
            },
            fastmcp_capability_name="publish_story_pull_request", approval_action=ACTION,
            tracking_payload_builder=lambda request, _result: {"story_id": request.get("story_id")},
        )

    def verify_publish(state: AgentState) -> dict[str, Any]:
        preview = dict(state.get("publish_preview", {}))
        result = dict(state.get("publish_result", {}))
        problems = verify_published_pull_request(preview, result)
        pull_request = result.get("pull_request") if isinstance(result.get("pull_request"), dict) else {}
        verification = {
            "verified": not problems, "problems": problems,
            "url": pull_request.get("web_url") or preview.get("web_url"),
            "pending_after_publish": pending_after_publish(preview),
        }
        event = record_runtime_event(
            state, node="verify_publish", status="completed" if not problems else "failed",
            message="PR verificada após a publicação." if not problems else "PR publicada com divergências.",
            payload={"problems": problems},
        )
        updates: dict[str, Any] = {**event, "publish_result": {**result, "verification": verification}}
        if problems:
            updates.update(append_error(state, "Verificação da publicação falhou: " + " ".join(problems)))
        return updates

    def stop_without_publish(state: AgentState) -> dict[str, Any]:
        return stop_without_execution(
            state, node="stop_without_publish",
            message="Fluxo encerrado sem publicar a PR (bloqueio ou aguardando aprovação).",
        )

    def route_after_gate(state: AgentState) -> str:
        return "publish" if dict(state.get("pending_approval", {})).get("approved") else "stop_without_publish"

    def route_after_publish(state: AgentState) -> str:
        status = dict(state.get("publish_result", {})).get("status")
        return "stop_without_publish" if status in {"execute_failed", "reconciliation_required"} else "verify_publish"

    graph.add_node("validate_request", validate_request)
    graph.add_node("preview_publish", preview_publish)
    graph.add_node("gate_publish", gate_publish)
    graph.add_node("publish", publish)
    graph.add_node("verify_publish", verify_publish)
    graph.add_node("stop_without_publish", stop_without_publish)

    graph.add_edge(START, "validate_request")
    for source, target in (("validate_request", "preview_publish"), ("preview_publish", "gate_publish")):
        graph.add_conditional_edges(
            source,
            lambda state, target=target: route_after_errors(state, next_node=target, stop_node="stop_without_publish"),
            {target: target, "stop_without_publish": "stop_without_publish"},
        )
    graph.add_conditional_edges("gate_publish", route_after_gate,
                                {"publish": "publish", "stop_without_publish": "stop_without_publish"})
    graph.add_conditional_edges("publish", route_after_publish,
                                {"verify_publish": "verify_publish", "stop_without_publish": "stop_without_publish"})
    graph.add_edge("verify_publish", END)
    graph.add_edge("stop_without_publish", END)
    return graph.compile()
