"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Retrieval query and result models for INT_ORCH_CONTEXT_BUILD.
"""

from pydantic import BaseModel, Field

from assistant.memory.store.models import MemoryArtifact, MemoryType


class RetrievalQuery(BaseModel):
    """Query for memory retrieval (INT_ORCH_CONTEXT_BUILD input)."""

    user_id: str | None = Field(
        default=None,
        description="User/session scope for retrieval (required for Mem0)",
    )
    intent_entities: list[str] = Field(default_factory=list, description="Entities from user turn")
    intent_tags: list[str] = Field(default_factory=list, description="Topical tags from user turn")
    intent_types: list[MemoryType] = Field(
        default_factory=list, description="Memory types to consider"
    )
    user_query_text: str | None = Field(
        default=None, description="Optional user message for BM25 body relevance"
    )
    category_caps: dict[MemoryType, int] = Field(
        default_factory=lambda: {
            MemoryType.PROFILE: 2,
            MemoryType.PREFERENCES: 3,
            MemoryType.PROJECTS: 4,
            MemoryType.TASKS: 4,
            MemoryType.FACTS: 3,
            MemoryType.SUMMARIES: 1,
        },
        description="Max artifacts per category",
    )


class ScoredArtifact(BaseModel):
    """Memory artifact with retrieval score."""

    artifact: MemoryArtifact
    score: float = Field(..., ge=0, description="Combined retrieval score")


class RecoveryDiagnostics(BaseModel):
    """Structured recovery info when degraded-fallback path is used."""

    status: str = Field(..., description="full_rebuild, repair, failure, no_action")
    affected_indexes: list[str] = Field(
        default_factory=list, description="Index files touched or attempted"
    )
    issues: list[str] = Field(
        default_factory=list, description="Integrity/consistency issues or failure reason"
    )


class RetrievalAudit(BaseModel):
    """Audit data for retrieval (selected IDs, scores, mode)."""

    selected_ids: list[str] = Field(default_factory=list)
    scores_by_id: dict[str, float] = Field(default_factory=dict)
    retrieval_mode: str = Field(
        default="deterministic", description="deterministic or deterministic_plus_bm25"
    )
    candidate_count: int = 0
    recovery_diagnostics: RecoveryDiagnostics | None = Field(
        default=None,
        description="When degraded_fallback: structured recovery status and affected indexes",
    )


class RetrievalResult(BaseModel):
    """Result of memory retrieval (INT_ORCH_CONTEXT_BUILD output)."""

    scored_artifacts: list[ScoredArtifact] = Field(default_factory=list)
    audit: RetrievalAudit = Field(default_factory=RetrievalAudit)
