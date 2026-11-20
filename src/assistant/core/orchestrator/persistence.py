"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Session record persistence helpers for orchestrator turn lifecycle.
"""

from datetime import UTC, datetime
from typing import Any

from assistant.core.config.schemas import RuntimeConfig
from assistant.store.interfaces import SessionStoreInterface
from assistant.store.models import (
    SessionRecord,
    SessionRecordType,
    TurnSummaryPayload,
    TurnTerminalStatus,
    UserMessagePayload,
)

# Assistant message content written for turns that failed before producing a reply.
FAILED_TURN_PLACEHOLDER = "[Request failed]"


async def persist_turn_initial(
    sessions: SessionStoreInterface,
    config: RuntimeConfig,
    *,
    session_id: str,
    turn_id: str,
    user_text: str,
    assistant_records: list[SessionRecord],
    attachments: list[dict[str, Any]] | None = None,
    prompt_trace: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> None:
    """Persist initial turn records including full conversation context for replay/restore.

    assistant_records must contain ASSISTANT_MESSAGE, ASSISTANT_TOOL_CALL, and
    TOOL_RESULT records in chronological order.
    """
    now = datetime.now(UTC)
    user_msg_id = f"msg-{turn_id}-user"
    user_payload = UserMessagePayload(
        message_id=user_msg_id,
        content=user_text,
        attachments=attachments or [],
        source_event_id=turn_id,
        user_id=user_id,
    )
    records: list[SessionRecord] = [
        SessionRecord(
            session_id=session_id,
            sequence=0,
            event_id=turn_id,
            turn_id=turn_id,
            timestamp=now,
            record_type=SessionRecordType.USER_MESSAGE,
            payload=user_payload.model_dump(),
        ),
    ]
    records.extend(assistant_records)

    capability_audit: dict[str, Any] = {}
    if prompt_trace is not None:
        capability_audit["prompt_trace"] = prompt_trace

    if capability_audit:
        summary_payload = TurnSummaryPayload(
            summary_text="turn diagnostics",
            capability_audit=capability_audit,
        )
        records.append(
            SessionRecord(
                session_id=session_id,
                sequence=0,
                event_id=f"turn-summary-{turn_id}",
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.TURN_SUMMARY,
                payload=summary_payload.model_dump(),
            )
        )
    await sessions.append(records)


async def persist_turn_terminal(
    sessions: SessionStoreInterface,
    *,
    session_id: str,
    turn_id: str,
    terminal_status: TurnTerminalStatus = TurnTerminalStatus.COMPLETED,
    terminal_payload: dict[str, Any] | None = None,
) -> None:
    """Persist the terminal turn record that marks the turn complete for replay.

    TurnTerminalStatus.SUSPENDED is reserved for future use (e.g. pending user
    input); v1 turns with pending_ask are persisted as COMPLETED.
    """
    payload = terminal_payload or {"status": terminal_status.value}
    if "status" not in payload:
        payload["status"] = terminal_status.value
    record = SessionRecord(
        session_id=session_id,
        sequence=await sessions.get_next_sequence(session_id),
        event_id=f"terminal-{turn_id}",
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        record_type=SessionRecordType.TURN_TERMINAL,
        payload=payload,
    )
    await sessions.append([record])


async def persist_turn_failed(
    sessions: SessionStoreInterface,
    config: RuntimeConfig,
    *,
    session_id: str,
    turn_id: str,
    user_text: str,
    user_id: str | None = None,
    status: TurnTerminalStatus = TurnTerminalStatus.FAILED,
    assistant_content: str | None = None,
) -> None:
    """Persist minimal turn records when execution fails before normal completion.

    Writes user message, placeholder assistant message, and TURN_TERMINAL with the
    given status so the turn is complete and replay invariants hold.
    """
    now = datetime.now(UTC)
    next_seq = await sessions.get_next_sequence(session_id)
    user_msg_id = f"msg-{turn_id}-user"
    assistant_msg_id = f"msg-{turn_id}-assistant"
    user_payload = UserMessagePayload(
        message_id=user_msg_id,
        content=user_text,
        attachments=[],
        source_event_id=turn_id,
        user_id=user_id,
    )
    content = assistant_content if assistant_content is not None else FAILED_TURN_PLACEHOLDER
    records: list[SessionRecord] = [
        SessionRecord(
            session_id=session_id,
            sequence=next_seq,
            event_id=turn_id,
            turn_id=turn_id,
            timestamp=now,
            record_type=SessionRecordType.USER_MESSAGE,
            payload=user_payload.model_dump(),
        ),
        SessionRecord(
            session_id=session_id,
            sequence=next_seq + 1,
            event_id=assistant_msg_id,
            turn_id=turn_id,
            timestamp=now,
            record_type=SessionRecordType.ASSISTANT_MESSAGE,
            payload={
                "message_id": assistant_msg_id,
                "content": content,
                "model_id": config.model.default_model_id,
            },
        ),
        SessionRecord(
            session_id=session_id,
            sequence=next_seq + 2,
            event_id=f"terminal-{turn_id}",
            turn_id=turn_id,
            timestamp=now,
            record_type=SessionRecordType.TURN_TERMINAL,
            payload={"status": status.value},
        ),
    ]
    await sessions.append(records)


async def persist_turn_terminal_failed(
    sessions: SessionStoreInterface,
    *,
    session_id: str,
    turn_id: str,
    status: TurnTerminalStatus = TurnTerminalStatus.FAILED,
) -> None:
    """Persist TURN_TERMINAL with failed status when turn fails after persist_turn_initial."""
    now = datetime.now(UTC)
    next_seq = await sessions.get_next_sequence(session_id)
    record = SessionRecord(
        session_id=session_id,
        sequence=next_seq,
        event_id=f"terminal-{turn_id}",
        turn_id=turn_id,
        timestamp=now,
        record_type=SessionRecordType.TURN_TERMINAL,
        payload={"status": status.value},
    )
    await sessions.append([record])
