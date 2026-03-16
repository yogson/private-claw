"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Index rebuild, repair, and degraded-mode diagnostics.
"""

from assistant.memory.indexing.maintenance.models import (
    ConsistencyReport,
    IndexIntegrityResult,
    MaintenanceDiagnostics,
)
from assistant.memory.indexing.maintenance.service import IndexMaintenanceService

__all__ = [
    "ConsistencyReport",
    "IndexIntegrityResult",
    "IndexMaintenanceService",
    "MaintenanceDiagnostics",
]
