from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import StoryPrLaunchPorts
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
from kwikledgers_langgraph.story_pr_preflight import after_pull_request_checklist

DEFAULT_TARGET_BRANCH = "stage-pre-prod"


def _required_request_fields() -> tuple[str, ...]:
    return (
        "story_id",
        "repository_name",
        "repository_path",
        "source_branch",
        "what_was_changed",
        "affected_processes",
        "expected_impacts",
        "important_points",
        "mermaid_diagram",
    )


def verify_created_pull_request(
    request: dict[str, Any], preview: dict[str, Any], result: dict[str, Any],
) -> list[str]:
    """Compare the created draft PR against what was approved; return every divergence."""
    problems: list[str] = []
    pull_request = result.get("pull_request") if isinstance(result.get("pull_request"), dict) else result
    if result.get("created") is not True:
        problems.append("A criação não foi confirmada (created != true).")
    if pull_request.get("is_draft") is not True:
        problems.append("A PR não está como Draft.")
    target = str(request.get("target_branch") or DEFAULT_TARGET_BRANCH)
    if pull_request.get("target_ref_name") != f"refs/heads/{target}":
        problems.append(f"A PR não aponta para {target}.")
    if pull_request.get("source_ref_name") != f"refs/heads/{request.get('source_branch')}":
        problems.append("A branch de origem da PR difere da solicitada.")
    if not pull_request.get("web_url"):
        problems.append("O link da PR não foi retornado.")
    expected = {str(identifier) for identifier in preview.get("expected_revisions", {})}
    linked = {str(item.get("id")) for item in pull_request.get("work_item_refs") or [] if isinstance(item, dict)}
    missing = sorted(expected - linked)
    if missing:
        problems.append("Work items sem vínculo com a PR: " + ", ".join(missing) + ".")
    return problems


def _preview_without_active_pull_request(preview: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    existing = preview.get("existing_active_pull_request")
    if existing is not None or preview.get("can_open_pull_request") is False:
        raise ValueError(f"Já existe PR ativa para esta branch: {existing}" if existing is not None
                         else "O preview nativo indica que a PR não pode ser aberta.")
    return {**preview, "preflight": {"head_sha": report.get("head_sha"), "warnings": report.get("warnings", [])}}


def build_story_pr_launch_graph(ports: StoryPrLaunchPorts) -> Any:
    graph = StateGraph(AgentState)

    def validate_request(state: AgentState) -> dict[str, Any]:
        return validate_required_request(
            state,
            request_key="pull_request_request",
            required_fields=_required_request_fields(),
            node="validate_request",
            started_message="Validando payload de lançamento da PR.",
            failed_message="Payload de lançamento da PR inválido.",
            completed_message="Payload de lançamento da PR validado.",
            missing_error_prefix="Payload de lançamento da PR incompleto: ",
        )

    def preflight(state: AgentState) -> dict[str, Any]:
        started = record_runtime_event(
            state, node="preflight", status="started", message="Executando checklist antes da PR.",
        )
        request = dict(state.get("pull_request_request", {}))
        try:
            report = ports.preflight.run_preflight(request)
        except Exception as error:
            failed = record_runtime_event(
                state, node="preflight", status="failed", message="Falha ao executar o checklist.",
                payload={"error": str(error)},
            )
            return {**started, **append_error(state, f"Falha ao executar o checklist: {error}"), **failed,
                    "pull_request_preflight": {"passed": False, "blocking": ["preflight_error"], "warnings": [],
                                               "checks": []},
                    "pull_request_result": {"status": "preflight_failed", "changed": False}}
        if not report.get("passed"):
            details = "; ".join(f"{item['id']}: {item['detail']}"
                                for item in report.get("checks", []) if item["severity"] == "block" and not item["ok"])
            failed = record_runtime_event(
                state, node="preflight", status="blocked", message="Checklist bloqueou a abertura da PR.",
                payload={"blocking": report.get("blocking", [])},
            )
            return {**started, **append_error(state, f"Checklist bloqueou a PR: {details}"), **failed,
                    "pull_request_preflight": report,
                    "pull_request_result": {"status": "preflight_blocked", "changed": False,
                                            "blocking": report.get("blocking", [])}}
        completed = record_runtime_event(
            state, node="preflight", status="completed", message="Checklist aprovado.",
            payload={"warnings": report.get("warnings", [])},
        )
        return {**started, **completed, "pull_request_preflight": report}

    def preview_pull_request(state: AgentState) -> dict[str, Any]:
        report = dict(state.get("pull_request_preflight", {}))
        return run_preview_step(
            state,
            request_key="pull_request_request",
            preview_key="pull_request_preview",
            result_key="pull_request_result",
            action="story_pr_launch.preview",
            node="preview_pull_request",
            started_message="Gerando preview da PR.",
            failed_message="Falha ao gerar o preview da PR.",
            completed_message="Preview da PR gerado com sucesso.",
            tracking_category="pull_request_preview",
            tracking_message="Preview da PR gerado pelo grafo de lançamento.",
            preview_operation=lambda request: _preview_without_active_pull_request(
                ports.pull_requests.preview_story_pull_request(request), report),
            tracking_payload_builder=lambda request, preview: {
                "title": preview.get("title"), "story_id": request.get("story_id"),
            },
            completed_payload_builder=lambda _request, preview: {"title": preview.get("title")},
        )

    def gate_create_pull_request(state: AgentState) -> dict[str, Any]:
        return run_mutation_gate(
            state,
            preview_key="pull_request_preview",
            result_key="pull_request_result",
            action="story_pr_launch.create",
            node="gate_create_pull_request",
            approval_required_message="Fluxo aguardando aprovação explícita para criar a PR.",
            approved_message="Aprovação explícita detectada para criar a PR.",
        )

    def create_pull_request(state: AgentState) -> dict[str, Any]:
        return run_execute_step(
            state,
            request_key="pull_request_request",
            result_key="pull_request_result",
            node="create_pull_request",
            started_message="Criando a PR após aprovação explícita.",
            failed_message="Falha ao criar a PR.",
            completed_message="PR criada com sucesso.",
            tracking_category="pull_request",
            tracking_message="PR criada pelo grafo de lançamento.",
            execute_operation=ports.pull_requests.create_story_pull_request,
            request_builder=lambda request, current: {
                **request, "preview": dict(current.get("pull_request_preview", {})),
            },
            fastmcp_capability_name="create_story_pull_request",
            approval_action="story_pr_launch.create",
            tracking_payload_builder=lambda request, result: {
                "story_id": request.get("story_id"), "title": result.get("title"),
            },
            completed_payload_builder=lambda _request, result: {"title": result.get("title")},
        )

    def verify_pull_request(state: AgentState) -> dict[str, Any]:
        request = dict(state.get("pull_request_request", {}))
        preview = dict(state.get("pull_request_preview", {}))
        result = dict(state.get("pull_request_result", {}))
        problems = verify_created_pull_request(request, preview, result)
        pull_request = result.get("pull_request") if isinstance(result.get("pull_request"), dict) else {}
        verification = {
            "verified": not problems,
            "problems": problems,
            "url": pull_request.get("web_url"),
            "pending_after_pr": after_pull_request_checklist(request, dict(state.get("pull_request_preflight", {}))),
        }
        event = record_runtime_event(
            state, node="verify_pull_request", status="completed" if not problems else "failed",
            message="PR verificada após a criação." if not problems else "PR criada com divergências.",
            payload={"problems": problems},
        )
        updates: dict[str, Any] = {**event, "pull_request_result": {**result, "verification": verification}}
        if problems:
            updates.update(append_error(state, "Verificação da PR falhou: " + " ".join(problems)))
        return updates

    def stop_without_create(state: AgentState) -> dict[str, Any]:
        return stop_without_execution(
            state, node="stop_without_create",
            message="Fluxo encerrado sem criar a PR (bloqueio do checklist ou aguardando aprovação).",
        )

    def route_after_gate(state: AgentState) -> str:
        pending = dict(state.get("pending_approval", {}))
        return "create_pull_request" if pending.get("approved") else "stop_without_create"

    def route_after_create(state: AgentState) -> str:
        status = dict(state.get("pull_request_result", {})).get("status")
        failed = status in {"execute_failed", "reconciliation_required"}
        return "stop_without_create" if failed else "verify_pull_request"

    graph.add_node("validate_request", validate_request)
    graph.add_node("preflight", preflight)
    graph.add_node("preview_pull_request", preview_pull_request)
    graph.add_node("gate_create_pull_request", gate_create_pull_request)
    graph.add_node("create_pull_request", create_pull_request)
    graph.add_node("verify_pull_request", verify_pull_request)
    graph.add_node("stop_without_create", stop_without_create)

    graph.add_edge(START, "validate_request")
    for source, target in (("validate_request", "preflight"), ("preflight", "preview_pull_request"),
                           ("preview_pull_request", "gate_create_pull_request")):
        graph.add_conditional_edges(
            source,
            lambda state, target=target: route_after_errors(state, next_node=target, stop_node="stop_without_create"),
            {target: target, "stop_without_create": "stop_without_create"},
        )
    graph.add_conditional_edges(
        "gate_create_pull_request", route_after_gate,
        {"create_pull_request": "create_pull_request", "stop_without_create": "stop_without_create"},
    )
    graph.add_conditional_edges(
        "create_pull_request", route_after_create,
        {"verify_pull_request": "verify_pull_request", "stop_without_create": "stop_without_create"},
    )
    graph.add_edge("verify_pull_request", END)
    graph.add_edge("stop_without_create", END)

    return graph.compile()
