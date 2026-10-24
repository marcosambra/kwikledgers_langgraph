from __future__ import annotations

from typing import Any
from urllib.parse import quote

from langgraph.graph import END, START, StateGraph

from kwikledgers_langgraph.adapters import StoryPrCherryPickPorts
from kwikledgers_langgraph.fastmcp_adapters import select_story_pull_request
from kwikledgers_langgraph.graphs.mutable_flow import (
    route_after_errors,
    run_execute_step,
    run_mutation_gate,
    run_preview_step,
    stop_without_execution,
    validate_required_request,
)
from kwikledgers_langgraph.guardrails import require_execution_approval
from kwikledgers_langgraph.observability import record_runtime_event
from kwikledgers_langgraph.state import AgentState, append_error
from kwikledgers_langgraph.story_pr_worktree import cherry_pick_branch_name

ACTION = "story_pr_cherry_pick.execute"
DEFAULT_SOURCE_TARGET = "stage-pre-prod"
DEFAULT_TARGET = "stage-homolog"
WAIT_SECONDS = 90


def _short(ref: str | None) -> str:
    return str(ref or "").removeprefix("refs/heads/")


def build_cherry_pick_preview(ports: StoryPrCherryPickPorts, request: dict[str, Any]) -> dict[str, Any]:
    """Pick one PR, derive the branch/worktree names and decide how the cherry-pick will run."""
    source_target = request.get("source_target_branch") or DEFAULT_SOURCE_TARGET
    target = request.get("target_branch") or DEFAULT_TARGET
    candidates = ports.pull_requests.list_candidate_pull_requests({**request, "source_target_branch": source_target})
    selected = select_story_pull_request(
        candidates, request.get("pull_request_id"), description=f"ativa para {source_target} neste repositório",
    )
    if not selected.get("source_commit_id"):
        raise ValueError("O Azure não informou o commit de origem da PR.")
    derived = {
        "story_id": int(request["story_id"]),
        "repository_name": selected["repository_name"],
        "repository_path": request.get("repository_path"),
        "pull_request_id": selected["pull_request_id"],
        "source_branch": _short(selected["source_ref_name"]),
        "pr_target_branch": _short(selected["target_ref_name"]),
        "target_branch": target,
    }
    if target == derived["pr_target_branch"] or target == derived["source_branch"]:
        raise ValueError("A branch alvo do cherry-pick deve diferir da origem e do alvo da PR.")
    derived["generated_branch"] = cherry_pick_branch_name(derived["source_branch"], target)
    inspection = ports.worktree.inspect(derived)
    if not inspection["target_exists"]:
        raise ValueError(f"A branch alvo {target} não existe no origin.")
    if inspection["remote_branch_sha"]:
        action = "already_done"
    elif inspection["worktree_exists"]:
        action = "finalize_local"
    else:
        action = "azure_api"
    steps = {
        "azure_api": [
            "Criar o cherry-pick pela API do Azure (sem merge da PR) em uma branch nova.",
            "Se houver conflito ou falha: fallback local em worktree dedicado "
            "(fetch, worktree add, git cherry-pick commit a commit, push simples sem force).",
            "Em conflito local o fluxo para e preserva o worktree; nada é abortado, resetado ou forçado.",
        ],
        "finalize_local": [
            "Worktree existente: verificar que não há conflito pendente, aplicar commits faltantes e push simples.",
        ],
        "already_done": ["A branch de cherry-pick já existe no origin; nada a executar."],
    }[action]
    return {
        **derived, "action": action, "wait_seconds": WAIT_SECONDS, "expected_source_commit": selected["source_commit_id"],
        "title": selected.get("title"), "web_url": selected.get("web_url"),
        "worktree_path": inspection["worktree_path"], "remote_branch_sha": inspection["remote_branch_sha"],
        "steps": steps,
    }


def build_cherry_pick_links(preview: dict[str, Any]) -> dict[str, str]:
    """Derive navigation links from the source PR URL (`<repo>/pullrequest/<id>`)."""
    pr_url = str(preview.get("web_url") or "")
    repo_url = pr_url.split("/pullrequest/")[0] if "/pullrequest/" in pr_url else ""
    if not repo_url:
        return {"pull_request_origem": pr_url} if pr_url else {}
    branch = quote(str(preview.get("generated_branch") or ""), safe="")
    target = quote(str(preview.get("target_branch") or ""), safe="")
    return {
        "pull_request_origem": pr_url,
        "branch_cherry_pick": f"{repo_url}?version=GB{branch}",
        "criar_pr_para_alvo": f"{repo_url}/pullrequestcreate?sourceRef={branch}&targetRef={target}",
    }


def pending_after_cherry_pick(preview: dict[str, Any], used_worktree: bool) -> list[str]:
    steps = [
        f"Abrir a PR de {preview['generated_branch']} para {preview['target_branch']} com story_pr_launch "
        f"(--target-branch {preview['target_branch']}), com preview e aprovação própria.",
    ]
    if used_worktree:
        steps.append(f"Após a PR ser criada e verificada, remover o worktree: git worktree remove "
                     f"{preview['worktree_path']}.")
    return steps


def build_story_pr_cherry_pick_graph(ports: StoryPrCherryPickPorts) -> Any:
    graph = StateGraph(AgentState)

    def validate_request(state: AgentState) -> dict[str, Any]:
        return validate_required_request(
            state, request_key="cherry_pick_request", required_fields=("story_id", "repository_name"),
            node="validate_request", started_message="Validando payload do cherry-pick.",
            failed_message="Payload do cherry-pick inválido.", completed_message="Payload do cherry-pick validado.",
            missing_error_prefix="Payload do cherry-pick incompleto: ",
        )

    def preview_cherry_pick(state: AgentState) -> dict[str, Any]:
        return run_preview_step(
            state, request_key="cherry_pick_request", preview_key="cherry_pick_preview",
            result_key="cherry_pick_result", action="story_pr_cherry_pick.preview", node="preview_cherry_pick",
            started_message="Planejando o cherry-pick da PR.", failed_message="Falha ao planejar o cherry-pick.",
            completed_message="Plano do cherry-pick gerado.", tracking_category="cherry_pick_preview",
            tracking_message="Preview do cherry-pick gerado.",
            preview_operation=lambda request: build_cherry_pick_preview(ports, request),
            tracking_payload_builder=lambda request, preview: {
                "story_id": request.get("story_id"), "pull_request_id": preview.get("pull_request_id"),
                "action": preview.get("action"),
            },
        )

    def gate_cherry_pick(state: AgentState) -> dict[str, Any]:
        return run_mutation_gate(
            state, preview_key="cherry_pick_preview", result_key="cherry_pick_result", action=ACTION,
            node="gate_cherry_pick",
            approval_required_message="Fluxo aguardando aprovação explícita para o cherry-pick.",
            approved_message="Aprovação explícita detectada para o cherry-pick.",
        )

    def cherry_pick_azure(state: AgentState) -> dict[str, Any]:
        return run_execute_step(
            state, request_key="cherry_pick_request", result_key="cherry_pick_result", node="cherry_pick_azure",
            started_message="Criando o cherry-pick pela API do Azure.",
            failed_message="Falha ao criar o cherry-pick no Azure.", completed_message="Cherry-pick do Azure retornou.",
            tracking_category="cherry_pick", tracking_message="Cherry-pick solicitado ao Azure.",
            execute_operation=ports.pull_requests.cherry_pick,
            request_builder=lambda request, current: {
                **request, "preview": dict(current.get("cherry_pick_preview", {})),
            },
            fastmcp_capability_name="cherry_pick_story_pull_request", approval_action=ACTION,
            tracking_payload_builder=lambda request, result: {
                "story_id": request.get("story_id"), "status": result.get("status"),
            },
        )

    def local_step(state: AgentState, node: str, operation: Any, message: str) -> dict[str, Any]:
        started = record_runtime_event(state, node=node, status="started", message=message)
        preview = dict(state.get("cherry_pick_preview", {}))
        try:
            require_execution_approval(state, action=ACTION)
            outcome = operation(preview)
        except Exception as error:
            failed = record_runtime_event(state, node=node, status="failed", message=message,
                                          payload={"error": str(error)})
            return {**started, **failed, **append_error(state, f"{message}: {error}"),
                    "cherry_pick_local": {"status": "failed", "detail": str(error)}}
        completed = record_runtime_event(state, node=node, status="completed", message=message,
                                         payload={"status": outcome.get("status", "ok")})
        return {**started, **completed, "cherry_pick_local": {**dict(state.get("cherry_pick_local", {})), **outcome}}

    def local_apply(state: AgentState) -> dict[str, Any]:
        return local_step(state, "local_apply", ports.worktree.apply,
                          "Fallback local: aplicando os commits da PR no worktree.")

    def local_finalize(state: AgentState) -> dict[str, Any]:
        return local_step(state, "local_finalize", ports.worktree.finalize,
                          "Retomando o worktree preservado.")

    def local_push(state: AgentState) -> dict[str, Any]:
        return local_step(state, "local_push", ports.worktree.push, "Publicando a branch do worktree (sem force).")

    def verify_cherry_pick(state: AgentState) -> dict[str, Any]:
        preview = dict(state.get("cherry_pick_preview", {}))
        azure = dict(state.get("cherry_pick_result", {}))
        local = dict(state.get("cherry_pick_local", {}))
        used_worktree = bool(local)
        problems: list[str] = []
        try:
            remote_sha = ports.worktree.remote_branch_sha(preview)
        except Exception as error:
            remote_sha = None
            problems.append(f"Não foi possível consultar o origin: {error}")
        if not remote_sha:
            problems.append(f"A branch {preview.get('generated_branch')} não existe no origin.")
        elif used_worktree and local.get("pushed_sha") and local["pushed_sha"] != remote_sha:
            problems.append("O commit publicado difere do commit do worktree.")
        if not used_worktree and azure.get("completed") is not True:
            problems.append("O Azure não confirmou o cherry-pick como concluído.")
        verification = {
            "verified": not problems, "problems": problems, "branch": preview.get("generated_branch"),
            "remote_sha": remote_sha, "via": "worktree" if used_worktree else "azure_api",
            "pending_after_cherry_pick": pending_after_cherry_pick(preview, used_worktree),
            "links": build_cherry_pick_links(preview),
        }
        event = record_runtime_event(
            state, node="verify_cherry_pick", status="completed" if not problems else "failed",
            message="Cherry-pick verificado." if not problems else "Cherry-pick com divergências.",
            payload={"problems": problems},
        )
        updates: dict[str, Any] = {**event, "cherry_pick_result": {
            **azure, "status": "cherry_picked" if not problems else "verification_failed",
            "verification": verification,
        }}
        if problems:
            updates.update(append_error(state, "Verificação do cherry-pick falhou: " + " ".join(problems)))
        return updates

    def report_stop(state: AgentState) -> dict[str, Any]:
        preview = dict(state.get("cherry_pick_preview", {}))
        local = dict(state.get("cherry_pick_local", {}))
        azure = dict(state.get("cherry_pick_result", {}))
        if preview.get("action") == "already_done":
            status = "already_done"
            report: dict[str, Any] = {"pending_after_cherry_pick": pending_after_cherry_pick(preview, False),
                                      "links": build_cherry_pick_links(preview)}
        elif local.get("status") == "conflict":
            status = "conflict_local"
            report = {"worktree_path": local.get("worktree_path"), "stuck_commit": local.get("stuck_commit"),
                      "conflicting_files": local.get("conflicting_files", []), "resume": local.get("resume")}
        elif azure.get("status") in {"inProgress", "queued", "notSet"} or azure.get("timedout"):
            status = "in_progress"
            report = {"cherry_pick_id": azure.get("cherry_pick_id"),
                      "resume": "Consulte get_story_cherry_pick antes de repetir; não rode novo cherry-pick."}
        else:
            return stop_without_execution(
                state, node="report_stop",
                message="Fluxo encerrado sem executar o cherry-pick (bloqueio, aprovação pendente ou falha).",
            )
        event = record_runtime_event(state, node="report_stop", status="completed",
                                     message=f"Cherry-pick encerrado com status '{status}'.")
        return {**event, "cherry_pick_result": {**azure, "status": status, "report": report}}

    def route_after_preview(state: AgentState) -> str:
        if state.get("errors"):
            return "report_stop"
        return "report_stop" if dict(state.get("cherry_pick_preview", {})).get("action") == "already_done" \
            else "gate_cherry_pick"

    def route_after_gate(state: AgentState) -> str:
        if not dict(state.get("pending_approval", {})).get("approved"):
            return "report_stop"
        action = dict(state.get("cherry_pick_preview", {})).get("action")
        return "local_finalize" if action == "finalize_local" else "cherry_pick_azure"

    def route_after_azure(state: AgentState) -> str:
        result = dict(state.get("cherry_pick_result", {}))
        if result.get("status") in {"execute_failed", "reconciliation_required"}:
            return "report_stop"
        if result.get("completed") is True:
            return "verify_cherry_pick"
        if result.get("conflict") or str(result.get("status", "")).lower() in {"failed", "abandoned"}:
            return "local_apply"
        return "report_stop"

    def route_after_local(state: AgentState, next_node: str = "local_push") -> str:
        if state.get("errors") or dict(state.get("cherry_pick_local", {})).get("status") != "applied":
            return "report_stop"
        return next_node

    for name, function in (
        ("validate_request", validate_request), ("preview_cherry_pick", preview_cherry_pick),
        ("gate_cherry_pick", gate_cherry_pick), ("cherry_pick_azure", cherry_pick_azure),
        ("local_apply", local_apply), ("local_finalize", local_finalize), ("local_push", local_push),
        ("verify_cherry_pick", verify_cherry_pick), ("report_stop", report_stop),
    ):
        graph.add_node(name, function)

    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges(
        "validate_request",
        lambda state: route_after_errors(state, next_node="preview_cherry_pick", stop_node="report_stop"),
        {"preview_cherry_pick": "preview_cherry_pick", "report_stop": "report_stop"},
    )
    graph.add_conditional_edges("preview_cherry_pick", route_after_preview,
                                {"gate_cherry_pick": "gate_cherry_pick", "report_stop": "report_stop"})
    graph.add_conditional_edges("gate_cherry_pick", route_after_gate, {
        "cherry_pick_azure": "cherry_pick_azure", "local_finalize": "local_finalize", "report_stop": "report_stop"})
    graph.add_conditional_edges("cherry_pick_azure", route_after_azure, {
        "verify_cherry_pick": "verify_cherry_pick", "local_apply": "local_apply", "report_stop": "report_stop"})
    for node in ("local_apply", "local_finalize"):
        graph.add_conditional_edges(node, route_after_local, {"local_push": "local_push", "report_stop": "report_stop"})
    graph.add_conditional_edges(
        "local_push", lambda state: "report_stop" if state.get("errors") else "verify_cherry_pick",
        {"verify_cherry_pick": "verify_cherry_pick", "report_stop": "report_stop"},
    )
    graph.add_edge("verify_cherry_pick", END)
    graph.add_edge("report_stop", END)
    return graph.compile()
