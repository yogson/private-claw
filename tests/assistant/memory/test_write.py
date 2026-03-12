"""Tests for memory write service."""

from datetime import UTC, datetime
from pathlib import Path

from assistant.memory.retrieval import RetrievalQuery, RetrievalService
from assistant.memory.store.models import MemoryArtifact, MemoryFrontmatter, MemoryType
from assistant.memory.store.parser import parse_memory_file, serialize_memory_artifact
from assistant.memory.store.paths import MemoryPaths
from assistant.memory.write import (
    MemoryUpdateAction,
    MemoryUpdateIntent,
    MemoryUpdateIntentCandidate,
    MemoryWriteService,
    WriteStatus,
)


def _write_artifact(root: Path, artifact: MemoryArtifact) -> None:
    paths = MemoryPaths(root)
    cat_dir = paths.category_dir(artifact.frontmatter.type)
    cat_dir.mkdir(parents=True, exist_ok=True)
    path = paths.artifact_path(artifact.frontmatter.type, artifact.frontmatter.memory_id)
    path.write_text(serialize_memory_artifact(artifact), encoding="utf-8")


def test_upsert_creates_new_artifact(tmp_path: Path) -> None:
    svc = MemoryWriteService(tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="i1",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        candidate=MemoryUpdateIntentCandidate(
            tags=["work"],
            entities=["project-x"],
            confidence=0.9,
            body_markdown="Important fact.",
        ),
    )
    audit = svc.apply_intent(intent)
    assert audit.status == WriteStatus.WRITTEN
    assert audit.memory_id is not None
    assert audit.memory_id.startswith("facts-")
    path = tmp_path / "memory" / "facts" / f"{audit.memory_id}.md"
    assert path.exists()
    artifact = parse_memory_file(path)
    assert artifact.body == "Important fact."
    assert "work" in artifact.frontmatter.tags
    assert "project-x" in artifact.frontmatter.entities


def test_upsert_low_confidence_skipped(tmp_path: Path) -> None:
    svc = MemoryWriteService(tmp_path, min_confidence=0.8)
    intent = MemoryUpdateIntent(
        intent_id="i2",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        candidate=MemoryUpdateIntentCandidate(
            tags=[],
            confidence=0.3,
            body_markdown="Low confidence fact.",
        ),
    )
    audit = svc.apply_intent(intent)
    assert audit.status == WriteStatus.SKIPPED_LOW_CONFIDENCE
    assert audit.memory_id is None
    assert (tmp_path / "memory" / "facts").exists() is False or not list(
        (tmp_path / "memory" / "facts").glob("*.md")
    )


def test_upsert_with_memory_id_updates(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="fact-001",
                type=MemoryType.FACTS,
                tags=["old"],
                entities=[],
                updated_at=now,
            ),
            body="Original body.",
        ),
    )
    svc = MemoryWriteService(tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="i3",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        memory_id="fact-001",
        candidate=MemoryUpdateIntentCandidate(
            tags=["new"],
            entities=["entity-a"],
            confidence=0.95,
            body_markdown="Updated body.",
        ),
    )
    audit = svc.apply_intent(intent)
    assert audit.status == WriteStatus.UPDATED
    assert audit.memory_id == "fact-001"
    artifact = parse_memory_file(tmp_path / "memory" / "facts" / "fact-001.md")
    assert artifact.body == "Updated body."
    assert artifact.frontmatter.tags == ["new"]
    assert "entity-a" in artifact.frontmatter.entities


def test_delete_removes_artifact(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="pref-1",
                type=MemoryType.PREFERENCES,
                tags=["theme"],
                updated_at=now,
            ),
            body="Dark mode.",
        ),
    )
    svc = MemoryWriteService(tmp_path)
    svc._indexer.build()
    intent = MemoryUpdateIntent(
        intent_id="i4",
        action=MemoryUpdateAction.DELETE,
        memory_type=MemoryType.PREFERENCES,
        memory_id="pref-1",
    )
    audit = svc.apply_intent(intent)
    assert audit.status == WriteStatus.DELETED
    assert not (tmp_path / "memory" / "preferences" / "pref-1.md").exists()


def test_delete_without_memory_id_rejected(tmp_path: Path) -> None:
    svc = MemoryWriteService(tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="i5",
        action=MemoryUpdateAction.DELETE,
        memory_type=MemoryType.FACTS,
    )
    audit = svc.apply_intent(intent)
    assert audit.status == WriteStatus.REJECTED_INVALID
    assert "memory_id" in audit.reason.lower() or "requires" in audit.reason.lower()


def test_touch_updates_last_used_at(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="prof-1",
                type=MemoryType.PROFILE,
                tags=[],
                updated_at=now,
                last_used_at=None,
            ),
            body="Profile.",
        ),
    )
    svc = MemoryWriteService(tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="i6",
        action=MemoryUpdateAction.TOUCH,
        memory_type=MemoryType.PROFILE,
        memory_id="prof-1",
    )
    audit = svc.apply_intent(intent)
    assert audit.status == WriteStatus.TOUCHED
    artifact = parse_memory_file(tmp_path / "memory" / "profile" / "prof-1.md")
    assert artifact.frontmatter.last_used_at is not None


def test_touch_rebuilds_indexes_when_missing(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="touch-1",
                type=MemoryType.PROFILE,
                tags=[],
                updated_at=now,
                last_used_at=None,
            ),
            body="Profile.",
        ),
    )
    svc = MemoryWriteService(tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="i-touch",
        action=MemoryUpdateAction.TOUCH,
        memory_type=MemoryType.PROFILE,
        memory_id="touch-1",
    )
    audit = svc.apply_intent(intent)
    assert audit.status == WriteStatus.TOUCHED
    assert (tmp_path / "runtime" / "memory_indexes" / "index_by_type.json").exists()


def test_touch_nonexistent_rejected(tmp_path: Path) -> None:
    svc = MemoryWriteService(tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="i7",
        action=MemoryUpdateAction.TOUCH,
        memory_type=MemoryType.PROFILE,
        memory_id="nonexistent",
    )
    audit = svc.apply_intent(intent)
    assert audit.status == WriteStatus.REJECTED_INVALID


def test_idempotent_noop_on_duplicate_intent(tmp_path: Path) -> None:
    svc = MemoryWriteService(tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="i8",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        candidate=MemoryUpdateIntentCandidate(
            confidence=0.9,
            body_markdown="Once.",
        ),
    )
    audit1 = svc.apply_intent(intent)
    assert audit1.status == WriteStatus.WRITTEN
    audit2 = svc.apply_intent(intent)
    assert audit2.status == WriteStatus.IDEMPOTENT_NOOP
    assert audit2.memory_id is None


def test_dedup_merges_into_existing(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="fact-existing",
                type=MemoryType.FACTS,
                tags=["work", "deadline"],
                entities=["project-a"],
                updated_at=now,
            ),
            body="Existing fact.",
        ),
    )
    svc = MemoryWriteService(tmp_path, dedup_enabled=True)
    intent = MemoryUpdateIntent(
        intent_id="i9",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        candidate=MemoryUpdateIntentCandidate(
            tags=["work", "deadline"],
            entities=["project-a"],
            confidence=0.85,
            body_markdown="Additional fact.",
        ),
    )
    audit = svc.apply_intent(intent)
    assert audit.status == WriteStatus.UPDATED
    assert audit.memory_id == "fact-existing"
    artifact = parse_memory_file(tmp_path / "memory" / "facts" / "fact-existing.md")
    assert "Existing fact" in artifact.body
    assert "Additional fact" in artifact.body
    assert "work" in artifact.frontmatter.tags
    assert "deadline" in artifact.frontmatter.tags


def test_idempotency_persists_across_restart(tmp_path: Path) -> None:
    svc1 = MemoryWriteService(tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="i-restart",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        candidate=MemoryUpdateIntentCandidate(
            confidence=0.9,
            body_markdown="Persisted.",
        ),
    )
    audit1 = svc1.apply_intent(intent)
    assert audit1.status == WriteStatus.WRITTEN
    svc2 = MemoryWriteService(tmp_path)
    audit2 = svc2.apply_intent(intent)
    assert audit2.status == WriteStatus.IDEMPOTENT_NOOP


def test_incremental_index_after_write(tmp_path: Path) -> None:
    svc = MemoryWriteService(tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="i10",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        candidate=MemoryUpdateIntentCandidate(
            tags=["indexed"],
            entities=["entity-a"],
            confidence=0.9,
            body_markdown="For retrieval.",
        ),
    )
    audit = svc.apply_intent(intent)
    assert audit.memory_id
    retrieval = RetrievalService(tmp_path)
    result = retrieval.retrieve(
        RetrievalQuery(
            intent_tags=["indexed"],
            intent_entities=["entity-a"],
        )
    )
    assert len(result.scored_artifacts) >= 1
    ids = [sa.artifact.frontmatter.memory_id for sa in result.scored_artifacts]
    assert audit.memory_id in ids
