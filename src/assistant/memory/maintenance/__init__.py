"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Index rebuild, repair, and degraded-mode diagnostics.

Re-exports from retrieval.maintenance for backward compatibility.
"""

from assistant.memory.retrieval.maintenance import (
    ConsistencyReport,
    IndexIntegrityResult,
    IndexMaintenanceService,
    MaintenanceDiagnostics,
)

__all__ = [
    "ConsistencyReport",
    "IndexIntegrityResult",
    "IndexMaintenanceService",
    "MaintenanceDiagnostics",
]
