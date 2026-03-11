"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Normalized event and channel response Pydantic models.
Implements INT_ORCH_EVENT_INPUT and INT_CHANNEL_RESPONSE contracts.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    USER_TEXT_MESSAGE = "user_text_message"
    USER_VOICE_MESSAGE = "user_voice_message"
    USER_ATTACHMENT_MESSAGE = "user_attachment_message"
    USER_CALLBACK_QUERY = "user_callback_query"
    SCHEDULER_TRIGGER = "scheduler_trigger"
    SYSTEM_CONTROL_EVENT = "system_control_event"


class MessageType(StrEnum):
    TEXT = "text"
    INTERACTIVE = "interactive"


class VoiceMeta(BaseModel):
    """Voice message metadata (INT_ORCH_EVENT_INPUT voice field)."""

    file_id: str
    duration_seconds: int
    transcript_text: str | None = None
    transcript_confidence: float | None = None


class AttachmentMeta(BaseModel):
    """Attachment message metadata (INT_ORCH_EVENT_INPUT attachment field)."""

    file_id: str
    mime_type: str
    file_size_bytes: int
    caption: str | None = None


class CallbackQueryMeta(BaseModel):
    """Callback query metadata (INT_ORCH_EVENT_INPUT callback_query field)."""

    callback_id: str
    callback_data: str
    origin_message_id: int | None = None
    ui_version: str = "1"


class NormalizedEvent(BaseModel):
    """
    Normalized inbound event (INT_ORCH_EVENT_INPUT).

    Produced by channel adapters and consumed by the orchestrator.
    """

    event_id: str
    event_type: EventType
    source: str
    session_id: str
    user_id: str
    created_at: datetime
    trace_id: str
    text: str | None = None
    voice: VoiceMeta | None = None
    attachment: AttachmentMeta | None = None
    callback_query: CallbackQueryMeta | None = None
    attachments: list[AttachmentMeta] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class ActionButton(BaseModel):
    """Interactive inline keyboard button definition."""

    label: str
    callback_id: str
    callback_data: str
    style: str | None = None


class ChannelResponse(BaseModel):
    """
    Outbound channel response (INT_CHANNEL_RESPONSE).

    Produced by the orchestrator and consumed by channel adapters.
    """

    response_id: str
    channel: str
    session_id: str
    trace_id: str
    message_type: MessageType
    text: str
    parse_mode: str | None = None
    ui_kind: str | None = None
    actions: list[ActionButton] = Field(default_factory=list)
    ui_version: str = "1"
