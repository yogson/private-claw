"""Tests for the /wtf (session recap) Telegram command dispatch (issue #78)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from structlog.testing import capture_logs

from assistant.api.orchestrator_handler import (
    RECAP_DISABLED_TEXT,
    RECAP_EMPTY_TEXT,
    RECAP_FAILED_TEXT,
    _build_orchestrator_handler,
)
from assistant.channels.telegram.commands import TelegramCommand, extract_supported_command
from assistant.channels.telegram.models import NormalizedEvent
from assistant.core.config.schemas import CompactionConfig, RecapConfig
from assistant.core.events.models import EventSource, EventType
from assistant.core.orchestrator.service import Orchestrator
from assistant.store.filesystem.lock import FilesystemLockCoordinator
from assistant.store.filesystem.session import FilesystemSessionStore
from assistant.store.interfaces import LockAcquisitionError
from assistant.store.models import SessionRecord, SessionRecordType

SESSION_ID = "tg:123"


def _event(text: str = "/wtf") -> NormalizedEvent:
    return NormalizedEvent(
        event_id="ev-wtf",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=SESSION_ID,
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-wtf",
        text=text,
        metadata={"chat_id": 123},
    )


def _adapter() -> MagicMock:
    """Mock adapter whose /wtf detection uses the real command parser."""
    adapter = MagicMock()
    adapter.is_stop_request.return_value = False
    adapter.is_verbose_request.return_value = False
    adapter.is_session_new_request.return_value = False
    adapter.is_session_reset_request.return_value = False
    adapter.is_compact_request.return_value = False
    adapter.is_wtf_request.side_effect = lambda event: (
        extract_supported_command(event.text) == TelegramCommand.WTF
    )
    return adapter


def _mock_orchestrator(enabled: bool = True) -> MagicMock:
    orchestrator = MagicMock()
    orchestrator.recap_enabled = enabled
    orchestrator.execute_turn = AsyncMock()
    orchestrator.compact_session = AsyncMock()
    return orchestrator


@pytest.mark.parametrize(
    ("enabled", "outcome", "expected"),
    [
        (True, "🎯 Adding /wtf\n✅ Done:\n• enum", "🎯 Adding /wtf\n✅ Done:\n• enum"),
        (True, None, RECAP_EMPTY_TEXT),
        (True, "", RECAP_FAILED_TEXT),
        (False, None, RECAP_DISABLED_TEXT),
    ],
    ids=["recap", "empty-session", "empty-output", "disabled"],
)
async def test_wtf_replies_without_running_a_turn(
    enabled: bool, outcome: str | None, expected: str
) -> None:
    orchestrator = _mock_orchestrator(enabled)
    orchestrator.recap_session = AsyncMock(return_value=outcome)
    handler = _build_orchestrator_handler(_adapter(), orchestrator, None)

    response = await handler(_event())

    assert response is not None
    assert response.text == expected
    assert response.parse_mode is None
    orchestrator.execute_turn.assert_not_awaited()
    orchestrator.compact_session.assert_not_awaited()
    if enabled:
        orchestrator.recap_session.assert_awaited_once_with(SESSION_ID, "trace-wtf")
    else:
        orchestrator.recap_session.assert_not_awaited()


async def test_wtf_llm_failure_replies_briefly_and_logs_trace_id() -> None:
    orchestrator = _mock_orchestrator()
    orchestrator.recap_session = AsyncMock(side_effect=RuntimeError("boom: secret traceback"))
    handler = _build_orchestrator_handler(_adapter(), orchestrator, None)

    with capture_logs() as logs:
        response = await handler(_event("/wtf@my_bot"))

    assert response is not None
    assert response.text == RECAP_FAILED_TEXT
    assert "boom" not in response.text
    failed = [entry for entry in logs if entry["event"] == "recap.failed"]
    assert failed and failed[0]["trace_id"] == "trace-wtf"


# ---------------------------------------------------------------------------
# During an active turn: real Orchestrator over filesystem sessions and locks
# ---------------------------------------------------------------------------


def _record(sequence: int, record_type: SessionRecordType, content: str) -> SessionRecord:
    return SessionRecord(
        session_id=SESSION_ID,
        sequence=sequence,
        event_id=f"evt-{sequence}",
        turn_id="t1",
        timestamp=datetime.now(UTC),
        record_type=record_type,
        payload={"message_id": f"m-{sequence}", "content": content},
    )


async def test_wtf_answers_during_active_turn_without_lock_or_persisting(
    tmp_path: Path,
) -> None:
    sessions = FilesystemSessionStore(tmp_path / "sessions")
    locks = FilesystemLockCoordinator(tmp_path / "locks")
    await sessions.append(
        [
            _record(0, SessionRecordType.USER_MESSAGE, "please add /wtf"),
            _record(1, SessionRecordType.ASSISTANT_MESSAGE, "added the enum value"),
        ]
    )
    store = MagicMock()
    store.sessions = sessions
    store.locks = locks
    runtime_config = MagicMock()
    runtime_config.compaction = CompactionConfig(enabled=False)
    runtime_config.recap = RecapConfig()
    session_factory = MagicMock()
    session_factory.resume = AsyncMock(side_effect=LockAcquisitionError("busy"))
    orchestrator = Orchestrator(
        store=store,
        config=runtime_config,
        idempotency=MagicMock(),
        session_factory=session_factory,
    )
    records_before = await sessions.read_session(SESSION_ID)

    # An in-flight agent turn: holds the session turn lock until released.
    lock_held = asyncio.Event()
    release_turn = asyncio.Event()

    async def _running_turn() -> str:
        owner = "session_context:running-turn"
        assert await locks.acquire(f"session:{SESSION_ID}", owner) is not None
        lock_held.set()
        await release_turn.wait()
        await locks.release(f"session:{SESSION_ID}", owner)
        return "turn finished"

    turn = asyncio.create_task(_running_turn())
    await lock_held.wait()

    prompts: list[str] = []

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(str(messages))
        return ModelResponse(parts=[TextPart("🎯 Adding /wtf")])

    handler = _build_orchestrator_handler(_adapter(), orchestrator, None)
    recap_agent = orchestrator._recap_service._agent  # type: ignore[union-attr]  # noqa: SLF001
    with recap_agent.override(model=FunctionModel(_respond)):
        response = await asyncio.wait_for(handler(_event()), timeout=5)

    assert response is not None
    assert response.text == "🎯 Adding /wtf"
    assert prompts and "please add /wtf" in prompts[0]
    session_factory.resume.assert_not_awaited()
    # The running turn is untouched and still in flight.
    assert not turn.done()
    release_turn.set()
    assert await turn == "turn finished"
    # The recap was not persisted to session history.
    assert await sessions.read_session(SESSION_ID) == records_before
