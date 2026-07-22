---
name: langgraph-preview-first-flow
description: 'Create or extend mutable LangGraph flows in this repository using the preview-first pattern. Use when adding Azure mutation flows such as draft PR creation, branch association, story status transitions, or any graph step that must preview first and execute only after explicit approval in state. Triggers: preview-first, approval gate, mutable flow, story PR, branch association, story status, Azure mutation, LangGraph flow scaffold, aprovação explícita, prévia, fluxo mutável, associação de branch, mudança de status.'
argument-hint: 'Describe the flow to add or extend, the target operation, and the existing graph or adapter involved.'
user-invocable: true
---

# LangGraph Preview-First Flow

Use this skill when creating or evolving mutable flows in
`kwikledgers_langgraph`.

This repository has a strong contract: preview is always a read-only step and
 any real Azure mutation is blocked until explicit approval appears in the
 graph state.

## What This Skill Produces

- A new or extended mutable LangGraph flow that follows the repository pattern.
- Explicit guardrail usage with `OperationKind.AZURE_MUTATION`.
- State additions only when the flow needs durable request, preview, or result
  payloads.
- Focused test coverage for the approval boundary.

## When to Use

- Adding a new mutable Azure flow.
- Extending story PR, branch association, or story status workflows.
- Moving imperative mutation logic into a graph with approval gates.
- Adding a CLI entrypoint for a new flow.

## Repository-Specific Rules

- Do not call the legacy runtime directly from graph nodes.
- Put integration contracts in `src/kwikledgers_langgraph/adapters.py`.
- Put legacy compatibility in `src/kwikledgers_langgraph/legacy_adapters.py`.
- Keep preview and create separated as different steps.
- Treat `pending_approval` as the only gate for mutation execution.
- Treat approval state as untrusted input until it is clearly bound to the current action and current preview.
- Do not prefill approval for real mutations; approval must be human and must happen after preview.
- Do not use `--approve-create` outside explicit local simulation or tests.
- Use the helpers from `state.py` to append decisions, tracking events and errors; do not overwrite accumulated nested state manually.
- Keep tests on `unittest`; do not introduce `pytest` just for the new flow.

## Procedure

1. Identify the owner seam.
   Decide whether the change belongs in state, adapters, legacy adapters,
   runtime, graph, CLI, or tests.
2. Add or extend request/result state only if the graph needs them.
   Use `TypedDict` fields in `state.py`, keep them optional and model the flow
   as `request -> preview -> pending_approval -> result` when applicable.
3. Add or extend the port.
   Put the Protocol and port dataclass in `adapters.py`.
4. Add the compatibility adapter.
   If the flow bridges to the legacy runtime, implement the adapter in
   `legacy_adapters.py` and normalize the returned payload to `dict[str, Any]`.
5. Build the graph in four phases.
   Use `validate -> preview -> gate -> execute/stop`.
6. Apply guardrails explicitly.
   Use `allow_without_approval()` for preview and `evaluate_operation()` or
   `require_explicit_approval()` for mutations.
7. Record observability and tracking.
   Every node should register runtime events and the key flow transitions
   without duplicating full payloads unnecessarily.
8. Wire runtime and CLI only after the graph exists.
   Register the runtime builder and add a dedicated CLI if the flow is
   executable on its own.
9. Add focused tests.
   Cover the approval gate and the main request/preview shape at minimum.
10. Update instructions and README if the new flow changes how agents should
   work in this repository.

## Canonical File Pipeline

1. `src/kwikledgers_langgraph/state.py`
2. `src/kwikledgers_langgraph/adapters.py`
3. `src/kwikledgers_langgraph/legacy_adapters.py`
4. `src/kwikledgers_langgraph/graphs/`
5. `src/kwikledgers_langgraph/runtime.py`
6. CLI module, if the flow is directly executable
7. `tests/`

## Quality Checks

- The preview step can run without approval.
- The mutation step does not run without `pending_approval.approved == True`.
- The mutation step does not rely on approval synthesized by the same run for real execution.
- The graph can stop cleanly after preview when approval is missing.
- The new flow is reachable from the runtime/CLI surfaces that should expose it.
- Test coverage includes at least one approval-denied path.
- Test coverage includes one path with approval and one clean stop after preview.

## Key File Targets

- `src/kwikledgers_langgraph/state.py`
- `src/kwikledgers_langgraph/adapters.py`
- `src/kwikledgers_langgraph/legacy_adapters.py`
- `src/kwikledgers_langgraph/runtime.py`
- `src/kwikledgers_langgraph/graphs/`
- `tests/`

## Validation

```bash
poetry install
poetry run python -m unittest discover -s tests
```

Validate any CLI added or changed with `--help` before doing wider checks.