from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kwikledgers_langgraph.backend_factory import build_work_item_creation_config_ports
from kwikledgers_langgraph.observability import (
    append_runtime_observability_artifacts, build_observability_context, record_runtime_event,
)
from kwikledgers_langgraph.runtime import build_work_item_creation_config_runtime
from kwikledgers_langgraph.state import AgentState
from kwikledgers_langgraph.state_persistence import write_state_file
from kwikledgers_langgraph.work_item_creation import validate_operator


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Read native Azure constraints and prepare operator-config; never create work items.",
        epilog="The host must set AZURE_PROJECT and AZURE_PROJECT_AREA in the workspace environment. "
               "Use the generated JSON with work_item_creation_cli --operator-config; approval is still required.",
    )
    result.add_argument("--repository", required=True, action="append", metavar="NAME=PATH",
                        help="Explicit authorized repository name and local path; repeat for each repository.")
    result.add_argument("--output", required=True, type=Path,
                        help="Local operator-config JSON; existing files are not replaced unless --replace is used.")
    result.add_argument("--replace", action="store_true",
                        help="Overwrite an existing --output file, only after the metadata was read successfully.")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    state: AgentState = {
        "source": "work-item-creation-config-cli",
        "observability": build_observability_context(
            flow_name="work_item_creation_config", source="work-item-creation-config-cli", backend_name="fastmcp"),
    }
    try:
        if args.output.exists() and not args.replace:
            raise FileExistsError("Operator config already exists; choose a new output file or use --replace")
        repositories: dict[str, str] = {}
        for entry in args.repository:
            name, separator, path = entry.partition("=")
            if not separator or not name.strip() or not path.strip() or name in repositories:
                raise ValueError("--repository requires unique NAME=PATH entries")
            repositories[name] = path
        state["work_item_creation_config_request"] = {"repositories": repositories}
        runtime = build_work_item_creation_config_runtime(build_work_item_creation_config_ports(backend_name="fastmcp"))
        state = runtime.invoke(state)
        result = state.get("work_item_creation_config_result", {})
        if state.get("errors") or result.get("status") != "prepared":
            print(json.dumps({"outcome": "blocked", "errors": state.get("errors", [])}, ensure_ascii=True),
                  file=sys.stderr)
            return 1
        operator = validate_operator(result.get("operator"))
        write_state_file(operator, str(args.output))
        state.update(record_runtime_event(state, node="persist_configuration", status="completed",
                                         message="Configuracao operacional persistida; nenhuma escrita Azure."))
        print(json.dumps({"outcome": "prepared", "operator_config": str(args.output.resolve()),
                          "project": operator["project"], "area": operator["area"],
                          "repositories": sorted(operator["repositories"]),
                          "azure_write": False}, ensure_ascii=True))
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        state.update(record_runtime_event(state, node="persist_configuration", status="failed",
                                         message="Preparacao da configuracao operacional bloqueada.",
                                         payload={"error_type": type(error).__name__}))
        print(json.dumps({"outcome": "blocked", "errors": [str(error)]}, ensure_ascii=True), file=sys.stderr)
        return 1
    finally:
        try:
            append_runtime_observability_artifacts(state)
        except (ValueError, RuntimeError, OSError) as error:
            print(f"Falha ao registrar observabilidade: {type(error).__name__}.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
