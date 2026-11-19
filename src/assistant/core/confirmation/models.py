"""
Component ID: CMP_CORE_CONFIRMATION_SERVICE

Data models for generic user confirmations (see docs/assistant_v1/domains/agent.md,
"User confirmations").
"""

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeGuard

from pydantic import BaseModel, Field

# Every id ConfirmationService issues: 16 lowercase hex characters.
_CONFIRMATION_ID_RE = re.compile(r"[0-9a-f]{16}")


def new_confirmation_id() -> str:
    """A fresh confirmation id (16 lowercase hex characters)."""
    return uuid.uuid4().hex[:16]


def is_confirmation_id(value: object) -> TypeGuard[str]:
    """True if ``value`` has the exact format of an id issued by new_confirmation_id()."""
    return isinstance(value, str) and _CONFIRMATION_ID_RE.fullmatch(value) is not None


class ConfirmationStatus(StrEnum):
    """Lifecycle status of a confirmation request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


class ConfirmationDecision(StrEnum):
    """User decision delivered by a channel (button press only)."""

    APPROVE = "approve"
    REJECT = "reject"


class ConfirmationUiRef(BaseModel):
    """Channel message that carries the confirmation buttons."""

    chat_id: int
    message_id: int


class ConfirmationOutcome(BaseModel):
    """Result of applying an approved confirmation via its per-kind handler."""

    ok: bool
    status: str
    detail: str = ""


class ConfirmationDraft(BaseModel):
    """Kind-specific proposal a tool hands to the confirmation service."""

    kind: str
    payload: dict[str, Any]
    summary_for_user: str
    dedupe_key: str
    origin_tool_call_id: str = ""


class ConfirmationRequest(BaseModel):
    """A pending or resolved user confirmation (held in memory only)."""

    confirmation_id: str
    kind: str
    session_id: str
    origin_tool_call_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    summary_for_user: str
    dedupe_key: str
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None = None
    result: ConfirmationOutcome | None = None
    ui_ref: ConfirmationUiRef | None = None

    @property
    def is_pending(self) -> bool:
        """True while the request still awaits a user decision."""
        return self.status == ConfirmationStatus.PENDING

    @property
    def detail(self) -> str:
        """Human-readable outcome detail ('' while pending)."""
        return self.result.detail if self.result is not None else ""


class ConfirmationRequestResult(BaseModel):
    """Outcome of ConfirmationService.request().

    ``request`` is the new or deduplicated pending request; ``None`` when the
    session already has a different pending confirmation (``blocked_by``).
    """

    request: ConfirmationRequest | None = None
    created: bool = False
    blocked_by: ConfirmationRequest | None = None


class ConfirmationResolveResult(BaseModel):
    """Outcome of ConfirmationService.resolve().

    ``request`` is ``None`` for unknown ids (e.g. after a restart). ``changed`` is
    False when the request was already resolved or expired before this call.
    """

    request: ConfirmationRequest | None = None
    changed: bool = False
