"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Session record persistence helpers for orchestrator turn lifecycle.
"""

from datetime import UTC, datetime
from typing import Any

from assistant.core.config.schemas import RuntimeConfig
from assistant.core.orchestrator.memory import MemoryOutcome
from assistant.store.interfaces import SessionStoreInterface
from assistant.store.models import (
    SessionRecord,
    SessionRecordType,
    ToolResultPayload,
    TurnSummaryPayload,
    TurnTerminalStatus,
    UserMessagePayload,
)


async def persist_turn_initial(
    sessions: SessionStoreInterface,
    config: RuntimeConfig,
    *,
    session_id: str,
    turn_id: str,
    user_text: str,
    assistant_records: list[SessionRecord],
    attachments: list[dict[str, Any]] | None = None,
    invalid_memory_intents: int = 0,
    prompt_trace: dict[str, Any] | None = None,
) -> None:
    """Persist initial turn records including full conversation context for replay/restore.

    assistant_records must contain ASSISTANT_MESSAGE, ASSISTANT_TOOL_CALL, and
    TOOL_RESULT records (excluding memory_propose_update results, which are
    persisted separately in persist_turn_outcomes with applied outcomes).
    """
    now = datetime.now(UTC)
    user_msg_id = f"msg-{turn_id}-user"
    user_payload = UserMessagePayload(
        message_id=user_msg_id,
        content=user_text,
        attachments=attachments or [],
        source_event_id=turn_id,
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
    if invalid_memory_intents > 0:
        capability_audit["invalid_memory_intents"] = invalid_memory_intents
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


async def persist_turn_outcomes(
    sessions: SessionStoreInterface,
    *,
    session_id: str,
    turn_id: str,
    outcomes: list[MemoryOutcome],
    terminal_status: TurnTerminalStatus = TurnTerminalStatus.COMPLETED,
    terminal_payload: dict[str, Any] | None = None,
) -> None:
    """Persist final memory outcome records and terminal turn record.

    terminal_status and terminal_payload are used for memory-outcome turns.
    TurnTerminalStatus.SUSPENDED is reserved for future use (e.g. pending user
    input); v1 turns with pending_ask are persisted as COMPLETED via
    persist_turn_initial.
    """
    now = datetime.now(UTC)
    next_sequence = await sessions.get_next_sequence(session_id)
    records: list[SessionRecord] = []
    for tool_call_id, result, error in outcomes:
        result_payload = ToolResultPayload(
            message_id=f"msg-final-result-{tool_call_id}",
            tool_call_id=tool_call_id,
            tool_name="memory_propose_update",
            result=result,
            error=error,
        )
        records.append(
            SessionRecord(
                session_id=session_id,
                sequence=next_sequence,
                event_id=f"tool-result-final-{tool_call_id}",
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.TOOL_RESULT,
                payload=result_payload.model_dump(),
            )
        )
        next_sequence += 1
    payload = terminal_payload or {"status": terminal_status.value}
    if "status" not in payload:
        payload["status"] = terminal_status.value
    records.append(
        SessionRecord(
            session_id=session_id,
            sequence=next_sequence,
            event_id=f"terminal-{turn_id}",
            turn_id=turn_id,
            timestamp=now,
            record_type=SessionRecordType.TURN_TERMINAL,
            payload=payload,
        )
    )
    await sessions.append(records)


async def persist_turn_failed(
    sessions: SessionStoreInterface,
    config: RuntimeConfig,
    *,
    session_id: str,
    turn_id: str,
    user_text: str,
    status: TurnTerminalStatus = TurnTerminalStatus.FAILED,
) -> None:
    """Persist minimal turn records when execution fails before normal completion.

    Writes user message, placeholder assistant message, and TURN_TERMINAL with failed
    status so the turn is complete and replay invariants hold.
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
    )
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
                "content": "[Request failed]",
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
