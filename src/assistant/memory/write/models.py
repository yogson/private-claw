"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Memory update intent and write audit models.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from assistant.memory.store.models import MemoryType


class MemoryUpdateAction(StrEnum):
    """Supported memory update actions."""

    UPSERT = "upsert"
    DELETE = "delete"
    TOUCH = "touch"


class MemoryUpdateSource(StrEnum):
    """Source of memory update intent."""

    EXPLICIT_USER_REQUEST = "explicit_user_request"
    AGENT_INFERRED = "agent_inferred"
    CAPABILITY_OUTPUT = "capability_output"
    SCHEDULER = "scheduler"


class MemoryUpdateIntentCandidate(BaseModel):
    """Candidate content for upsert."""

    tags: list[str] = Field(default_factory=list, description="Topical tags")
    entities: list[str] = Field(default_factory=list, description="Referenced entities")
    priority: int = Field(default=5, ge=0, le=10, description="Priority 0-10")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence 0.0-1.0")
    body_markdown: str = Field(default="", description="Markdown body content")


class MemoryUpdateIntent(BaseModel):
    """Orchestrator-normalized memory update intent.

    Schema matches memory domain intake contract.
    """

    intent_id: str = Field(..., min_length=1, description="Unique intent identifier")
    action: MemoryUpdateAction = Field(..., description="upsert, delete, or touch")
    memory_type: MemoryType = Field(..., description="Memory category")
    memory_id: str | None = Field(default=None, description="Existing memory ID for update")
    candidate: MemoryUpdateIntentCandidate | None = Field(
        default=None,
        description="Candidate content for upsert",
    )
    reason: str = Field(default="", description="Capture rationale")
    source: MemoryUpdateSource = Field(
        default=MemoryUpdateSource.AGENT_INFERRED,
        description="Intent source",
    )


class WriteStatus(StrEnum):
    """Result status for a write intent."""

    WRITTEN = "written"
    UPDATED = "updated"
    DELETED = "deleted"
    TOUCHED = "touched"
    SKIPPED_LOW_CONFIDENCE = "skipped_low_confidence"
    SKIPPED_DEDUP_MERGED = "skipped_dedup_merged"
    IDEMPOTENT_NOOP = "idempotent_noop"
    REJECTED_INVALID = "rejected_invalid"


class WriteAudit(BaseModel):
    """Per-intent audit result."""

    intent_id: str = Field(..., description="Intent identifier")
    status: WriteStatus = Field(..., description="Final status")
    memory_id: str | None = Field(default=None, description="Affected memory ID")
    reason: str = Field(default="", description="Status reason or error")
