from __future__ import annotations

import argparse
import json
from typing import Any

from kwikledgers_langgraph.backend_factory import build_sprint_stories_ports, resolve_backend_name
from kwikledgers_langgraph.observability import (
    append_runtime_observability_artifacts,
    build_observability_context,
)
from kwikledgers_langgraph.runtime import build_sprint_stories_runtime
from kwikledgers_langgraph.state import AgentState


def _build_initial_state(source: str, backend_name: str) -> AgentState:
    return {
        "source": source,
        "tracking_events": [],
        "sprint_stories_request": {},
        "observability": build_observability_context(
            flow_name="sprint_stories",
            source=source,
            backend_name=backend_name,
        ),
    }


def run_sprint_stories(*, source: str, backend_name: str | None = None) -> dict[str, Any]:
    resolved_backend = resolve_backend_name(backend_name)
    runtime = build_sprint_stories_runtime(build_sprint_stories_ports(backend_name=resolved_backend))
    state = _build_initial_state(source, resolved_backend)
    result = runtime.invoke(state)
    if not isinstance(result, dict):
        raise RuntimeError("O runtime de historias detalhadas do sprint retornou um estado final invalido.")
    return result


def _record_runtime_observability(result: dict[str, Any]) -> None:
    append_runtime_observability_artifacts(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Executa o runtime LangGraph que busca as historias detalhadas do sprint atual (descricao, criterios de aceite e children).",
    )
    parser.add_argument("--source", default="sprint-stories-cli", help="Origem registrada no estado e no tracking.")
    parser.add_argument(
        "--backend",
        default=None,
        help="Backend de integracao do runtime. O padrao atual e 'legacy'.",
    )
    parser.add_argument(
        "--print-state",
        action="store_true",
        help="Imprime o estado final completo em JSON.",
    )
    parser.add_argument(
        "--disable-observability-sync",
        action="store_true",
        help="Nao grava a observabilidade resumida do runtime em AI_Tracking/Metrics/ai-usage-log.md.",
    )
    args = parser.parse_args(argv)

    result = run_sprint_stories(source=args.source, backend_name=args.backend)

    sprint_result = result.get("sprint_stories_result", {})
    counts = sprint_result.get("counts", {})
    if not args.disable_observability_sync:
        try:
            _record_runtime_observability(result)
        except RuntimeError as error:
            print(f"Aviso: falha ao sincronizar observabilidade externa: {error}")
    print(
        "Runtime LangGraph de historias detalhadas do sprint executado com sucesso "
        f"para {counts.get('total_stories', counts.get('stories', 0))} historias."
    )
    if args.print_state:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
