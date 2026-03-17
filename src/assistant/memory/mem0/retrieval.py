"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Mem0 Platform-backed memory retrieval adapter.
"""

from datetime import UTC, datetime

import structlog
from mem0 import MemoryClient

from assistant.core.config.schemas import MemoryConfig
from assistant.memory.retrieval.models import (
    RecoveryDiagnostics,
    RetrievalAudit,
    RetrievalQuery,
    RetrievalResult,
    ScoredArtifact,
)
from assistant.memory.store.models import MemoryArtifact, MemoryFrontmatter, MemoryType

logger = structlog.get_logger(__name__)


def _resolve_user_id(query: RetrievalQuery, config: MemoryConfig) -> str:
    return (query.user_id or config.default_user_id).strip() or config.default_user_id


class Mem0RetrievalService:
    """Retrieve memory artifacts via Mem0 Platform search API."""

    def __init__(self, config: MemoryConfig) -> None:
        if not config.api_key.strip():
            raise ValueError(
                "Mem0 api_key is required. Set ASSISTANT_MEMORY_API_KEY or "
                "configure api_key in config/memory.yaml"
            )
        self._config = config
        kwargs: dict[str, str] = {"api_key": config.api_key}
        if config.org_id:
            kwargs["org_id"] = config.org_id
        if config.project_id:
            kwargs["project_id"] = config.project_id
        self._client = MemoryClient(**kwargs)

    def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """Retrieve relevant memories via Mem0 search."""
        user_id = _resolve_user_id(query, self._config)
        search_query = query.user_query_text or "relevant memories"
        filters: dict[str, str] = {"user_id": user_id}
        try:
            raw = self._client.search(search_query, filters=filters, top_k=10)
        except Exception as exc:
            logger.warning(
                "memory.mem0.retrieval_failed",
                query=search_query,
                user_id=user_id,
                error=str(exc),
                exc_info=True,
            )
            return RetrievalResult(
                scored_artifacts=[],
                audit=RetrievalAudit(
                    retrieval_mode="mem0_search_error",
                    candidate_count=0,
                    recovery_diagnostics=RecoveryDiagnostics(
                        status="failure",
                        affected_indexes=[],
                        issues=[str(exc)],
                    ),
                ),
            )
        memories = raw if isinstance(raw, list) else raw.get("memories", raw.get("results", []))
        if not isinstance(memories, list):
            memories = []
        scored: list[ScoredArtifact] = []
        for i, m in enumerate(memories[:10]):
            if not isinstance(m, dict):
                continue
            memory_text = m.get("memory", m.get("content", ""))
            if not memory_text:
                continue
            mem_id = m.get("id", f"mem-{i}")
            score = float(m.get("score", 1.0 - i * 0.1))
            meta = m.get("metadata") or {}
            mem_type_str = meta.get("memory_type", "facts")
            try:
                mem_type = MemoryType(mem_type_str)
            except ValueError:
                mem_type = MemoryType.FACTS
            created = m.get("created_at")
            updated = m.get("updated_at") or created
            if isinstance(created, str):
                try:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    created_dt = datetime.now(UTC)
            else:
                created_dt = datetime.now(UTC)
            if isinstance(updated, str):
                try:
                    updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                except ValueError:
                    updated_dt = datetime.now(UTC)
            else:
                updated_dt = datetime.now(UTC)
            tags = meta.get("tags", [])
            if isinstance(tags, str):
                tags = [tags] if tags else []
            entities = meta.get("entities", [])
            if isinstance(entities, str):
                entities = [entities] if entities else []
            frontmatter = MemoryFrontmatter(
                memory_id=mem_id,
                type=mem_type,
                tags=tags,
                entities=entities,
                priority=5,
                confidence=1.0,
                updated_at=updated_dt,
                last_used_at=None,
                created_at=created_dt,
            )
            artifact = MemoryArtifact(frontmatter=frontmatter, body=memory_text)
            scored.append(ScoredArtifact(artifact=artifact, score=score))
        capped: list[ScoredArtifact] = []
        per_category: dict[MemoryType, int] = {}
        for sa in scored:
            t = sa.artifact.frontmatter.type
            cap = query.category_caps.get(t, 4)
            count = per_category.get(t, 0)
            if count < cap:
                capped.append(sa)
                per_category[t] = count + 1
        audit = RetrievalAudit(
            selected_ids=[sa.artifact.frontmatter.memory_id for sa in capped],
            scores_by_id={sa.artifact.frontmatter.memory_id: sa.score for sa in capped},
            retrieval_mode="mem0_search",
            candidate_count=len(memories),
        )
        return RetrievalResult(scored_artifacts=capped, audit=audit)

    def ensure_indexes(self) -> None:
        """No-op: Mem0 Platform manages indexes internally."""
