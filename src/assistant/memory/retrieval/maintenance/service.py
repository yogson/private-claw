"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Index rebuild, repair, and integrity check for memory indexes.
"""

import json
from typing import Any

from assistant.memory.retrieval.indexer import MemoryIndexer, scan_artifacts
from assistant.memory.retrieval.maintenance.models import (
    ConsistencyReport,
    IndexIntegrityResult,
    MaintenanceDiagnostics,
)
from assistant.memory.store.paths import MemoryPaths


def _gather_indexed_ids(indexes: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for _name, data in indexes.items():
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    ids.update(v)
                elif isinstance(v, str):
                    ids.add(v)
        elif isinstance(data, list):
            for e in data:
                if isinstance(e, dict) and "memory_id" in e:
                    ids.add(e["memory_id"])
    return ids


class IndexMaintenanceService:
    """Index integrity check, rebuild, and consistency scan."""

    def __init__(self, paths: MemoryPaths) -> None:
        self._paths = paths
        self._indexer = MemoryIndexer(paths)

    def check_integrity(self) -> IndexIntegrityResult:
        """Check if indexes exist, parse, and match current schema version."""
        manifest_path = self._paths.index_path(MemoryPaths.INDEX_MANIFEST)
        if not manifest_path.exists():
            return IndexIntegrityResult(
                is_healthy=False,
                reason="index_manifest_missing",
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return IndexIntegrityResult(
                is_healthy=False,
                reason="index_manifest_corrupt",
            )
        version = manifest.get("version")
        if version != MemoryPaths.INDEX_VERSION:
            return IndexIntegrityResult(
                is_healthy=False,
                reason=(
                    f"index_version_mismatch: expected {MemoryPaths.INDEX_VERSION}, got {version}"
                ),
            )
        for name in (
            MemoryPaths.INDEX_BY_TYPE,
            MemoryPaths.INDEX_BY_TAG,
            MemoryPaths.INDEX_BY_ENTITY,
            MemoryPaths.INDEX_BY_PROJECT,
            MemoryPaths.INDEX_BY_RECENCY,
        ):
            path = self._paths.index_path(name)
            if not path.exists():
                return IndexIntegrityResult(
                    is_healthy=False,
                    reason=f"index_missing:{name}",
                )
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return IndexIntegrityResult(
                    is_healthy=False,
                    reason=f"index_parse_failure:{name}",
                )
        return IndexIntegrityResult(is_healthy=True)

    def run_consistency_scan(self) -> ConsistencyReport:
        """Compare indexes against source memory files."""
        indexes = self._indexer.load_all_indexes()
        indexed_ids = _gather_indexed_ids(indexes)
        artifacts = scan_artifacts(self._paths)
        scanned_ids = {a.frontmatter.memory_id for a in artifacts}
        orphaned = [i for i in indexed_ids if i not in scanned_ids]
        missing = [s for s in scanned_ids if s not in indexed_ids]
        return ConsistencyReport(
            indexed_ids=indexed_ids,
            scanned_ids=scanned_ids,
            orphaned_in_index=orphaned,
            missing_from_index=missing,
            is_consistent=len(orphaned) == 0 and len(missing) == 0,
        )

    def rebuild(self) -> MaintenanceDiagnostics:
        """Full rebuild of indexes from canonical memory files."""
        self._paths.indexes_dir.mkdir(parents=True, exist_ok=True)
        self._indexer.build()
        artifacts = scan_artifacts(self._paths)
        return MaintenanceDiagnostics(
            recovery_status="full_rebuild",
            rebuilt=True,
            affected_indexes=[
                MemoryPaths.INDEX_BY_TYPE,
                MemoryPaths.INDEX_BY_TAG,
                MemoryPaths.INDEX_BY_ENTITY,
                MemoryPaths.INDEX_BY_PROJECT,
                MemoryPaths.INDEX_BY_RECENCY,
                MemoryPaths.INDEX_MANIFEST,
            ],
            artifact_count=len(artifacts),
        )

    def repair(self) -> MaintenanceDiagnostics:
        """Run consistency scan and rebuild if inconsistent."""
        integrity = self.check_integrity()
        if not integrity.is_healthy:
            diag = self.rebuild()
            diag.consistency_issues.append(integrity.reason or "unknown")
            return diag
        report = self.run_consistency_scan()
        if report.is_consistent:
            return MaintenanceDiagnostics(
                recovery_status="no_action",
                rebuilt=False,
                artifact_count=len(report.scanned_ids),
            )
        diag = self.rebuild()
        diag.recovery_status = "repair"
        diag.consistency_issues = [f"orphaned:{len(report.orphaned_in_index)}"] + [
            f"missing:{len(report.missing_from_index)}"
        ]
        return diag
