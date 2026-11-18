"""
Component ID: CMP_CORE_CONFIRMATION_SERVICE

Generic user-confirmation mechanism (memory updates first; other kinds plug in
through ConfirmationHandlerRegistry).
"""

from assistant.core.confirmation.handlers import (
    ConfirmationHandlerInterface,
    ConfirmationHandlerRegistry,
)
from assistant.core.confirmation.history import (
    CONFIRMATION_RESOLVED_MARKER,
    ConfirmationRecorderInterface,
    SessionConfirmationRecorder,
    format_resolution_text,
)
from assistant.core.confirmation.memory_update import (
    MEMORY_UPDATE_KIND,
    MemoryUpdateHandler,
    build_memory_update_draft,
)
from assistant.core.confirmation.models import (
    ConfirmationDecision,
    ConfirmationDraft,
    ConfirmationOutcome,
    ConfirmationRequest,
    ConfirmationRequestResult,
    ConfirmationResolveResult,
    ConfirmationStatus,
    ConfirmationUiRef,
)
from assistant.core.confirmation.service import (
    DEFAULT_CONFIRMATION_TTL_SECONDS,
    ConfirmationService,
)

__all__ = [
    "CONFIRMATION_RESOLVED_MARKER",
    "DEFAULT_CONFIRMATION_TTL_SECONDS",
    "MEMORY_UPDATE_KIND",
    "ConfirmationDecision",
    "ConfirmationDraft",
    "ConfirmationHandlerInterface",
    "ConfirmationHandlerRegistry",
    "ConfirmationOutcome",
    "ConfirmationRecorderInterface",
    "ConfirmationRequest",
    "ConfirmationRequestResult",
    "ConfirmationResolveResult",
    "ConfirmationService",
    "ConfirmationStatus",
    "ConfirmationUiRef",
    "MemoryUpdateHandler",
    "SessionConfirmationRecorder",
    "build_memory_update_draft",
    "format_resolution_text",
]
