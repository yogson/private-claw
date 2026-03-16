"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Models for index maintenance diagnostics and audit.
"""

from pydantic import BaseModel, Field


class IndexIntegrityResult(BaseModel):
    """Result of index integrity check."""

    is_healthy: bool = Field(..., description="True if indexes are valid and consistent")
    reason: str | None = Field(default=None, description="Human-readable reason when degraded")


class ConsistencyReport(BaseModel):
    """Report from consistency scan of indexes vs source memory files."""

    indexed_ids: set[str] = Field(default_factory=set, description="IDs present in indexes")
    scanned_ids: set[str] = Field(default_factory=set, description="IDs from file scan")
    orphaned_in_index: list[str] = Field(
        default_factory=list, description="IDs in index but not in files"
    )
    missing_from_index: list[str] = Field(
        default_factory=list, description="IDs in files but not in index"
    )
    is_consistent: bool = Field(..., description="True if no orphans and no missing")


class MaintenanceDiagnostics(BaseModel):
    """Audit record for index rebuild/repair operations."""

    recovery_status: str = Field(..., description="full_rebuild, repair, no_action")
    rebuilt: bool = Field(default=False, description="Whether rebuild was performed")
    affected_indexes: list[str] = Field(default_factory=list, description="Index files touched")
    artifact_count: int = Field(default=0, description="Artifacts indexed")
    consistency_issues: list[str] = Field(
        default_factory=list, description="Issues found during consistency scan"
    )
