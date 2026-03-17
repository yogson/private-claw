"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Controlled memory write, confidence thresholds, and deduplication.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

from assistant.memory.indexing import MemoryIndexer
from assistant.memory.store.models import MemoryArtifact, MemoryFrontmatter, MemoryType
from assistant.memory.store.parser import parse_memory_file, serialize_memory_artifact
from assistant.memory.store.paths import MemoryPaths
from assistant.memory.write.atomic_io import atomic_write_text
from assistant.memory.write.dedup import find_dedup_target, merge_artifact
from assistant.memory.write.intent_audit import append_audit, load_seen_intent_ids
from assistant.memory.write.models import (
    MemoryUpdateAction,
    MemoryUpdateIntent,
    WriteAudit,
    WriteStatus,
)


def _generate_memory_id(memory_type: MemoryType) -> str:
    prefix = memory_type.value
    suffix = uuid.uuid4().hex[:12]
    return f"{prefix}-{suffix}"


class MemoryWriteService:
    """Apply memory update intents with confidence and dedup policies."""

    def __init__(
        self,
        data_root: Path | str,
        min_confidence: float = 0.5,
        dedup_enabled: bool = True,
    ) -> None:
        self._paths = MemoryPaths(Path(data_root))
        self._indexer = MemoryIndexer(self._paths)
        self._min_confidence = min_confidence
        self._dedup_enabled = dedup_enabled
        self._seen_intent_ids = load_seen_intent_ids(self._paths.data_root)

    def apply_intent(self, intent: MemoryUpdateIntent, user_id: str | None = None) -> WriteAudit:
        """Apply a memory update intent. Returns audit with status and affected memory_id."""
        if intent.intent_id in self._seen_intent_ids:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.IDEMPOTENT_NOOP,
                memory_id=None,
                reason="duplicate intent_id",
            )
        audit = self._apply_intent_impl(intent)
        if audit.status not in (
            WriteStatus.REJECTED_INVALID,
            WriteStatus.IDEMPOTENT_NOOP,
        ):
            self._seen_intent_ids.add(intent.intent_id)
            append_audit(
                self._paths.data_root,
                intent.intent_id,
                audit.status.value,
                audit.memory_id,
            )
        return audit

    def _apply_intent_impl(self, intent: MemoryUpdateIntent) -> WriteAudit:
        if intent.action == MemoryUpdateAction.UPSERT:
            return self._upsert(intent)
        if intent.action == MemoryUpdateAction.DELETE:
            return self._delete(intent)
        if intent.action == MemoryUpdateAction.TOUCH:
            return self._touch(intent)
        return WriteAudit(
            intent_id=intent.intent_id,
            status=WriteStatus.REJECTED_INVALID,
            reason=f"unknown action: {intent.action}",
        )

    def _upsert(self, intent: MemoryUpdateIntent) -> WriteAudit:
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
        memory_id = intent.memory_id
        is_update = False
        is_dedup_merge = False
        if memory_id:
            path = self._paths.artifact_path(intent.memory_type, memory_id)
            is_update = path.exists()
        elif self._dedup_enabled:
            dedup_id = find_dedup_target(
                self._paths,
                intent.memory_type,
                cand.tags,
                cand.entities,
            )
            if dedup_id:
                memory_id = dedup_id
                is_update = True
                is_dedup_merge = True
        if not memory_id:
            memory_id = _generate_memory_id(intent.memory_type)
        now = datetime.now(UTC)
        path = self._paths.artifact_path(intent.memory_type, memory_id)
        old_tags: list[str] = []
        old_entities: list[str] = []
        if is_dedup_merge and path.exists():
            try:
                existing = parse_memory_file(path)
                old_tags = existing.frontmatter.tags
                old_entities = existing.frontmatter.entities
                artifact = merge_artifact(
                    existing,
                    cand.tags,
                    cand.entities,
                    cand.priority,
                    cand.confidence,
                    cand.body_markdown,
                    now,
                )
            except (ValueError, OSError):
                artifact = MemoryArtifact(
                    frontmatter=MemoryFrontmatter(
                        memory_id=memory_id,
                        type=intent.memory_type,
                        tags=cand.tags,
                        entities=cand.entities,
                        priority=cand.priority,
                        confidence=cand.confidence,
                        updated_at=now,
                        last_used_at=None,
                        created_at=None,
                    ),
                    body=cand.body_markdown,
                )
        elif is_update and path.exists():
            try:
                existing = parse_memory_file(path)
                old_tags = existing.frontmatter.tags
                old_entities = existing.frontmatter.entities
                artifact = MemoryArtifact(
                    frontmatter=MemoryFrontmatter(
                        memory_id=memory_id,
                        type=intent.memory_type,
                        tags=cand.tags,
                        entities=cand.entities,
                        priority=cand.priority,
                        confidence=cand.confidence,
                        updated_at=now,
                        last_used_at=existing.frontmatter.last_used_at,
                        created_at=existing.frontmatter.created_at,
                    ),
                    body=cand.body_markdown,
                )
            except (ValueError, OSError):
                artifact = MemoryArtifact(
                    frontmatter=MemoryFrontmatter(
                        memory_id=memory_id,
                        type=intent.memory_type,
                        tags=cand.tags,
                        entities=cand.entities,
                        priority=cand.priority,
                        confidence=cand.confidence,
                        updated_at=now,
                        last_used_at=None,
                        created_at=now,
                    ),
                    body=cand.body_markdown,
                )
        else:
            artifact = MemoryArtifact(
                frontmatter=MemoryFrontmatter(
                    memory_id=memory_id,
                    type=intent.memory_type,
                    tags=cand.tags,
                    entities=cand.entities,
                    priority=cand.priority,
                    confidence=cand.confidence,
                    updated_at=now,
                    last_used_at=None,
                    created_at=now if not is_update else None,
                ),
                body=cand.body_markdown,
            )
        atomic_write_text(path, serialize_memory_artifact(artifact))
        if self._indexer.indexes_exist():
            if is_update:
                self._indexer.remove_artifact(
                    intent.memory_type,
                    memory_id,
                    old_tags,
                    old_entities,
                )
            self._indexer.add_artifact(artifact)
        else:
            self._indexer.build()
        status = WriteStatus.UPDATED if is_update else WriteStatus.WRITTEN
        return WriteAudit(
            intent_id=intent.intent_id,
            status=status,
            memory_id=memory_id,
            reason="",
        )

    def _delete(self, intent: MemoryUpdateIntent) -> WriteAudit:
        if not intent.memory_id:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.REJECTED_INVALID,
                reason="delete requires memory_id",
            )
        path = self._paths.artifact_path(intent.memory_type, intent.memory_id)
        if not path.exists():
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.DELETED,
                memory_id=intent.memory_id,
                reason="already absent",
            )
        tags: list[str] = []
        entities: list[str] = []
        try:
            existing = parse_memory_file(path)
            tags = existing.frontmatter.tags
            entities = existing.frontmatter.entities
        except (ValueError, OSError):
            pass
        path.unlink()
        if self._indexer.indexes_exist():
            self._indexer.remove_artifact(
                intent.memory_type,
                intent.memory_id,
                tags,
                entities,
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
        path = self._paths.artifact_path(intent.memory_type, intent.memory_id)
        if not path.exists():
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.REJECTED_INVALID,
                memory_id=intent.memory_id,
                reason="artifact not found",
            )
        try:
            artifact = parse_memory_file(path)
        except (ValueError, OSError) as e:
            return WriteAudit(
                intent_id=intent.intent_id,
                status=WriteStatus.REJECTED_INVALID,
                memory_id=intent.memory_id,
                reason=str(e),
            )
        now = datetime.now(UTC)
        fm = artifact.frontmatter
        new_fm = MemoryFrontmatter(
            memory_id=fm.memory_id,
            type=fm.type,
            tags=fm.tags,
            entities=fm.entities,
            priority=fm.priority,
            confidence=fm.confidence,
            updated_at=fm.updated_at,
            last_used_at=now,
            created_at=fm.created_at,
        )
        updated = MemoryArtifact(frontmatter=new_fm, body=artifact.body)
        atomic_write_text(path, serialize_memory_artifact(updated))
        if self._indexer.indexes_exist():
            self._indexer.remove_artifact(
                intent.memory_type,
                intent.memory_id,
                fm.tags,
                fm.entities,
            )
            self._indexer.add_artifact(updated)
        else:
            self._indexer.build()
        return WriteAudit(
            intent_id=intent.intent_id,
            status=WriteStatus.TOUCHED,
            memory_id=intent.memory_id,
            reason="",
        )
