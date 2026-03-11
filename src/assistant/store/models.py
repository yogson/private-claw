"""
Component ID: CMP_STORE_STATE_FACADE

Pydantic models for store domain data structures.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SessionRecordType(StrEnum):
    """Types of records that can be stored in a session log."""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    ASSISTANT_TOOL_CALL = "assistant_tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM_MESSAGE = "system_message"
    TURN_SUMMARY = "turn_summary"
    TURN_TERMINAL = "turn_terminal"


class TurnTerminalStatus(StrEnum):
    """Terminal status values for turn completion."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


class SystemMessageScope(StrEnum):
    """Scope for system messages."""

    SESSION = "session"
    TURN = "turn"


class UserMessagePayload(BaseModel):
    """Payload for user_message record type."""

    message_id: str
    content: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    source_event_id: str | None = None


class AssistantMessagePayload(BaseModel):
    """Payload for assistant_message record type."""

    message_id: str
    content: str
    model_id: str | None = None
    usage: dict[str, int] | None = None
    finish_reason: str | None = None


class AssistantToolCallPayload(BaseModel):
    """Payload for assistant_tool_call record type."""

    message_id: str
    tool_call_id: str
    tool_name: str
    arguments_json: str


class ToolResultPayload(BaseModel):
    """Payload for tool_result record type."""

    message_id: str
    tool_call_id: str
    tool_name: str
    result: Any | None = None
    error: str | None = None


class SystemMessagePayload(BaseModel):
    """Payload for system_message record type."""

    message_id: str
    content: str
    scope: SystemMessageScope


class TurnSummaryPayload(BaseModel):
    """Payload for turn_summary record type."""

    summary_text: str
    retrieval_audit: dict[str, Any] | None = None
    capability_audit: dict[str, Any] | None = None


class TurnTerminalPayload(BaseModel):
    """Payload for turn_terminal record type."""

    status: TurnTerminalStatus
    error_code: str | None = None
    error_message: str | None = None


SessionRecordPayload = (
    UserMessagePayload
    | AssistantMessagePayload
    | AssistantToolCallPayload
    | ToolResultPayload
    | SystemMessagePayload
    | TurnSummaryPayload
    | TurnTerminalPayload
)


class SessionRecord(BaseModel):
    """
    A single record in a session log (CMP_DATA_MODEL_SESSION_LOG).

    Stored as JSONL in runtime/sessions/<session_id>.jsonl
    """

    session_id: str
    sequence: int
    event_id: str
    turn_id: str
    timestamp: datetime
    record_type: SessionRecordType
    payload: dict[str, Any]


class IdempotencyRecord(BaseModel):
    """
    Idempotency record for duplicate event prevention (CMP_DATA_MODEL_IDEMPOTENCY_RECORD).

    Stored in runtime/idempotency/<key_hash>.json
    """

    key: str
    source: str
    created_at: datetime
    ttl_seconds: int


class LockRecord(BaseModel):
    """
    Lock record for session/task coordination (CMP_DATA_MODEL_LOCK_RECORD).

    Stored in runtime/locks/<lock_key>.lock
    """

    lock_key: str
    owner_id: str
    acquired_at: datetime
    expires_at: datetime


class RecoveryStatus(StrEnum):
    """Status of recovery scan."""

    HEALTHY = "healthy"
    RECOVERED = "recovered"
    DEGRADED = "degraded"
    FAILED = "failed"


class RecoveryMarker(BaseModel):
    """
    Recovery marker for startup consistency tracking (CMP_DATA_MODEL_STORE_RECOVERY_MARKER).

    Stored in runtime/recovery/<component>.json
    """

    component: str
    last_scan_at: datetime
    status: RecoveryStatus
    issues_found: int = 0
    issues_repaired: int = 0
    details: list[str] = Field(default_factory=list)


class TaskStatus(StrEnum):
    """Status of a sub-agent or scheduler task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class TaskRecord(BaseModel):
    """
    Task state record for sub-agent and scheduler tasks.

    Stored in runtime/tasks/<task_id>.json
    """

    task_id: str
    parent_session_id: str | None = None
    parent_turn_id: str | None = None
    task_type: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    ttl_seconds: int | None = None
    expires_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
