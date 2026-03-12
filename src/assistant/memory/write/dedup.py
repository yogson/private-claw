"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Deduplication and consolidation for memory artifacts.
"""

from datetime import datetime

from assistant.memory.store.models import (
    MemoryArtifact,
    MemoryFrontmatter,
    MemoryType,
)
from assistant.memory.store.parser import parse_memory_file
from assistant.memory.store.paths import MemoryPaths

BODY_CONSOLIDATION_SEP = "\n\n---\n\n"


def find_dedup_target(
    paths: MemoryPaths,
    memory_type: MemoryType,
    tags: list[str],
    entities: list[str],
) -> str | None:
    """Find existing artifact with overlapping tags/entities for consolidation."""
    cat_dir = paths.category_dir(memory_type)
    if not cat_dir.exists():
        return None
    tag_set = {t.lower() for t in tags}
    entity_set = {e.lower() for e in entities}
    candidates: list[tuple[str, int, float]] = []
    for md_path in cat_dir.glob("*.md"):
        try:
            artifact = parse_memory_file(md_path)
        except (ValueError, OSError):
            continue
        fm = artifact.frontmatter
        existing_tags = {t.lower() for t in fm.tags}
        existing_entities = {e.lower() for e in fm.entities}
        overlap = len(tag_set & existing_tags) + len(entity_set & existing_entities)
        if overlap > 0:
            candidates.append((fm.memory_id, overlap, fm.confidence))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return candidates[0][0]


def merge_artifact(
    existing: MemoryArtifact,
    new_tags: list[str],
    new_entities: list[str],
    new_priority: int,
    new_confidence: float,
    new_body: str,
    updated_at: datetime,
) -> MemoryArtifact:
    """Merge new candidate into existing artifact: union tags/entities, append body."""
    fm = existing.frontmatter
    merged_tags = _stable_union(fm.tags, new_tags)
    merged_entities = _stable_union(fm.entities, new_entities)
    priority = max(fm.priority, new_priority)
    confidence = max(fm.confidence, new_confidence)
    body = existing.body.strip()
    if new_body.strip():
        body = f"{body}{BODY_CONSOLIDATION_SEP}{new_body.strip()}" if body else new_body.strip()
    return MemoryArtifact(
        frontmatter=MemoryFrontmatter(
            memory_id=fm.memory_id,
            type=fm.type,
            tags=merged_tags,
            entities=merged_entities,
            priority=priority,
            confidence=confidence,
            updated_at=updated_at,
            last_used_at=fm.last_used_at,
            created_at=fm.created_at,
        ),
        body=body,
    )


def _stable_union(a: list[str], b: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for x in a + b:
        xl = x.lower()
        if xl not in seen:
            seen.add(xl)
            result.append(x)
    return result
