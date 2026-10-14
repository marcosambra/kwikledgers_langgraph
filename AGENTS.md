# AGENTS

## Purpose

This repository evolves the future KwikLedgers orchestration runtime as a
standalone Python project based on LangGraph.

The legacy runtime outside this repository is reference and integration
surface only. Do not edit the legacy runtime when the work can be isolated in
this repository.

## Start Here

Read these files in this order before making non-trivial changes:

1. `README.md`
2. `.github/copilot-instructions.md`
3. `.github/skills/langgraph-preview-first-flow/SKILL.md` when the task
   involves a mutable flow

## Runtime Contract

- Treat guardrails, observability and approval gates as core runtime behavior.
- Graph nodes must return partial state updates; do not assume the full state
  is present.
- Use helpers from `src/kwikledgers_langgraph/state.py` when appending
  decisions, tracking events, errors or observability timeline entries.
- Use Python 3.12 or newer for code, environments and tests.

## Legacy Boundary

- Keep integration ports in `src/kwikledgers_langgraph/adapters.py`.
- Keep legacy bridges in `src/kwikledgers_langgraph/legacy_adapters.py`.
- Do not call the legacy runtime directly from graph nodes.
- If legacy compatibility is needed, prefer `KWIKLEDGERS_MCP_SERVERS_PATH` or
  `KWIKLEDGERS_LEGACY_ROOT`; do not hardcode monorepo paths.

## Mutable Flow Rule

For any Azure mutation or equivalent mutable action, the canonical flow is:

1. validate
2. preview
3. gate
4. execute or stop

Additional rules:

- Preview is read-only.
- Mutation requires `pending_approval`.
- Real approval must be human, must happen after preview and must be bound to
  the current action.
- Do not synthesize approval in the same run for real execution.
- Treat `--approve-create` as local simulation or explicit test support only.
- Persist only the minimum necessary data in preview, tracking and timeline.

## Canonical Change Path

When adding a new flow, touch files in this sequence when applicable:

1. `src/kwikledgers_langgraph/state.py`
2. `src/kwikledgers_langgraph/adapters.py`
3. `src/kwikledgers_langgraph/legacy_adapters.py`
4. `src/kwikledgers_langgraph/graphs/`
5. `src/kwikledgers_langgraph/runtime.py`
6. CLI module
7. `tests/`

## Validation

Run the smallest relevant validation immediately after an edit.

Primary commands:

- `poetry install`
- `poetry lock`
- `poetry run python -m unittest discover -s tests`
- `poetry run kwikledgers-langgraph --help`
- `poetry run kwikledgers-langgraph-story-pr --help`

## Documentation Rule

- Update `README.md` when the public workflow of this repository changes.
- While this repository still lives inside the workspace monorepo, append each
  repository change to `../docs/history/kwikledgers-alteration-YYYY-MM-DD.md`.

## Decision Guide

- If the task is a read-only operational flow, mirror the daily summary
  pattern.
- If the task is a mutable flow, use the preview-first skill and enforce the
  approval boundary.
- If the task seems to require editing legacy code directly, stop and verify
  whether the seam can be moved into an adapter instead.