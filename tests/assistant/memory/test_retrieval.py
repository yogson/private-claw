"""Tests for memory retrieval pipeline."""

from datetime import UTC, datetime
from pathlib import Path

from assistant.memory.retrieval import (
    MemoryIndexer,
    RetrievalQuery,
    RetrievalResult,
    RetrievalService,
)
from assistant.memory.store.models import (
    MemoryArtifact,
    MemoryFrontmatter,
    MemoryType,
)
from assistant.memory.store.parser import serialize_memory_artifact
from assistant.memory.store.paths import MemoryPaths


def _write_artifact(root: Path, artifact: MemoryArtifact) -> None:
    paths = MemoryPaths(root)
    cat_dir = paths.category_dir(artifact.frontmatter.type)
    cat_dir.mkdir(parents=True, exist_ok=True)
    path = paths.artifact_path(artifact.frontmatter.type, artifact.frontmatter.memory_id)
    path.write_text(serialize_memory_artifact(artifact), encoding="utf-8")


def test_indexer_build_and_load(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    artifacts = [
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="prof-1",
                type=MemoryType.PROFILE,
                tags=["user"],
                entities=["me"],
                updated_at=now,
            ),
            body="User profile content.",
        ),
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="fact-1",
                type=MemoryType.FACTS,
                tags=["work", "deadline"],
                entities=["project-x"],
                priority=7,
                confidence=0.9,
                updated_at=now,
            ),
            body="Important fact about project.",
        ),
    ]
    for a in artifacts:
        _write_artifact(tmp_path, a)

    paths = MemoryPaths(tmp_path)
    indexer = MemoryIndexer(paths)
    indexer.build()

    assert paths.index_path("index_by_type.json").exists()
    by_type = indexer.load_index("index_by_type.json")
    assert isinstance(by_type, dict)
    assert "profile" in by_type
    assert "prof-1" in by_type["profile"]
    assert "facts" in by_type
    assert "fact-1" in by_type["facts"]

    by_tag = indexer.load_index("index_by_tag.json")
    assert "work" in by_tag
    assert "fact-1" in by_tag["work"]
    assert "deadline" in by_tag
    assert "fact-1" in by_tag["deadline"]


def test_retrieval_service_empty(tmp_path: Path) -> None:
    svc = RetrievalService(tmp_path)
    svc.ensure_indexes()
    result = svc.retrieve(RetrievalQuery())
    assert isinstance(result, RetrievalResult)
    assert len(result.scored_artifacts) == 0
    assert result.audit.retrieval_mode == "deterministic"


def test_retrieval_service_with_artifacts(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="pref-dark",
                type=MemoryType.PREFERENCES,
                tags=["theme", "ui"],
                entities=[],
                priority=8,
                confidence=1.0,
                updated_at=now,
            ),
            body="User prefers dark mode.",
        ),
    )
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="fact-1",
                type=MemoryType.FACTS,
                tags=["work"],
                entities=["project-a"],
                priority=5,
                confidence=0.8,
                updated_at=now,
            ),
            body="Project A deadline is next week.",
        ),
    )

    svc = RetrievalService(tmp_path)
    svc.ensure_indexes()
    result = svc.retrieve(
        RetrievalQuery(
            intent_tags=["work"],
            intent_entities=["project-a"],
        )
    )
    assert len(result.scored_artifacts) >= 1
    ids = [sa.artifact.frontmatter.memory_id for sa in result.scored_artifacts]
    assert "fact-1" in ids
    assert result.audit.candidate_count >= 1


def test_retrieval_category_caps(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    for i in range(5):
        _write_artifact(
            tmp_path,
            MemoryArtifact(
                frontmatter=MemoryFrontmatter(
                    memory_id=f"fact-{i}",
                    type=MemoryType.FACTS,
                    tags=[],
                    entities=[],
                    priority=5,
                    confidence=0.9,
                    updated_at=now,
                ),
                body=f"Fact {i} content.",
            ),
        )

    svc = RetrievalService(tmp_path)
    svc.ensure_indexes()
    result = svc.retrieve(
        RetrievalQuery(
            category_caps={MemoryType.FACTS: 2},
        )
    )
    facts_selected = [
        sa for sa in result.scored_artifacts if sa.artifact.frontmatter.type == MemoryType.FACTS
    ]
    assert len(facts_selected) <= 2


def test_retrieval_bm25_mode(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="bm25-test",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="Python programming and coding best practices.",
        ),
    )

    svc = RetrievalService(tmp_path)
    svc.ensure_indexes()
    result = svc.retrieve(RetrievalQuery(user_query_text="Python coding"))
    assert result.audit.retrieval_mode == "deterministic_plus_bm25"
    if result.scored_artifacts:
        assert result.audit.scores_by_id


def test_retrieval_degraded_fallback(tmp_path: Path) -> None:
    """When indexes exist but are empty/corrupt, retrieve falls back to direct file scan."""
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="fallback-1",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="Content for degraded fallback.",
        ),
    )
    index_dir = tmp_path / "runtime" / "memory_indexes"
    index_dir.mkdir(parents=True)
    (index_dir / "index_by_type.json").write_text("{}")
    (index_dir / "index_by_tag.json").write_text("{}")
    (index_dir / "index_by_entity.json").write_text("{}")
    (index_dir / "index_by_project.json").write_text("{}")
    (index_dir / "index_by_recency.json").write_text("[]")
    svc = RetrievalService(tmp_path)
    result = svc.retrieve(RetrievalQuery())
    assert len(result.scored_artifacts) >= 1
    assert result.audit.retrieval_mode == "degraded_fallback"


def test_retrieval_case_insensitive_tags(tmp_path: Path) -> None:
    """Tag/entity lookup is case-insensitive."""
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="case-test",
                type=MemoryType.FACTS,
                tags=["Work", "Deadline"],
                entities=["Project-X"],
                updated_at=now,
            ),
            body="Mixed case tags and entities.",
        ),
    )
    svc = RetrievalService(tmp_path)
    svc.ensure_indexes()
    result = svc.retrieve(
        RetrievalQuery(
            intent_tags=["work"],
            intent_entities=["project-x"],
        )
    )
    ids = [sa.artifact.frontmatter.memory_id for sa in result.scored_artifacts]
    assert "case-test" in ids


def test_indexer_load_missing_returns_empty(tmp_path: Path) -> None:
    paths = MemoryPaths(tmp_path)
    indexer = MemoryIndexer(paths)
    by_type = indexer.load_index("index_by_type.json")
    assert by_type == {}
    by_recency = indexer.load_index("index_by_recency.json")
    assert by_recency == []
