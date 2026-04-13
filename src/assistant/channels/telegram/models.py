"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Channel-level normalized event and channel response Pydantic models.
Sub-models for INT_ORCH_EVENT_INPUT are owned by core.events.models and
re-exported here for backward compatibility.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from assistant.core.events.models import (
    AttachmentMeta,
    CallbackQueryMeta,
    EventSource,
    EventType,
    VoiceMeta,
)

__all__ = [
    "AttachmentMeta",
    "CallbackQueryMeta",
    "EventSource",
    "EventType",
    "VoiceMeta",
    "MessageType",
    "ActionButton",
    "ChannelResponse",
    "NormalizedEvent",
]


class MessageType(StrEnum):
    TEXT = "text"
    INTERACTIVE = "interactive"


class NormalizedEvent(BaseModel):
    """
    Normalized inbound event (INT_ORCH_EVENT_INPUT).

    Produced by channel adapters and consumed by the orchestrator.
    """

    event_id: str
    event_type: EventType
    source: EventSource
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
    web_app_url: str | None = None


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
