# RAI-ADR-001: Minimal Identity Resolution for Daily Summary

## Status

Accepted

## Context

The `daily_summary` flow previously had a placeholder identity-loading node with no
real integration seam. The next runtime slice introduces a concrete identity
resolver so the flow can carry user context when explicitly configured.

This feature handles personal data because user email and display name may enter
runtime state.

## Decision

The runtime will resolve only the minimum identity fields needed by the current
flow:

- `resolved_email`
- `display_name`
- `email_source`

The default adapter reads those values only from explicit environment variables.
If no values are configured, the flow continues without identity.

## Responsible AI Controls

- No demographic inference or automated decision-making is introduced.
- Identity resolution is opt-in through explicit environment configuration.
- Tracking and observability do not persist raw email or display name payloads.
- The graph records only boolean flags about whether identity fields were present.

## Privacy Consequences

Positive:

- The flow avoids collecting unnecessary personal attributes.
- It does not log raw identity fields into tracking payloads.

Residual risk:

- Resolved identity still exists in runtime state for downstream flow use.
- Environment variables remain an operational secret-handling surface and must be
  managed by the caller environment.

## Follow-Up

- Reuse the same minimal-data rule when adding stronger identity adapters.
- If future flows persist identity externally, define retention and redaction rules first.