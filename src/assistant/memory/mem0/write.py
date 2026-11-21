"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Mem0 Platform-backed memory write adapter.
"""

from pathlib import Path

from mem0 import MemoryClient

from assistant.core.config.schemas import MemoryConfig
from assistant.memory.mem0.scope import check_configured_scope
from assistant.memory.write.intent_audit import append_audit, load_seen_intent_ids
from assistant.memory.write.models import (
    MemoryUpdateAction,
    MemoryUpdateIntent,
    WriteAudit,
    WriteStatus,
)


def _resolve_user_id(user_id: str | None, config: MemoryConfig) -> str:
    return (user_id or config.default_user_id).strip() or config.default_user_id


def _intent_to_messages(intent: MemoryUpdateIntent) -> list[dict[str, str]]:
    """Convert memory intent to Mem0 add messages format.

    Exactly one user message with only the confirmed text (#58, D14): with
    infer=False the v3 add endpoint stores every message as a separate memory.
    """
    if not intent.candidate:
        return []
    body = intent.candidate.body_markdown.strip()
    if not body:
        return []
    return [{"role": "user", "content": body}]


class Mem0MemoryWriteService:
    """Apply memory update intents via Mem0 Platform API."""

    def __init__(self, config: MemoryConfig, data_root: Path | str) -> None:
        if not config.api_key.strip():
            raise ValueError(
                "Mem0 api_key is required. Set ASSISTANT_MEMORY_API_KEY or "
                "configure api_key in config/memory.yaml"
            )
        self._config = config
        self._data_root = Path(data_root)
        self._seen_intent_ids = load_seen_intent_ids(self._data_root)
        # mem0ai 2.x resolves org/project from the API key; the constructor no longer
        # accepts org_id/project_id, so configured values are verified instead.
        self._client = MemoryClient(api_key=config.api_key)
        check_configured_scope(self._client, config)
        self._min_confidence = 0.5

    def apply_intent(self, intent: MemoryUpdateIntent, user_id: str | None = None) -> WriteAudit:
        """Apply a memory update intent via Mem0 add/delete/update."""
        if intent.intent_id in self._seen_intent_ids:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.IDEMPOTENT_NOOP,
                memory_id=None,
                reason="duplicate intent_id",
            )
        effective_user_id = _resolve_user_id(user_id, self._config)

        if intent.action == MemoryUpdateAction.UPSERT:
            audit = self._upsert(intent, effective_user_id)
        elif intent.action == MemoryUpdateAction.DELETE:
            audit = self._delete(intent, effective_user_id)
        elif intent.action == MemoryUpdateAction.TOUCH:
            audit = self._touch(intent)
        else:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.REJECTED_INVALID,
                reason=f"unknown action: {intent.action}",
            )
        if audit.status not in (
            WriteStatus.REJECTED_INVALID,
            WriteStatus.IDEMPOTENT_NOOP,
        ):
            self._seen_intent_ids.add(intent.intent_id)
            append_audit(
                self._data_root,
                intent.intent_id,
                audit.status.value,
                audit.memory_id,
            )
        return audit

    def _upsert(self, intent: MemoryUpdateIntent, user_id: str) -> WriteAudit:
        if not intent.candidate:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.REJECTED_INVALID,
                reason="upsert requires candidate",
            )
        cand = intent.candidate
        if cand.confidence < self._min_confidence:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.SKIPPED_LOW_CONFIDENCE,
                memory_id=None,
                reason=f"confidence {cand.confidence} below threshold {self._min_confidence}",
            )
        messages = _intent_to_messages(intent)
        if not messages:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.REJECTED_INVALID,
                reason="upsert candidate body is empty",
            )
        metadata: dict[str, str | list[str]] = {
            "memory_type": intent.memory_type.value,
            "intent_id": intent.intent_id,
        }
        if intent.reason.strip():
            metadata["reason"] = intent.reason.strip()[:300]
        if cand.tags:
            metadata["tags"] = cand.tags
        if cand.entities:
            metadata["entities"] = cand.entities
        try:
            result = self._client.add(
                messages,
                user_id=user_id,
                metadata=metadata,
                infer=False,
            )
        except Exception as exc:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.REJECTED_INVALID,
                reason=str(exc),
            )
        memory_id = intent.memory_id
        events = result if isinstance(result, list) else result.get("results", [])
        if events and isinstance(events[0], dict):
            memory_id = events[0].get("id", memory_id or "")
        if not memory_id:
            memory_id = f"{intent.memory_type.value}-{intent.intent_id[:12]}"
        status = WriteStatus.UPDATED if intent.memory_id else WriteStatus.WRITTEN
        return WriteAudit(
            intent_id=intent.intent_id,
            status=status,
            memory_id=memory_id,
            reason="",
        )

    def _delete(self, intent: MemoryUpdateIntent, user_id: str) -> WriteAudit:
        if not intent.memory_id:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.REJECTED_INVALID,
                reason="delete requires memory_id",
            )
        try:
            self._client.delete(intent.memory_id)
        except Exception as exc:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.REJECTED_INVALID,
                memory_id=intent.memory_id,
                reason=str(exc),
            )
        return WriteAudit(
            intent_id=intent.intent_id,
            status=WriteStatus.DELETED,
            memory_id=intent.memory_id,
            reason="",
        )

    def _touch(self, intent: MemoryUpdateIntent) -> WriteAudit:
        if not intent.memory_id:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.REJECTED_INVALID,
                reason="touch requires memory_id",
            )
        return WriteAudit(
            intent_id=intent.intent_id,
            status=WriteStatus.TOUCHED,
            memory_id=intent.memory_id,
            reason="",
        )
