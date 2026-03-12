"""Tests for memory index maintenance: rebuild, repair, integrity check."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from assistant.memory.maintenance import (
    ConsistencyReport,
    IndexIntegrityResult,
    IndexMaintenanceService,
    MaintenanceDiagnostics,
)
from assistant.memory.retrieval import RetrievalQuery, RetrievalService
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


def test_check_integrity_missing_manifest(tmp_path: Path) -> None:
    """Missing manifest returns degraded."""
    paths = MemoryPaths(tmp_path)
    svc = IndexMaintenanceService(paths)
    result = svc.check_integrity()
    assert isinstance(result, IndexIntegrityResult)
    assert result.is_healthy is False
    assert "manifest" in (result.reason or "").lower()


def test_check_integrity_corrupt_manifest(tmp_path: Path) -> None:
    """Corrupt manifest returns degraded."""
    index_dir = tmp_path / "runtime" / "memory_indexes"
    index_dir.mkdir(parents=True)
    (index_dir / "index_version.json").write_text("not json")
    paths = MemoryPaths(tmp_path)
    svc = IndexMaintenanceService(paths)
    result = svc.check_integrity()
    assert result.is_healthy is False
    assert "corrupt" in (result.reason or "").lower()


def test_check_integrity_version_mismatch(tmp_path: Path) -> None:
    """Version mismatch returns degraded."""
    index_dir = tmp_path / "runtime" / "memory_indexes"
    index_dir.mkdir(parents=True)
    (index_dir / "index_version.json").write_text('{"version": 99}')
    for name in (
        "index_by_type.json",
        "index_by_tag.json",
        "index_by_entity.json",
        "index_by_project.json",
        "index_by_recency.json",
    ):
        (index_dir / name).write_text("{}" if "recency" not in name else "[]")
    paths = MemoryPaths(tmp_path)
    svc = IndexMaintenanceService(paths)
    result = svc.check_integrity()
    assert result.is_healthy is False
    assert "version" in (result.reason or "").lower()


def test_check_integrity_healthy(tmp_path: Path) -> None:
    """Valid indexes return healthy."""
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="test-1",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="",
        ),
    )
    paths = MemoryPaths(tmp_path)
    svc = IndexMaintenanceService(paths)
    diag = svc.rebuild()
    assert diag.rebuilt
    result = svc.check_integrity()
    assert result.is_healthy is True
    assert result.reason is None


def test_rebuild_creates_indexes_and_manifest(tmp_path: Path) -> None:
    """Rebuild creates all index files and manifest."""
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="prof-1",
                type=MemoryType.PROFILE,
                tags=["user"],
                entities=["me"],
                updated_at=now,
            ),
            body="Profile content.",
        ),
    )
    paths = MemoryPaths(tmp_path)
    svc = IndexMaintenanceService(paths)
    diag = svc.rebuild()
    assert isinstance(diag, MaintenanceDiagnostics)
    assert diag.rebuilt is True
    assert diag.recovery_status == "full_rebuild"
    assert diag.artifact_count == 1
    assert len(diag.affected_indexes) >= 5
    manifest = paths.index_path(MemoryPaths.INDEX_MANIFEST)
    assert manifest.exists()
    data = json.loads(manifest.read_text())
    assert data.get("version") == MemoryPaths.INDEX_VERSION


def test_consistency_scan_consistent(tmp_path: Path) -> None:
    """Consistency scan reports consistent when index matches files."""
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="a1",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="",
        ),
    )
    paths = MemoryPaths(tmp_path)
    svc = IndexMaintenanceService(paths)
    svc.rebuild()
    report = svc.run_consistency_scan()
    assert isinstance(report, ConsistencyReport)
    assert report.is_consistent is True
    assert "a1" in report.scanned_ids
    assert "a1" in report.indexed_ids
    assert len(report.orphaned_in_index) == 0
    assert len(report.missing_from_index) == 0


def test_consistency_scan_inconsistent(tmp_path: Path) -> None:
    """Consistency scan detects orphans and missing."""
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="present",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="",
        ),
    )
    paths = MemoryPaths(tmp_path)
    svc = IndexMaintenanceService(paths)
    svc.rebuild()
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="new_one",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="",
        ),
    )
    report = svc.run_consistency_scan()
    assert report.is_consistent is False
    assert "new_one" in report.missing_from_index


def test_repair_rebuilds_when_inconsistent(tmp_path: Path) -> None:
    """Repair triggers rebuild when consistency fails."""
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="x",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="",
        ),
    )
    paths = MemoryPaths(tmp_path)
    svc = IndexMaintenanceService(paths)
    svc.rebuild()
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="y",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="",
        ),
    )
    diag = svc.repair()
    assert diag.rebuilt is True
    assert diag.recovery_status == "repair"
    assert len(diag.consistency_issues) >= 1


def test_retrieval_degraded_fallback_sets_recovery_diagnostics(tmp_path: Path) -> None:
    """Degraded fallback with integrity failure sets recovery_diagnostics."""
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="audit-test",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="Content.",
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
    assert result.audit.recovery_diagnostics is not None
    assert result.audit.recovery_diagnostics.status == "full_rebuild"
    assert len(result.audit.recovery_diagnostics.affected_indexes) >= 5
    assert any("manifest" in i for i in result.audit.recovery_diagnostics.issues)


def test_retrieval_degraded_fallback_continues_on_rebuild_failure(tmp_path: Path) -> None:
    """When rebuild raises OSError, retrieval still returns results with failure diagnostics."""
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="resilient",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="Content.",
        ),
    )
    index_dir = tmp_path / "runtime" / "memory_indexes"
    index_dir.mkdir(parents=True)
    for name in (
        "index_by_type.json",
        "index_by_tag.json",
        "index_by_entity.json",
        "index_by_project.json",
        "index_by_recency.json",
    ):
        (index_dir / name).write_text("{}" if "recency" not in name else "[]")

    with patch.object(
        IndexMaintenanceService,
        "rebuild",
        side_effect=OSError("Permission denied"),
    ):
        svc = RetrievalService(tmp_path)
        result = svc.retrieve(RetrievalQuery())
    assert len(result.scored_artifacts) >= 1
    assert result.audit.retrieval_mode == "degraded_fallback"
    assert result.audit.recovery_diagnostics is not None
    assert result.audit.recovery_diagnostics.status == "failure"
    assert any("rebuild_failed" in i for i in result.audit.recovery_diagnostics.issues)


def test_repair_no_action_when_consistent(tmp_path: Path) -> None:
    """Repair does nothing when indexes are consistent."""
    now = datetime.now(UTC)
    _write_artifact(
        tmp_path,
        MemoryArtifact(
            frontmatter=MemoryFrontmatter(
                memory_id="z",
                type=MemoryType.FACTS,
                tags=[],
                entities=[],
                updated_at=now,
            ),
            body="",
        ),
    )
    paths = MemoryPaths(tmp_path)
    svc = IndexMaintenanceService(paths)
    svc.rebuild()
    diag = svc.repair()
    assert diag.rebuilt is False
    assert diag.recovery_status == "no_action"
