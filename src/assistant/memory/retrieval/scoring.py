"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Transparent weighted scoring for memory retrieval (industry-standard BM25 + metadata).
"""

import re
from typing import TYPE_CHECKING

from rank_bm25 import BM25Okapi

from assistant.memory.store.models import MemoryArtifact, MemoryType

if TYPE_CHECKING:
    from assistant.memory.retrieval.models import RetrievalQuery

_DEFAULT_WEIGHTS = {
    "entity_match": 1.0,
    "tag_match": 0.8,
    "type_match": 0.6,
    "recency": 0.5,
    "priority": 0.4,
    "confidence": 0.6,
}
_RECENCY_DECAY_DAYS = 30.0


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"\b[a-zA-Z0-9]+\b", text.lower())
    return [t for t in tokens if len(t) > 1]


def _entity_score(artifact: MemoryArtifact, entities: list[str]) -> float:
    if not entities:
        return 0.5
    overlap = set(e.lower() for e in entities) & set(
        x.lower() for x in artifact.frontmatter.entities
    )
    if not overlap:
        return 0.0
    return min(1.0, 0.5 + 0.5 * len(overlap) / max(1, len(entities)))


def _tag_score(artifact: MemoryArtifact, tags: list[str]) -> float:
    if not tags:
        return 0.5
    overlap = set(t.lower() for t in tags) & set(x.lower() for x in artifact.frontmatter.tags)
    if not overlap:
        return 0.0
    return min(1.0, 0.5 + 0.5 * len(overlap) / max(1, len(tags)))


def _type_score(artifact: MemoryArtifact, types: list[MemoryType]) -> float:
    if not types:
        return 0.5
    return 1.0 if artifact.frontmatter.type in types else 0.0


def _recency_score(artifact: MemoryArtifact, recency_rank: dict[str, int]) -> float:
    rank = recency_rank.get(artifact.frontmatter.memory_id, 999)
    decay = 1.0 - (rank / (rank + _RECENCY_DECAY_DAYS))
    return max(0.0, min(1.0, decay))


def _priority_score(artifact: MemoryArtifact) -> float:
    return artifact.frontmatter.priority / 10.0


def _confidence_score(artifact: MemoryArtifact) -> float:
    return artifact.frontmatter.confidence


def score_metadata(
    artifact: MemoryArtifact,
    query: "RetrievalQuery",
    recency_rank: dict[str, int],
) -> float:
    """Compute transparent weighted metadata score."""
    w = _DEFAULT_WEIGHTS
    s = 0.0
    s += w["entity_match"] * _entity_score(artifact, query.intent_entities)
    s += w["tag_match"] * _tag_score(artifact, query.intent_tags)
    s += w["type_match"] * _type_score(artifact, query.intent_types)
    s += w["recency"] * _recency_score(artifact, recency_rank)
    s += w["priority"] * _priority_score(artifact)
    s += w["confidence"] * _confidence_score(artifact)
    return s


def score_bm25(artifacts: list[MemoryArtifact], query_text: str) -> dict[str, float]:
    """Compute BM25 scores for artifacts against query (industry-standard Okapi BM25)."""
    if not query_text.strip():
        return {}
    query_tokens = _tokenize(query_text)
    if not query_tokens:
        return {}
    corpus = [_tokenize(a.body) for a in artifacts]
    if not any(corpus):
        return {}
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(query_tokens)
    return {a.frontmatter.memory_id: float(s) for a, s in zip(artifacts, scores, strict=True)}


def blend_scores(
    metadata_scores: dict[str, float],
    bm25_scores: dict[str, float],
    bm25_weight: float = 0.3,
) -> dict[str, float]:
    """Blend metadata and BM25 scores. BM25 is optional (when user_query provided)."""
    all_ids = set(metadata_scores) | set(bm25_scores)
    result: dict[str, float] = {}
    max_bm25 = max(bm25_scores.values(), default=1.0) or 1.0
    for mid in all_ids:
        meta = metadata_scores.get(mid, 0.0)
        bm25_raw = bm25_scores.get(mid, 0.0)
        bm25_norm = bm25_raw / max_bm25 if max_bm25 else 0.0
        blended = (1.0 - bm25_weight) * meta + bm25_weight * bm25_norm
        result[mid] = max(0.0, blended)
    return result
