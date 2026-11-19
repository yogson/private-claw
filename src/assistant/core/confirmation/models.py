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

from pydantic import BaseModel, Field, field_validator

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


# Bounds of ConfirmationAttachment content, in UTF-16 units. A channel renders each part
# as one message headed by the title, ``part i/N`` and the confirmation id; with these
# bounds header + part fit one Telegram message (4096).
ATTACHMENT_TITLE_MAX_UTF16 = 120
ATTACHMENT_PART_MAX_UTF16 = 3700


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


class ConfirmationAttachment(BaseModel):
    """Content the user must receive IN FULL before deciding (e.g. a full prompt diff).

    Produced by the kind's core code, already split into ``parts`` (in order) of at most
    ATTACHMENT_PART_MAX_UTF16 units each; never truncated. A channel delivers every part
    as its own plain-text message before the buttons, and must not show usable buttons
    unless every part was delivered (see docs/assistant_v1/domains/agent.md).
    """

    title: str
    parts: list[str] = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def _title_fits(cls, v: str) -> str:
        if not v.strip() or "\n" in v or _utf16_len(v) > ATTACHMENT_TITLE_MAX_UTF16:
            raise ValueError(
                f"title must be one non-empty line of at most {ATTACHMENT_TITLE_MAX_UTF16} "
                "UTF-16 units"
            )
        return v

    @field_validator("parts")
    @classmethod
    def _parts_fit(cls, v: list[str]) -> list[str]:
        for part in v:
            if not part.strip() or _utf16_len(part) > ATTACHMENT_PART_MAX_UTF16:
                raise ValueError(
                    f"every part must be non-empty and at most {ATTACHMENT_PART_MAX_UTF16} "
                    "UTF-16 units (split the content, never truncate it)"
                )
        return v


class ConfirmationDraft(BaseModel):
    """Kind-specific proposal a tool hands to the confirmation service."""

    kind: str
    payload: dict[str, Any]
    summary_for_user: str
    dedupe_key: str
    origin_tool_call_id: str = ""
    # Name of the tool creating the draft (``ctx.tool_name``). The orchestrator only
    # accepts drafts from tools in CONFIRMATION_TOOL_NAMES.
    origin_tool_name: str = ""
    # Delivered in full before the buttons (empty for most kinds, e.g. memory_update).
    attachments: list[ConfirmationAttachment] = Field(default_factory=list)


class ConfirmationRequest(BaseModel):
    """A pending or resolved user confirmation (held in memory only)."""

    confirmation_id: str
    kind: str
    session_id: str
    origin_tool_call_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    summary_for_user: str
    dedupe_key: str
    # Never truncated (unlike summary_for_user); not written to the history record.
    attachments: list[ConfirmationAttachment] = Field(default_factory=list)
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
