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
    is_confirmation_record,
    neutralize_confirmation_marker,
    unresolved_pending_ids,
)
from assistant.core.confirmation.memory_update import (
    MEMORY_BODY_MAX_CHARS,
    MEMORY_UPDATE_KIND,
    MemoryUpdateHandler,
    build_memory_update_draft,
    memory_body_length_error,
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
    NOT_DELIVERED_OUTCOME_STATUS,
    ConfirmationService,
)

__all__ = [
    "CONFIRMATION_RESOLVED_MARKER",
    "DEFAULT_CONFIRMATION_TTL_SECONDS",
    "MEMORY_BODY_MAX_CHARS",
    "MEMORY_UPDATE_KIND",
    "NOT_DELIVERED_OUTCOME_STATUS",
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
    "is_confirmation_record",
    "memory_body_length_error",
    "neutralize_confirmation_marker",
    "unresolved_pending_ids",
]
