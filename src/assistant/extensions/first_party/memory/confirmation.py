"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Memory-update confirmation kind: draft building (user-facing summary, dedupe key)
and the handler that applies an approved intent through the memory writer. Registered
with the generic ConfirmationService (core.confirmation) at startup.
"""

import asyncio
import hashlib
import json

from assistant.core.confirmation.models import (
    ConfirmationDraft,
    ConfirmationOutcome,
    ConfirmationRequest,
)
from assistant.memory.interfaces import MemoryWriterInterface
from assistant.memory.write.models import MemoryUpdateAction, MemoryUpdateIntent, WriteStatus

MEMORY_UPDATE_KIND = "memory_update"
# Only these writer statuses mean the memory store actually changed.
_APPLIED_STATUSES = frozenset(
    {WriteStatus.WRITTEN, WriteStatus.UPDATED, WriteStatus.DELETED, WriteStatus.TOUCHED}
)
# The user must see exactly what is saved: longer bodies are rejected at propose
# time (memory_body_length_error) instead of being shown truncated.
MEMORY_BODY_MAX_CHARS = 2500
_REASON_MAX_CHARS = 300
_MEMORY_ID_MAX_CHARS = 200


def memory_body_length_error(intent: MemoryUpdateIntent) -> str | None:
    """Reason to reject a proposal whose body cannot be shown in full, else None."""
    body = intent.candidate.body_markdown.strip() if intent.candidate else ""
    if intent.action != MemoryUpdateAction.UPSERT or len(body) <= MEMORY_BODY_MAX_CHARS:
        return None
    return (
        f"memory body is {len(body)} chars; at most {MEMORY_BODY_MAX_CHARS} can be shown to the "
        "user for confirmation. Shorten it or split it into several smaller memories."
    )


def build_memory_update_draft(
    intent: MemoryUpdateIntent, origin_tool_call_id: str = "", origin_tool_name: str = ""
) -> ConfirmationDraft:
    """Build a confirmation draft showing the actual memory change to the user."""
    return ConfirmationDraft(
        kind=MEMORY_UPDATE_KIND,
        payload=intent.model_dump(mode="json"),
        summary_for_user=memory_update_summary(intent),
        dedupe_key=memory_update_dedupe_key(intent),
        origin_tool_call_id=origin_tool_call_id,
        origin_tool_name=origin_tool_name,
    )


def memory_update_summary(intent: MemoryUpdateIntent) -> str:
    """Render what will change: action, memory type, and body or target memory_id."""
    lines = [f"Action: {intent.action.value}", f"Type: {intent.memory_type.value}"]
    if intent.memory_id:
        lines.append(f"Memory ID: {_clip(intent.memory_id, _MEMORY_ID_MAX_CHARS)}")
    body = intent.candidate.body_markdown.strip() if intent.candidate else ""
    if intent.action == MemoryUpdateAction.UPSERT and body:
        if len(body) > MEMORY_BODY_MAX_CHARS:
            # Normally rejected at propose time; never hide that text is missing.
            body = (
                body[:MEMORY_BODY_MAX_CHARS]
                + f"\n… [truncated: {MEMORY_BODY_MAX_CHARS} of {len(body)} chars shown,"
                " the full text will be saved]"
            )
        lines.append("")
        lines.append(body)
    if intent.reason.strip():
        lines.append("")
        lines.append(f"Reason: {_clip(intent.reason.strip(), _REASON_MAX_CHARS)}")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def memory_update_dedupe_key(intent: MemoryUpdateIntent) -> str:
    """Stable key for 'the same proposal' regardless of model-supplied intent_id."""
    body = intent.candidate.body_markdown.strip() if intent.candidate else ""
    material = json.dumps(
        [
            intent.action.value,
            intent.memory_type.value,
            intent.memory_id or "",
            " ".join(body.split()),
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


class MemoryUpdateHandler:
    """Applies approved memory updates via MemoryWriterInterface."""

    kind = MEMORY_UPDATE_KIND

    def __init__(self, writer: MemoryWriterInterface) -> None:
        self._writer = writer

    async def apply(
        self, request: ConfirmationRequest, *, user_id: str | None
    ) -> ConfirmationOutcome:
        """Apply the intent; only WRITTEN/UPDATED/DELETED/TOUCHED count as applied."""
        intent = MemoryUpdateIntent.model_validate(request.payload)
        # The confirmation id is unique per request: a stable writer idempotency key.
        intent = intent.model_copy(update={"intent_id": f"confirmation:{request.confirmation_id}"})
        effective_user_id = user_id or request.session_id
        # Writers are synchronous (Mem0 does network I/O); keep the event loop free.
        audit = await asyncio.to_thread(self._writer.apply_intent, intent, effective_user_id)
        ok = audit.status in _APPLIED_STATUSES
        detail = audit.reason or audit.status.value
        if ok and audit.memory_id:
            detail = f"{audit.status.value} (memory_id={audit.memory_id})"
        return ConfirmationOutcome(ok=ok, status=audit.status.value, detail=detail)
