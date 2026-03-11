"""
Component ID: CMP_STORE_STATE_FACADE

Store module providing persistence abstractions for sessions, tasks, idempotency, and locks.
"""

from assistant.store.facade import StoreFacade
from assistant.store.interfaces import (
    IdempotencyLedgerInterface,
    LockCoordinatorInterface,
    SessionStoreInterface,
    StoreFacadeInterface,
    TaskStoreInterface,
)

__all__ = [
    "StoreFacade",
    "StoreFacadeInterface",
    "SessionStoreInterface",
    "TaskStoreInterface",
    "IdempotencyLedgerInterface",
    "LockCoordinatorInterface",
]
