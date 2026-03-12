"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Canonical normalized inbound event model implementing INT_ORCH_EVENT_INPUT.
Consumed by the orchestrator from channel adapters and the scheduler.
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


class EventSource(StrEnum):
    TELEGRAM = "telegram"
    SCHEDULER = "scheduler"
    API = "api"
    SYSTEM = "system"


class SchedulerTriggerKind(StrEnum):
    REMINDER = "reminder"
    MAINTENANCE = "maintenance"
    MONITOR_CHECK = "monitor_check"


class VoiceMeta(BaseModel):
    """Voice message metadata."""

    file_id: str
    duration_seconds: int
    transcript_text: str | None = None
    transcript_confidence: float | None = None


class AttachmentMeta(BaseModel):
    """Attachment message metadata."""

    file_id: str
    mime_type: str
    file_size_bytes: int
    file_name: str | None = None
    caption: str | None = None


class CallbackQueryMeta(BaseModel):
    """
    Callback query metadata.

    Session-resume callbacks carry action='resume_session' and
    target_session_id in callback_data as a signed payload.
    """

    callback_id: str
    callback_data: str
    origin_message_id: int | None = None
    ui_version: str = "1"


class SchedulerMeta(BaseModel):
    """Scheduler-originated event metadata. Present only for scheduler_trigger events."""

    job_id: str
    trigger_kind: SchedulerTriggerKind
    scheduled_for: datetime
    attempt_number: int = 1


class OrchestratorEvent(BaseModel):
    """
    Canonical INT_ORCH_EVENT_INPUT contract.

    Produced by channel adapters and the scheduler, consumed by the orchestrator.
    All required fields must be present regardless of event type.
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

    scheduler: SchedulerMeta | None = None
