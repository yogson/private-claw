"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Deterministic retrieval pipeline with category caps and bounded context assembly.
"""

from pathlib import Path
from typing import Any

from assistant.memory.retrieval.indexer import MemoryIndexer, scan_artifacts
from assistant.memory.retrieval.models import (
    RetrievalAudit,
    RetrievalQuery,
    RetrievalResult,
    ScoredArtifact,
)
from assistant.memory.retrieval.scoring import blend_scores, score_bm25, score_metadata
from assistant.memory.store.models import MemoryArtifact, MemoryType
from assistant.memory.store.parser import parse_memory_file
from assistant.memory.store.paths import MemoryPaths


def _has_intent_hints(query: RetrievalQuery) -> bool:
    return bool(query.intent_types or query.intent_tags or query.intent_entities)


def _gather_candidates(indexes: dict[str, Any], query: RetrievalQuery) -> set[str]:
    candidates: set[str] = set()
    by_type = indexes.get(MemoryPaths.INDEX_BY_TYPE, {}) or {}
    by_tag = indexes.get(MemoryPaths.INDEX_BY_TAG, {}) or {}
    by_entity = indexes.get(MemoryPaths.INDEX_BY_ENTITY, {}) or {}
    by_project = indexes.get(MemoryPaths.INDEX_BY_PROJECT, {}) or {}
    by_recency = indexes.get(MemoryPaths.INDEX_BY_RECENCY, []) or []

    if query.intent_types:
        for t in query.intent_types:
            candidates.update(by_type.get(t.value, []))
    elif not _has_intent_hints(query):
        for ids in by_type.values():
            candidates.update(ids)

    for tag in query.intent_tags:
        candidates.update(by_tag.get(tag.lower(), []))
    for entity in query.intent_entities:
        el = entity.lower()
        candidates.update(by_entity.get(el, []))
        candidates.update(by_project.get(el, []))

    if not candidates and by_recency:
        for entry in by_recency[:50]:
            mid = entry.get("memory_id") if isinstance(entry, dict) else None
            if mid:
                candidates.add(mid)

    return candidates


def _build_recency_rank(indexes: dict[str, Any]) -> dict[str, int]:
    by_recency = indexes.get(MemoryPaths.INDEX_BY_RECENCY, []) or []
    result: dict[str, int] = {}
    for i, e in enumerate(by_recency):
        if isinstance(e, dict):
            mid = e.get("memory_id", "")
            if mid:
                result[mid] = i
    return result


def _load_artifacts(paths: MemoryPaths, memory_ids: set[str]) -> dict[str, MemoryArtifact]:
    result: dict[str, MemoryArtifact] = {}
    for cat in MemoryPaths.MEMORY_CATEGORIES:
        cat_dir = paths.category_dir(cat)
        if not cat_dir.exists():
            continue
        for md_path in cat_dir.glob("*.md"):
            mid = md_path.stem
            if mid not in memory_ids:
                continue
            try:
                result[mid] = parse_memory_file(md_path)
            except (ValueError, OSError):
                continue
    return result


class RetrievalService:
    """Deterministic retrieval with index-backed candidate selection and weighted scoring."""

    def __init__(self, data_root: Path | str) -> None:
        self._paths = MemoryPaths(Path(data_root))
        self._indexer = MemoryIndexer(self._paths)

    def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """Retrieve relevant memory artifacts with category caps and bounded context."""
        indexes = self._indexer.load_all_indexes()
        candidates = _gather_candidates(indexes, query)

        if not candidates and not self._indexer.indexes_exist():
            self._indexer.build()
            indexes = self._indexer.load_all_indexes()
            candidates = _gather_candidates(indexes, query)

        used_degraded_fallback = False
        if not candidates:
            scanned = scan_artifacts(self._paths)
            candidates = {a.frontmatter.memory_id for a in scanned}
            if scanned:
                used_degraded_fallback = True
                recency_ts = [
                    (
                        a.frontmatter.last_used_at or a.frontmatter.updated_at,
                        a.frontmatter.memory_id,
                    )
                    for a in scanned
                ]
                recency_ts.sort(
                    key=lambda x: x[0].isoformat() if x[0] else "",
                    reverse=True,
                )
                indexes = dict(indexes)
                indexes[MemoryPaths.INDEX_BY_RECENCY] = [
                    {"memory_id": mid, "updated_at": (ts.isoformat() if ts else "")}
                    for ts, mid in recency_ts
                ]

        recency_rank = _build_recency_rank(indexes)
        artifacts_map = _load_artifacts(self._paths, candidates)
        artifacts = list(artifacts_map.values())

        metadata_scores = {
            a.frontmatter.memory_id: score_metadata(a, query, recency_rank) for a in artifacts
        }
        bm25_scores = (
            score_bm25(artifacts, query.user_query_text)
            if query.user_query_text and query.user_query_text.strip()
            else {}
        )
        final_scores = blend_scores(
            metadata_scores,
            bm25_scores,
            bm25_weight=0.3 if bm25_scores else 0.0,
        )

        scored: list[ScoredArtifact] = [
            ScoredArtifact(artifact=a, score=final_scores[a.frontmatter.memory_id])
            for a in artifacts
        ]
        scored.sort(key=lambda x: x.score, reverse=True)

        capped: list[ScoredArtifact] = []
        per_category: dict[MemoryType, int] = {}
        for sa in scored:
            t = sa.artifact.frontmatter.type
            cap = query.category_caps.get(t, 4)
            count = per_category.get(t, 0)
            if count < cap:
                capped.append(sa)
                per_category[t] = count + 1

        if used_degraded_fallback:
            mode = "degraded_fallback"
        elif bm25_scores:
            mode = "deterministic_plus_bm25"
        else:
            mode = "deterministic"
        audit = RetrievalAudit(
            selected_ids=[sa.artifact.frontmatter.memory_id for sa in capped],
            scores_by_id={sa.artifact.frontmatter.memory_id: sa.score for sa in capped},
            retrieval_mode=mode,
            candidate_count=len(candidates),
        )
        return RetrievalResult(scored_artifacts=capped, audit=audit)

    def ensure_indexes(self) -> None:
        """Build indexes if missing. Call on startup for integrity check."""
        if not self._indexer.indexes_exist():
            self._indexer.build()
