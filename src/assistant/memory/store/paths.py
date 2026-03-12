"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Directory structure and path layout for memory artifacts and indexes.
"""

from pathlib import Path

from assistant.memory.store.models import MemoryType


def _validate_path_safe(memory_id: str) -> None:
    """Reject path separators and traversal segments."""
    if "/" in memory_id or "\\" in memory_id or ".." in memory_id:
        raise ValueError(f"memory_id must not contain path separators or '..': {memory_id!r}")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
    if not memory_id or not all(c in allowed for c in memory_id):
        raise ValueError(
            f"memory_id must be path-safe (alphanumeric, underscore, hyphen, dot): {memory_id!r}"
        )


class MemoryPaths:
    """Path layout for memory artifacts and indexes under data root.

    Artifacts: data_root/memory/{category}/{memory_id}.md
    Indexes: data_root/runtime/memory_indexes/*.json
    """

    MEMORY_CATEGORIES = (
        MemoryType.PROFILE,
        MemoryType.PREFERENCES,
        MemoryType.PROJECTS,
        MemoryType.TASKS,
        MemoryType.FACTS,
        MemoryType.SUMMARIES,
    )

    INDEX_BY_TYPE = "index_by_type.json"
    INDEX_BY_TAG = "index_by_tag.json"
    INDEX_BY_ENTITY = "index_by_entity.json"
    INDEX_BY_PROJECT = "index_by_project.json"
    INDEX_BY_RECENCY = "index_by_recency.json"

    def __init__(self, data_root: Path) -> None:
        self._data_root = Path(data_root)
        self._memory_root = self._data_root / "memory"
        self._indexes_dir = self._data_root / "runtime" / "memory_indexes"

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def memory_root(self) -> Path:
        return self._memory_root

    @property
    def indexes_dir(self) -> Path:
        return self._indexes_dir

    def category_dir(self, memory_type: MemoryType) -> Path:
        """Directory for a memory category."""
        return self._memory_root / memory_type.value

    def artifact_path(self, memory_type: MemoryType, memory_id: str) -> Path:
        """Path for a memory artifact file.

        Raises:
            ValueError: If memory_id contains path separators or traversal segments.
        """
        _validate_path_safe(memory_id)
        cat_dir = self.category_dir(memory_type)
        resolved = (cat_dir / f"{memory_id}.md").resolve()
        cat_resolved = cat_dir.resolve()
        try:
            resolved.relative_to(cat_resolved)
        except ValueError:
            raise ValueError(f"memory_id would escape category directory: {memory_id!r}") from None
        return cat_dir / f"{memory_id}.md"

    def index_path(self, index_name: str) -> Path:
        """Path for an index file."""
        return self._indexes_dir / index_name
