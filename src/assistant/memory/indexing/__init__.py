"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Shared indexing and index-maintenance lifecycle for memory artifacts.
"""

from assistant.memory.indexing.indexer import MemoryIndexer, scan_artifacts
from assistant.memory.indexing.maintenance import (
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
    "MemoryIndexer",
    "scan_artifacts",
]
