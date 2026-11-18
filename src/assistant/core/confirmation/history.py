"""
Component ID: CMP_CORE_CONFIRMATION_SERVICE

Writes confirmation resolutions into session history so the next agent turn sees them.

Each resolution is persisted as its own complete synthetic turn (USER_MESSAGE +
TURN_TERMINAL) with turn_id ``confirmation-<id>``: replay keeps complete turns
only, records_to_messages renders user messages verbatim, and compaction
summarizes or keeps it like any other turn.
"""

import json
from datetime import UTC, datetime
from typing import Protocol

from assistant.core.confirmation.models import ConfirmationRequest
from assistant.store.interfaces import SessionStoreInterface
from assistant.store.models import (
    SessionRecord,
    SessionRecordType,
    TurnTerminalStatus,
    UserMessagePayload,
)

CONFIRMATION_RESOLVED_MARKER = "[[CONFIRMATION_RESOLVED]]"
CONFIRMATION_TURN_PREFIX = "confirmation-"
# What the marker becomes in user-typed text, so a user cannot forge a resolution.
_NEUTRALIZED_MARKER = "[[user-typed: CONFIRMATION_RESOLVED]]"


def is_confirmation_record(record: SessionRecord) -> bool:
    """True for the synthetic resolution records written by SessionConfirmationRecorder."""
    return record.turn_id.startswith(CONFIRMATION_TURN_PREFIX) and record.event_id == record.turn_id


def neutralize_confirmation_marker(text: str) -> str:
    """Defuse the resolution marker in text that did not come from the recorder."""
    return text.replace(CONFIRMATION_RESOLVED_MARKER, _NEUTRALIZED_MARKER)


class ConfirmationRecorderInterface(Protocol):
    """Persists a resolved confirmation where the agent can see it."""

    async def record_resolution(self, request: ConfirmationRequest) -> None:
        """Persist the final state of a resolved/expired/failed request."""
        ...


def format_resolution_text(request: ConfirmationRequest) -> str:
    """Render the history line for a resolved request."""
    payload = {
        "confirmation_id": request.confirmation_id,
        "kind": request.kind,
        "status": request.status.value,
        "summary": request.summary_for_user,
        "detail": request.detail,
        "origin_tool_call_id": request.origin_tool_call_id,
    }
    return f"{CONFIRMATION_RESOLVED_MARKER} " + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


class SessionConfirmationRecorder:
    """Appends resolution records to the session log."""

    def __init__(self, sessions: SessionStoreInterface) -> None:
        self._sessions = sessions

    async def record_resolution(self, request: ConfirmationRequest) -> None:
        """Append a synthetic complete turn carrying the resolution record."""
        now = request.resolved_at or datetime.now(UTC)
        turn_id = f"{CONFIRMATION_TURN_PREFIX}{request.confirmation_id}"
        user_payload = UserMessagePayload(
            message_id=f"msg-{turn_id}",
            content=format_resolution_text(request),
            source_event_id=turn_id,
        )
        records = [
            SessionRecord(
                session_id=request.session_id,
                sequence=0,
                event_id=turn_id,
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.USER_MESSAGE,
                payload=user_payload.model_dump(),
            ),
            SessionRecord(
                session_id=request.session_id,
                sequence=0,
                event_id=f"terminal-{turn_id}",
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.TURN_TERMINAL,
                payload={"status": TurnTerminalStatus.COMPLETED.value},
            ),
        ]
        await self._sessions.append(records)
