from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

GitRunner = Callable[[Path, list[str]], tuple[int, str]]

BASE_BRANCH = "stage-pre-prod"
BRANCH_PATTERN = r"^(feature|hotfix)/KL-{story_id}-[a-z0-9][a-z0-9-]*$"
CHERRY_PICK_BRANCH_PATTERN = r"^cherry-pick/KL-{story_id}-[a-z0-9][a-z0-9._-]*$"
AZURE_HOSTS = ("dev.azure.com", "ssh.dev.azure.com", "vs-ssh.visualstudio.com")
PLACEHOLDERS = ("todo", "a definir", "tbd", "lorem", "preencher", "n/a")
MERMAID_KEYWORDS = ("flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram", "erDiagram", "gantt")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
FORBIDDEN_PATH_PATTERNS = (
    r"(^|/)\.env($|\.(?!example$|sample$)[^/]*$)",
    r"(^|/)(vendor|node_modules)/",
    r"\.(pem|key|p12|pfx)$",
    r"(^|/)id_(rsa|ed25519)(\.pub)?$",
)
FORBIDDEN_ADDED_LINES = ("TEMP-LOCAL", "<<<<<<<", ">>>>>>>")
TEXT_FIELDS = ("what_was_changed", "affected_processes", "expected_impacts", "important_points")


def default_git_runner(repository: Path, arguments: list[str]) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return completed.returncode, completed.stdout if completed.returncode == 0 else completed.stderr


def _check(check_id: str, severity: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"id": check_id, "severity": severity, "ok": ok, "detail": detail}


def _sanitize_remote(url: str) -> str:
    if "://" not in url:
        return url.split("@", 1)[-1]
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}{parts.path}"


def _remote_host(url: str) -> str:
    if "://" in url:
        return urlsplit(url).hostname or ""
    return url.split("@", 1)[-1].split(":", 1)[0]


def _request_checks(request: dict[str, Any], repository: Path, repository_name: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    tests_declared = bool(request.get("tests_updated")) or bool(request.get("tests_unchanged"))
    checks.append(_check("tests_declared", "block", tests_declared,
                         "Testes declarados." if tests_declared else
                         "Informe tests_updated ou tests_unchanged; não há PR sem declaração de testes."))
    checks.append(_check("tests_evidence", "warn", bool(str(request.get("tests_evidence") or "").strip()),
                         "Evidência de testes informada." if str(request.get("tests_evidence") or "").strip() else
                         "Sem tests_evidence (comando e resultado) para citar na PR."))

    templates = [repository / "pull_request_template.md", repository / ".github" / "pull_request_template.md"]
    checks.append(_check("pr_template", "block", any(path.is_file() for path in templates),
                         "Template de PR do repositório encontrado." if any(path.is_file() for path in templates) else
                         "pull_request_template.md ausente no repositório."))
    weak = [field for field in TEXT_FIELDS
            if len(str(request.get(field) or "").strip()) < 10
            or str(request.get(field) or "").strip().lower() in PLACEHOLDERS]
    checks.append(_check("pr_text_filled", "block", not weak,
                         "Seções da PR preenchidas." if not weak else
                         f"Seções vazias ou placeholder: {', '.join(weak)}."))
    diagram = str(request.get("mermaid_diagram") or "").strip()
    diagram_ok = diagram.startswith(MERMAID_KEYWORDS)
    checks.append(_check("mermaid_diagram", "block", diagram_ok,
                         "Diagrama Mermaid presente." if diagram_ok else
                         "mermaid_diagram vazio ou sem tipo de diagrama válido na primeira linha."))

    if "frontend" in repository_name.lower():
        evidence = [Path(str(item)).expanduser() for item in request.get("evidence_files") or []]
        images = [path for path in evidence if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES]
        checks.append(_check("frontend_screenshot", "block", bool(images),
                             f"Print de evidência: {images[0].name}." if images else
                             "Repositório frontend exige evidence_files com ao menos um print existente."))
    return checks


def run_preflight(
    request: dict[str, Any], *, runner: GitRunner = default_git_runner,
    workspace_root: Path | None = None, today: date | None = None,
) -> dict[str, Any]:
    """Run deterministic, read-only checks that must pass before opening a story draft PR."""
    story_id = int(request["story_id"])
    branch = str(request["source_branch"])
    repository_name = str(request["repository_name"])
    repository = Path(str(request.get("repository_path") or "")).expanduser()
    checks: list[dict[str, Any]] = []
    head_sha: str | None = None

    code, toplevel = runner(repository, ["rev-parse", "--show-toplevel"])
    is_root = code == 0 and Path(toplevel.strip()).resolve() == repository.resolve()
    checks.append(_check("repository_root", "block", is_root,
                         "Raiz do repositório confirmada." if is_root else
                         "repository_path não é a raiz de um repositório Git."))
    if not is_root:
        return _summarize(checks, head_sha)

    code, origin = runner(repository, ["remote", "get-url", "origin"])
    origin = origin.strip()
    azure = code == 0 and _remote_host(origin) in AZURE_HOSTS
    checks.append(_check("origin_azure", "block", azure,
                         f"origin: {_sanitize_remote(origin)}" if code == 0 else "origin não encontrado."))

    name_ok = (re.match(BRANCH_PATTERN.format(story_id=story_id), branch) is not None
               or re.match(CHERRY_PICK_BRANCH_PATTERN.format(story_id=story_id), branch) is not None)
    checks.append(_check("branch_name", "block", name_ok,
                         "Branch segue <feature|hotfix|cherry-pick>/KL-<id>-<nome>." if name_ok else
                         f"Branch '{branch}' fora do padrão <feature|hotfix|cherry-pick>/KL-{story_id}-<nome>."))

    code, local_sha = runner(repository, ["rev-parse", f"refs/heads/{branch}"])
    local_sha = local_sha.strip() if code == 0 else ""
    code, remote_out = runner(repository, ["ls-remote", "--heads", "origin", branch])
    remote_sha = remote_out.split()[0] if code == 0 and remote_out.strip() else ""
    head_sha = local_sha or None
    published = bool(local_sha) and local_sha == remote_sha
    if not local_sha:
        detail = "Branch local inexistente."
    elif not remote_sha:
        detail = "Branch não publicada no origin."
    elif local_sha != remote_sha:
        detail = "HEAD local difere do origin: há commits não publicados ou divergência."
    else:
        detail = f"Publicada e idêntica ao origin ({local_sha[:8]})."
    checks.append(_check("branch_published", "block", published, detail))
    if not local_sha:
        checks.extend(_request_checks(request, repository, repository_name))
        return _summarize(checks, head_sha)

    base_branch = str(request.get("target_branch") or BASE_BRANCH)
    base = f"origin/{base_branch}"
    code, ahead_out = runner(repository, ["rev-list", "--count", f"{base}..{branch}"])
    ahead = int(ahead_out.strip()) if code == 0 and ahead_out.strip().isdigit() else None
    checks.append(_check("ahead_of_base", "block", bool(ahead),
                         f"{ahead} commit(s) à frente de {base_branch}." if ahead else
                         f"Sem commits novos em relação a {base_branch} (ou base indisponível)."))
    code, behind_out = runner(repository, ["rev-list", "--count", f"{branch}..{base}"])
    behind = int(behind_out.strip()) if code == 0 and behind_out.strip().isdigit() else None
    checks.append(_check("up_to_date_with_base", "warn", behind == 0,
                         "Branch contém o HEAD atual da base." if behind == 0 else
                         f"Branch está {behind} commit(s) atrás de {base_branch}; considere integrar a base."))

    code, log = runner(repository, ["log", f"{base}..{branch}", "--format=%h%x1f%B%x1e"])
    entries = [entry.strip() for entry in log.split("\x1e") if entry.strip()] if code == 0 else []
    token = f"KL-{story_id}"
    missing_ref = [entry.split("\x1f", 1)[0] for entry in entries if token not in entry]
    checks.append(_check("commits_reference_story", "block", not missing_ref and bool(entries),
                         f"Todos os commits citam {token}." if not missing_ref and entries else
                         f"Commits sem {token}: {', '.join(missing_ref) or 'nenhum commit lido'}."))
    coauthored = [entry.split("\x1f", 1)[0] for entry in entries if re.search(r"co-authored-by", entry, re.I)]
    checks.append(_check("no_coauthored_trailer", "block", not coauthored,
                         "Nenhum trailer Co-authored-by." if not coauthored else
                         f"Commits com Co-authored-by: {', '.join(coauthored)}."))

    code, names = runner(repository, ["diff", "--name-only", f"{base}...{branch}"])
    files = [line for line in names.splitlines() if line.strip()] if code == 0 else []
    forbidden = [path for path in files if any(re.search(pattern, path) for pattern in FORBIDDEN_PATH_PATTERNS)]
    checks.append(_check("no_forbidden_files", "block", not forbidden,
                         "Nenhum arquivo sensível ou gerado no diff." if not forbidden else
                         f"Arquivos proibidos no diff: {', '.join(forbidden)}."))
    code, patch = runner(repository, ["diff", "-U0", f"{base}...{branch}"])
    flagged = sorted({marker for line in patch.splitlines()
                      if line.startswith("+") and not line.startswith("+++")
                      for marker in FORBIDDEN_ADDED_LINES if marker in line}) if code == 0 else []
    checks.append(_check("no_temporary_code", "block", not flagged,
                         "Nenhum marcador temporário ou conflito nas linhas adicionadas." if not flagged else
                         f"Marcadores nas linhas adicionadas: {', '.join(flagged)}."))
    attention = [path for path in files if path.endswith(".gitignore") or "migration" in path.lower()]
    checks.append(_check("unexpected_scope_files", "warn", not attention,
                         "Sem .gitignore ou migrations no diff." if not attention else
                         f"Confirme se pertencem à entrega: {', '.join(attention)}."))

    code, status = runner(repository, ["status", "--porcelain", "--untracked-files=no"])
    dirty = [line for line in status.splitlines() if line.strip()] if code == 0 else []
    checks.append(_check("tracked_worktree_clean", "warn", not dirty,
                         "Sem alterações rastreadas pendentes." if not dirty else
                         f"{len(dirty)} alteração(ões) rastreada(s) fora da PR; não serão incluídas."))

    checks.extend(_request_checks(request, repository, repository_name))

    root = workspace_root or Path(__file__).resolve().parents[2]
    history = root / "docs" / "history" / f"kwikledgers-alteration-{(today or date.today()).isoformat()}.md"
    logged = history.is_file() and token in history.read_text(encoding="utf-8", errors="replace")
    checks.append(_check("history_entry", "warn", logged,
                         f"Histórico do dia cita {token}." if logged else
                         f"Sem registro de {token} em {history.name}."))
    return _summarize(checks, head_sha)


def _summarize(checks: list[dict[str, Any]], head_sha: str | None) -> dict[str, Any]:
    blocking = [item["id"] for item in checks if item["severity"] == "block" and not item["ok"]]
    warnings = [item["id"] for item in checks if item["severity"] == "warn" and not item["ok"]]
    return {"passed": not blocking, "blocking": blocking, "warnings": warnings,
            "head_sha": head_sha, "checks": checks}


def after_pull_request_checklist(request: dict[str, Any], preflight: dict[str, Any]) -> list[str]:
    """Steps that remain after the draft PR exists, so none depends on memory."""
    items = ["Mover a história de Desenvolvimento para Aguardando PR pelo grafo story_status_transition (preview e aprovação).",
             "Registrar a PR criada no histórico do workspace (docs/history)."]
    if "frontend" in str(request.get("repository_name", "")).lower():
        items.insert(0, "Anexar o print de evidência na PR (anexo não é feito pelo grafo).")
    items.extend(f"Resolver aviso: {identifier}." for identifier in preflight.get("warnings", []))
    return items


class GitPreflightAdapter:
    def __init__(self, runner: GitRunner = default_git_runner, workspace_root: Path | None = None) -> None:
        self.runner, self.workspace_root = runner, workspace_root

    def run_preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        return run_preflight(request, runner=self.runner, workspace_root=self.workspace_root)
