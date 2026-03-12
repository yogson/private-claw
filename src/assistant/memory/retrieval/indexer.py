"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Index build and load for deterministic memory retrieval.
"""

import json
from typing import Any

from assistant.memory.store.models import MemoryArtifact, MemoryType
from assistant.memory.store.parser import parse_memory_file
from assistant.memory.store.paths import MemoryPaths


def scan_artifacts(paths: MemoryPaths) -> list[MemoryArtifact]:
    artifacts: list[MemoryArtifact] = []
    for cat in MemoryPaths.MEMORY_CATEGORIES:
        cat_dir = paths.category_dir(cat)
        if not cat_dir.exists():
            continue
        for md_path in cat_dir.glob("*.md"):
            try:
                artifacts.append(parse_memory_file(md_path))
            except (ValueError, OSError):
                continue
    return artifacts


def _build_indexes(artifacts: list[MemoryArtifact]) -> dict[str, object]:
    index_by_type: dict[str, list[str]] = {}
    index_by_tag: dict[str, list[str]] = {}
    index_by_entity: dict[str, list[str]] = {}
    index_by_project: dict[str, list[str]] = {}
    index_by_recency: list[dict[str, str | str]] = []

    for a in artifacts:
        mid = a.frontmatter.memory_id
        t = a.frontmatter.type.value
        ts = a.frontmatter.updated_at.isoformat() if a.frontmatter.updated_at else ""
        last_used = a.frontmatter.last_used_at.isoformat() if a.frontmatter.last_used_at else ts
        recency_ts = last_used or ts

        index_by_type.setdefault(t, []).append(mid)
        for tag in a.frontmatter.tags:
            index_by_tag.setdefault(tag.lower(), []).append(mid)
        for entity in a.frontmatter.entities:
            index_by_entity.setdefault(entity.lower(), []).append(mid)
        if t == MemoryType.PROJECTS.value:
            index_by_project.setdefault(mid, []).append(mid)
        for entity in a.frontmatter.entities:
            index_by_project.setdefault(entity.lower(), []).append(mid)

        index_by_recency.append({"memory_id": mid, "updated_at": recency_ts})

    index_by_recency.sort(key=lambda x: x["updated_at"], reverse=True)

    return {
        MemoryPaths.INDEX_BY_TYPE: index_by_type,
        MemoryPaths.INDEX_BY_TAG: index_by_tag,
        MemoryPaths.INDEX_BY_ENTITY: index_by_entity,
        MemoryPaths.INDEX_BY_PROJECT: index_by_project,
        MemoryPaths.INDEX_BY_RECENCY: index_by_recency,
    }


class MemoryIndexer:
    """Build and load filesystem-backed memory indexes."""

    def __init__(self, paths: MemoryPaths) -> None:
        self._paths = paths

    def build(self) -> None:
        """Scan memory files and write index JSON files and version manifest."""
        artifacts = scan_artifacts(self._paths)
        indexes = _build_indexes(artifacts)
        self._paths.indexes_dir.mkdir(parents=True, exist_ok=True)
        for name, data in indexes.items():
            path = self._paths.index_path(name)
            path.write_text(json.dumps(data, indent=0), encoding="utf-8")
        manifest_path = self._paths.index_path(MemoryPaths.INDEX_MANIFEST)
        manifest_path.write_text(
            json.dumps({"version": MemoryPaths.INDEX_VERSION}, indent=0),
            encoding="utf-8",
        )

    def load_index(self, name: str) -> Any:
        """Load a single index by name. Returns empty structure if missing."""
        path = self._paths.index_path(name)
        if not path.exists():
            return _empty_index(name)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _empty_index(name)

    def load_all_indexes(self) -> dict[str, Any]:
        """Load all index files. Missing or corrupt return empty structures."""
        return {
            MemoryPaths.INDEX_BY_TYPE: self.load_index(MemoryPaths.INDEX_BY_TYPE),
            MemoryPaths.INDEX_BY_TAG: self.load_index(MemoryPaths.INDEX_BY_TAG),
            MemoryPaths.INDEX_BY_ENTITY: self.load_index(MemoryPaths.INDEX_BY_ENTITY),
            MemoryPaths.INDEX_BY_PROJECT: self.load_index(MemoryPaths.INDEX_BY_PROJECT),
            MemoryPaths.INDEX_BY_RECENCY: self.load_index(MemoryPaths.INDEX_BY_RECENCY),
        }

    def indexes_exist(self) -> bool:
        """Return True if all index files exist."""
        for name in (
            MemoryPaths.INDEX_BY_TYPE,
            MemoryPaths.INDEX_BY_TAG,
            MemoryPaths.INDEX_BY_ENTITY,
            MemoryPaths.INDEX_BY_PROJECT,
            MemoryPaths.INDEX_BY_RECENCY,
        ):
            if not self._paths.index_path(name).exists():
                return False
        return True

    def add_artifact(self, artifact: MemoryArtifact) -> None:
        """Incrementally add one artifact to indexes."""
        self._paths.indexes_dir.mkdir(parents=True, exist_ok=True)
        indexes = self.load_all_indexes()
        mid = artifact.frontmatter.memory_id
        t = artifact.frontmatter.type.value
        ts = artifact.frontmatter.updated_at.isoformat() if artifact.frontmatter.updated_at else ""
        last_used = (
            artifact.frontmatter.last_used_at.isoformat()
            if artifact.frontmatter.last_used_at
            else ts
        )
        recency_ts = last_used or ts

        by_type = indexes.get(MemoryPaths.INDEX_BY_TYPE, {}) or {}
        by_type.setdefault(t, []).append(mid)
        indexes[MemoryPaths.INDEX_BY_TYPE] = by_type

        by_tag = indexes.get(MemoryPaths.INDEX_BY_TAG, {}) or {}
        for tag in artifact.frontmatter.tags:
            by_tag.setdefault(tag.lower(), []).append(mid)
        indexes[MemoryPaths.INDEX_BY_TAG] = by_tag

        by_entity = indexes.get(MemoryPaths.INDEX_BY_ENTITY, {}) or {}
        for entity in artifact.frontmatter.entities:
            by_entity.setdefault(entity.lower(), []).append(mid)
        indexes[MemoryPaths.INDEX_BY_ENTITY] = by_entity

        by_project = indexes.get(MemoryPaths.INDEX_BY_PROJECT, {}) or {}
        if t == MemoryType.PROJECTS.value:
            by_project.setdefault(mid, []).append(mid)
        for entity in artifact.frontmatter.entities:
            by_project.setdefault(entity.lower(), []).append(mid)
        indexes[MemoryPaths.INDEX_BY_PROJECT] = by_project

        by_recency = indexes.get(MemoryPaths.INDEX_BY_RECENCY, []) or []
        by_recency.append({"memory_id": mid, "updated_at": recency_ts})
        by_recency.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        indexes[MemoryPaths.INDEX_BY_RECENCY] = by_recency

        self._write_indexes(indexes)

    def remove_artifact(
        self,
        memory_type: MemoryType,
        memory_id: str,
        tags: list[str],
        entities: list[str],
    ) -> None:
        """Incrementally remove one artifact from indexes."""
        if not self.indexes_exist():
            return
        indexes = self.load_all_indexes()
        t = memory_type.value

        by_type = indexes.get(MemoryPaths.INDEX_BY_TYPE, {}) or {}
        if t in by_type:
            by_type[t] = [x for x in by_type[t] if x != memory_id]
            if not by_type[t]:
                del by_type[t]
        indexes[MemoryPaths.INDEX_BY_TYPE] = by_type

        by_tag = indexes.get(MemoryPaths.INDEX_BY_TAG, {}) or {}
        for tag in tags:
            tl = tag.lower()
            if tl in by_tag:
                by_tag[tl] = [x for x in by_tag[tl] if x != memory_id]
                if not by_tag[tl]:
                    del by_tag[tl]
        indexes[MemoryPaths.INDEX_BY_TAG] = by_tag

        by_entity = indexes.get(MemoryPaths.INDEX_BY_ENTITY, {}) or {}
        for entity in entities:
            el = entity.lower()
            if el in by_entity:
                by_entity[el] = [x for x in by_entity[el] if x != memory_id]
                if not by_entity[el]:
                    del by_entity[el]
        indexes[MemoryPaths.INDEX_BY_ENTITY] = by_entity

        by_project = indexes.get(MemoryPaths.INDEX_BY_PROJECT, {}) or {}
        if memory_id in by_project:
            del by_project[memory_id]
        for entity in entities:
            el = entity.lower()
            if el in by_project:
                by_project[el] = [x for x in by_project[el] if x != memory_id]
                if not by_project[el]:
                    del by_project[el]
        indexes[MemoryPaths.INDEX_BY_PROJECT] = by_project

        by_recency = indexes.get(MemoryPaths.INDEX_BY_RECENCY, []) or []
        by_recency = [x for x in by_recency if x.get("memory_id") != memory_id]
        indexes[MemoryPaths.INDEX_BY_RECENCY] = by_recency

        self._write_indexes(indexes)

    def _write_indexes(self, indexes: dict[str, object]) -> None:
        for name, data in indexes.items():
            path = self._paths.index_path(name)
            path.write_text(json.dumps(data, indent=0), encoding="utf-8")


def _empty_index(name: str) -> Any:
    if "recency" in name:
        return []
    return {}
