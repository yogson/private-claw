"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Pydantic schemas for memory artifact frontmatter and directory model.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class MemoryType(StrEnum):
    """Canonical memory categories for v1."""

    PROFILE = "profile"
    PREFERENCES = "preferences"
    PROJECTS = "projects"
    TASKS = "tasks"
    FACTS = "facts"
    SUMMARIES = "summaries"


class MemoryFrontmatter(BaseModel):
    """Required frontmatter fields for memory artifacts.

    All persisted memory files must validate against this schema.
    """

    memory_id: str = Field(..., min_length=1, description="Unique memory identifier")
    type: MemoryType = Field(..., description="Memory category")
    tags: list[str] = Field(default_factory=list, description="Topical tags for retrieval")
    entities: list[str] = Field(default_factory=list, description="Referenced entities")
    priority: int = Field(default=5, ge=0, le=10, description="Priority score 0-10")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence 0.0-1.0")
    updated_at: datetime = Field(..., description="Last update timestamp")
    last_used_at: datetime | None = Field(default=None, description="Last retrieval timestamp")
    created_at: datetime | None = Field(default=None, description="Creation timestamp")

    @field_validator("memory_id")
    @classmethod
    def memory_id_path_safe(cls, v: str) -> str:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
        if not v or not all(c in allowed for c in v):
            raise ValueError("memory_id must be path-safe (alphanumeric, underscore, hyphen, dot)")
        return v


class MemoryArtifact(BaseModel):
    """Complete memory artifact: frontmatter plus markdown body."""

    frontmatter: MemoryFrontmatter
    body: str = Field(default="", description="Markdown body content")
