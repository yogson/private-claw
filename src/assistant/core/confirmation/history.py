"""
Component ID: CMP_CORE_CONFIRMATION_SERVICE

Writes confirmation resolutions into session history so the next agent turn sees them.

Each resolution is persisted as its own complete synthetic turn (USER_MESSAGE +
TURN_TERMINAL) with turn_id ``confirmation-<id>``: replay keeps complete turns
only, records_to_messages renders user messages verbatim, and compaction
summarizes or keeps it like any other turn.
"""

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol

from assistant.core.confirmation.models import ConfirmationRequest, is_confirmation_id
from assistant.store.interfaces import SessionStoreInterface
from assistant.store.models import (
    SessionRecord,
    SessionRecordType,
    TurnTerminalStatus,
    UserMessagePayload,
)

CONFIRMATION_RESOLVED_MARKER = "[[CONFIRMATION_RESOLVED]]"
CONFIRMATION_TURN_PREFIX = "confirmation-"
# Tool result status that tells the agent a confirmation is awaiting the user.
PENDING_TOOL_RESULT_STATUS = "pending_confirmation"
# Registry of the first-party tools that create confirmations through
# ConfirmationService.request() and report them as ``pending_confirmation``. Only
# their results are trusted by unresolved_pending_ids(): any other tool's output
# (MCP servers, fetched JSON, external APIs) is untrusted and may use the same shape.
# Every new confirmation-creating tool (e.g. capability edits) must be added here.
CONFIRMATION_TOOL_NAMES: frozenset[str] = frozenset({"memory_propose_update"})
# What the marker becomes in user-typed text, so a user cannot forge a resolution.
_NEUTRALIZED_MARKER = "[[user-typed: CONFIRMATION_RESOLVED]]"


def is_confirmation_record(record: SessionRecord) -> bool:
    """True for the synthetic resolution records written by SessionConfirmationRecorder."""
    return record.turn_id.startswith(CONFIRMATION_TURN_PREFIX) and record.event_id == record.turn_id


def unresolved_pending_ids(records: Iterable[SessionRecord]) -> list[str]:
    """Confirmation ids a tool result shows as pending with no resolution record after it.

    Scans tool results of the tools in CONFIRMATION_TOOL_NAMES with
    ``status == "pending_confirmation"`` and a well-formed id (is_confirmation_id), and
    drops ids that already have a ``confirmation-<id>`` resolution record. Results of
    other tools and malformed ids are ignored: they did not come from
    ConfirmationService and must never become a trusted resolution record.
    """
    pending: dict[str, None] = {}
    resolved: set[str] = set()
    for record in records:
        if is_confirmation_record(record):
            resolved.add(record.turn_id.removeprefix(CONFIRMATION_TURN_PREFIX))
            continue
        if record.record_type != SessionRecordType.TOOL_RESULT:
            continue
        if record.payload.get("tool_name") not in CONFIRMATION_TOOL_NAMES:
            continue
        result = record.payload.get("result")
        if not isinstance(result, dict) or result.get("status") != PENDING_TOOL_RESULT_STATUS:
            continue
        confirmation_id = result.get("confirmation_id")
        if is_confirmation_id(confirmation_id):
            pending[confirmation_id] = None
    return [cid for cid in pending if cid not in resolved]


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
