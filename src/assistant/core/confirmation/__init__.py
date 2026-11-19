"""
Component ID: CMP_CORE_CONFIRMATION_SERVICE

Generic user-confirmation mechanism; kinds plug in through ConfirmationHandlerRegistry
(e.g. ``memory_update`` in assistant.extensions.first_party.memory.confirmation).
"""

from assistant.core.confirmation.capability_edit import (
    CAPABILITY_EDIT_KIND,
    CapabilityEditHandler,
    build_capability_edit_draft,
)
from assistant.core.confirmation.handlers import (
    ConfirmationHandlerInterface,
    ConfirmationHandlerRegistry,
)
from assistant.core.confirmation.history import (
    CONFIRMATION_RESOLVED_MARKER,
    CONFIRMATION_TOOL_NAMES,
    ConfirmationRecorderInterface,
    SessionConfirmationRecorder,
    format_resolution_text,
    is_confirmation_record,
    neutralize_confirmation_marker,
    unresolved_pending_ids,
)
from assistant.core.confirmation.models import (
    ATTACHMENT_PART_MAX_UTF16,
    ATTACHMENT_TITLE_MAX_UTF16,
    ConfirmationAttachment,
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
    "ATTACHMENT_PART_MAX_UTF16",
    "ATTACHMENT_TITLE_MAX_UTF16",
    "CAPABILITY_EDIT_KIND",
    "CONFIRMATION_RESOLVED_MARKER",
    "CONFIRMATION_TOOL_NAMES",
    "DEFAULT_CONFIRMATION_TTL_SECONDS",
    "NOT_DELIVERED_OUTCOME_STATUS",
    "CapabilityEditHandler",
    "ConfirmationAttachment",
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
    "SessionConfirmationRecorder",
    "build_capability_edit_draft",
    "format_resolution_text",
    "is_confirmation_record",
    "neutralize_confirmation_marker",
    "unresolved_pending_ids",
]
