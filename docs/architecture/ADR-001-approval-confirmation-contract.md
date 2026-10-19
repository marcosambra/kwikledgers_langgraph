# ADR-001: Approval Confirmation Contract for Mutable Flows

## Status

Accepted

## Context

The story pull request flow is the first mutable preview-first flow in this runtime.
The previous guardrail contract accepted a fully bound `pending_approval` from input
state as sufficient proof for a real mutation, as long as it matched the action,
preview payload and run ID.

That design kept the mutation gate too close to caller-controlled input because the
same structure represented both:

- the approval request emitted by the runtime
- the approval proof consumed by the runtime

The repository contract requires real approval to happen after preview and to be
bound to the current action.

## Decision Drivers

- Preserve the canonical `pending_approval` gate in repository state.
- Reduce trust in pre-seeded approval payloads supplied on first invocation.
- Keep local CLI simulation available for tests and explicit manual validation.
- Avoid external dependencies or legacy coupling for the first hardening step.

## Decision

Mutable flows will separate approval request emission from approval confirmation:

- `pending_approval` remains the runtime-issued request object.
- `pending_approval` now carries a generated `request_id`.
- external approval is represented separately as `approval_confirmation`.
- guardrail approval requires a matching `request_id`, matching action, matching
  preview, matching run ID and a replayed preview context.
- same-run approval remains allowed only through explicit local simulation.

For the story PR flow, replayed preview context is expressed by rerunning the flow
with the prior preview payload still present in state, so the preview step can prove
that the current preview is the same one that was previously shown.
The public CLI surface must therefore persist the full post-preview state and reuse
that same state during resume instead of rebuilding a fresh observability context.

## Options Considered

1. Keep relying on `pending_approval.approved` alone.

Rejected because it conflates request and confirmation and trusts caller-supplied
state too much.

2. Add an external signed approval token.

Rejected for now because it adds secret management and operational dependencies not
yet present in this standalone runtime slice.

3. Split request from confirmation inside runtime state.

Accepted because it hardens the contract with minimal surface change and preserves
the repository's preview-first model.

## Consequences

Positive:

- The first invocation no longer depends on caller-supplied approved state.
- Approval now models the intended two-step flow more explicitly.
- Tests can distinguish pre-seeded request state from real post-preview approval.

Negative:

- Real approval now requires replaying prior state rather than only toggling a
  boolean field.
- The contract is harder to bypass, but not cryptographically secure against a
  fully forged caller state.

## Follow-Up

- Reuse the same request/confirmation contract in future branch association and
  story status flows.
- Consider signed or externally persisted approval artifacts if the runtime later
  needs stronger trust guarantees across process boundaries.