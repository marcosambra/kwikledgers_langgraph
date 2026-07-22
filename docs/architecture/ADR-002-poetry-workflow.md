# ADR-002: Poetry as the Operational Workflow

## Status

Accepted

## Context

The repository already used `poetry-core` as its build backend, but its day to day
workflow still mixed direct `PYTHONPATH=src` execution, `python -m` commands and a
CI pipeline based on `pip install .`.

That mismatch created two avoidable problems:

- local and CI environments were installed differently
- the documented developer workflow did not match the chosen packaging toolchain

## Decision Drivers

- Keep installation and execution consistent across local development and CI.
- Use the repository as a standalone Python project instead of an ad hoc source tree.
- Reduce command drift in instructions for contributors and agents.

## Decision

The repository will use Poetry as the default operational workflow:

- dependencies are installed with `poetry install`
- validation commands run through `poetry run`
- CI installs Poetry and uses the same commands documented for contributors
- CLI validation prefers the package entrypoints exposed by the project scripts

## Options Considered

1. Keep direct `PYTHONPATH=src` commands.

Rejected because it bypasses the packaging workflow already declared by the project.

2. Keep `pip install .` in CI and document Poetry only for local use.

Rejected because it preserves unnecessary divergence between local and automated runs.

3. Standardize both local and CI flows on Poetry.

Accepted because it is the smallest change that makes the workflow coherent.

## Consequences

Positive:

- One installation path for contributors and CI.
- Documented commands now match the packaging surface of the repository.
- Future dependency changes have a single primary execution model.

Negative:

- CI now depends on installing Poetry before validation.
- Contributors must have Poetry available to follow the default workflow.

## Follow-Up

- Add lockfile management if the repository decides to commit a `poetry.lock`.
- Consider caching Poetry virtualenvs in CI if runtime becomes a bottleneck.