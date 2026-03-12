"""Tests for orchestrator memory confirmation lifecycle service."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from assistant.core.orchestrator.confirmation import MemoryConfirmationService
from assistant.memory.write.service import MemoryWriteService
from assistant.store.filesystem.session import FilesystemSessionStore
from assistant.store.models import (
    AssistantToolCallPayload,
    SessionRecord,
    SessionRecordType,
    ToolResultPayload,
)


@pytest.mark.asyncio
async def test_list_and_reject_pending_confirmation(tmp_path: Path) -> None:
    session_store = FilesystemSessionStore(tmp_path / "sessions")
    writer = MemoryWriteService(tmp_path)
    service = MemoryConfirmationService(session_store, writer)
    session_id = "tg:123"
    turn_id = "turn-1"
    tool_call_id = "memory-proposal-turn-1-0"
    now = datetime.now(UTC)

    call_payload = AssistantToolCallPayload(
        message_id="msg-1",
        tool_call_id=tool_call_id,
        tool_name="memory_propose_update",
        arguments_json='{"intent_id":"i1","action":"upsert","memory_type":"preferences","reason":"x","source":"agent_inferred","requires_user_confirmation":true,"candidate":{"body_markdown":"abc","priority":5,"confidence":0.9,"tags":[],"entities":[]}}',
    )
    pending_payload = ToolResultPayload(
        message_id="msg-2",
        tool_call_id=tool_call_id,
        tool_name="memory_propose_update",
        result={"status": "pending_confirmation", "reason": "", "requires_user_confirmation": True},
        error=None,
    )
    await session_store.append(
        [
            SessionRecord(
                session_id=session_id,
                sequence=0,
                event_id="call-1",
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
                payload=call_payload.model_dump(),
            ),
            SessionRecord(
                session_id=session_id,
                sequence=1,
                event_id="result-1",
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.TOOL_RESULT,
                payload=pending_payload.model_dump(),
            ),
        ]
    )

    pending = await service.list_pending(session_id)
    assert len(pending) == 1
    ok, msg = await service.resolve_pending(session_id, tool_call_id, approve=False)
    assert ok is True
    assert "rejected" in msg.lower()
    assert await service.list_pending(session_id) == []


@pytest.mark.asyncio
async def test_apply_failure_persists_result_and_returns_user_safe_message(tmp_path: Path) -> None:
    """When apply_intent raises, persist failed outcome and return user-safe message."""
    session_store = FilesystemSessionStore(tmp_path / "sessions")
    mock_writer = MagicMock(spec=MemoryWriteService)
    mock_writer.apply_intent.side_effect = RuntimeError("Storage unavailable")
    service = MemoryConfirmationService(session_store, mock_writer)
    session_id = "tg:456"
    turn_id = "turn-2"
    tool_call_id = "call-xyz"
    now = datetime.now(UTC)

    call_payload = AssistantToolCallPayload(
        message_id="msg-1",
        tool_call_id=tool_call_id,
        tool_name="memory_propose_update",
        arguments_json='{"intent_id":"i2","action":"upsert","memory_type":"facts","reason":"y","source":"explicit_user_request","requires_user_confirmation":true,"candidate":{"body_markdown":"xyz","priority":5,"confidence":0.9,"tags":[],"entities":[]}}',
    )
    pending_payload = ToolResultPayload(
        message_id="msg-2",
        tool_call_id=tool_call_id,
        tool_name="memory_propose_update",
        result={"status": "pending_confirmation", "reason": "", "requires_user_confirmation": True},
        error=None,
    )
    await session_store.append(
        [
            SessionRecord(
                session_id=session_id,
                sequence=0,
                event_id="call-2",
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
                payload=call_payload.model_dump(),
            ),
            SessionRecord(
                session_id=session_id,
                sequence=1,
                event_id="result-2",
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.TOOL_RESULT,
                payload=pending_payload.model_dump(),
            ),
        ]
    )

    ok, msg = await service.resolve_pending(session_id, tool_call_id, approve=True)
    assert ok is True
    assert "could not be applied" in msg.lower() or "try again" in msg.lower()
    records = await session_store.read_session(session_id)
    tool_results = [r for r in records if r.record_type == SessionRecordType.TOOL_RESULT]
    assert len(tool_results) >= 2
    last_result = tool_results[-1]
    assert last_result.payload.get("result", {}).get("status") == "failed"
