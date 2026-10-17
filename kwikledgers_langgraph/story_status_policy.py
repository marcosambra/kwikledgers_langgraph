from __future__ import annotations

from typing import Any


CANONICAL_STORY_STATUS_FLOW = (
    "New",
    "Refinamento",
    "Backlog Sprint",
    "Desenvolvimento",
    "Aguardando PR",
    "Homologação",
    "QA",
)


def build_story_status_transition_preview(
    request: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    current_status = str(snapshot.get("current_status") or "").strip()
    target_status = str(request.get("target_status") or "").strip()
    expected_current_status = str(request.get("expected_current_status") or "").strip() or None
    current_index = _find_status_index(current_status)
    target_index = _find_status_index(target_status)
    validation_messages: list[str] = []
    exception_reason = request.get("transition_exception_reason")
    explicit_exception = isinstance(exception_reason, str) and bool(exception_reason.strip())
    if exception_reason is not None and (not explicit_exception or not expected_current_status):
        validation_messages.append("Exceção exige justificativa explícita e status atual esperado.")

    if snapshot.get("work_item_type") and snapshot.get("work_item_type") != "User Story":
        validation_messages.append("O item informado nao e uma User Story.")
    if expected_current_status and expected_current_status != current_status:
        validation_messages.append("O status atual da story nao corresponde ao status esperado informado no request.")
    if current_index is None:
        validation_messages.append("O status atual da story nao faz parte do fluxo canonico esperado.")
    if target_index is None:
        validation_messages.append("O status de destino nao faz parte do fluxo canonico esperado.")
    if (current_index is not None and target_index is not None and target_index != current_index + 1
            and not explicit_exception):
        validation_messages.append("A transicao solicitada nao e a proxima etapa permitida no fluxo canonico.")

    return {
        **snapshot,
        "target_status": target_status,
        "expected_current_status": expected_current_status,
        "transition_exception_reason": exception_reason,
        "transition_allowed": not validation_messages,
        "validation_messages": validation_messages,
        "canonical_flow": list(CANONICAL_STORY_STATUS_FLOW),
        "patch_preview": {"System.State": target_status},
    }


def _find_status_index(status: str) -> int | None:
    if status in CANONICAL_STORY_STATUS_FLOW:
        return CANONICAL_STORY_STATUS_FLOW.index(status)
    return None