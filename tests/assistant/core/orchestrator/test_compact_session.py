"""Orchestrator-level tests for _compact_session and _should_compact_session."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from assistant.core.config.schemas import CompactionConfig
from assistant.core.orchestrator.service import Orchestrator
from assistant.store.models import (
    SessionRecord,
    SessionRecordType,
    SystemMessageScope,
    TurnTerminalStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rec(
    session_id: str = "s1",
    sequence: int = 0,
    turn_id: str = "t1",
    event_id: str | None = None,
    record_type: SessionRecordType = SessionRecordType.USER_MESSAGE,
    payload: dict | None = None,
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        sequence=sequence,
        event_id=event_id or f"evt-{sequence}",
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        record_type=record_type,
        payload=payload or {"message_id": f"m-{sequence}", "content": "test"},
    )


def _system(sequence: int, turn_id: str = "t0") -> SessionRecord:
    return _rec(
        sequence=sequence,
        turn_id=turn_id,
        record_type=SessionRecordType.SYSTEM_MESSAGE,
        payload={
            "message_id": f"sys-{sequence}",
            "content": "system prompt",
            "scope": SystemMessageScope.SESSION.value,
        },
    )


def _compaction_summary(
    sequence: int,
    content: str = "[Session Compacted - Pass 1]\n\nSummary of previous conversation.",
    turn_id: str = "compaction-2024-01-01T00:00:00",
) -> SessionRecord:
    return _rec(
        sequence=sequence,
        turn_id=turn_id,
        event_id=f"compaction-{sequence}",
        record_type=SessionRecordType.COMPACTION_SUMMARY,
        payload={
            "message_id": f"summary-{sequence}",
            "content": content,
            "scope": SystemMessageScope.SESSION.value,
        },
    )


def _assistant_with_usage(
    sequence: int,
    turn_id: str = "t1",
    input_tokens: int = 1000,
    output_tokens: int = 500,
) -> SessionRecord:
    return _rec(
        sequence=sequence,
        turn_id=turn_id,
        record_type=SessionRecordType.ASSISTANT_MESSAGE,
        payload={
            "message_id": f"m-{sequence}",
            "content": "response",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        },
    )


def _terminal(
    sequence: int,
    turn_id: str = "t1",
) -> SessionRecord:
    return _rec(
        sequence=sequence,
        turn_id=turn_id,
        record_type=SessionRecordType.TURN_TERMINAL,
        payload={"status": TurnTerminalStatus.COMPLETED.value},
    )


# ---------------------------------------------------------------------------
# Fixture: build a minimal Orchestrator with mocked dependencies
# ---------------------------------------------------------------------------


def _make_compaction_config(**overrides: object) -> CompactionConfig:
    defaults = {
        "enabled": True,
        "token_threshold": 10_000,
        "min_turns_before_compact": 2,
        "max_compactions": 3,
    }
    defaults.update(overrides)
    return CompactionConfig(**defaults)  # type: ignore[arg-type]


def _build_orchestrator(
    session_records: list[SessionRecord],
    compaction_config: CompactionConfig | None = None,
) -> Orchestrator:
    """Create an Orchestrator with mocked store & compaction service."""
    cfg = compaction_config or _make_compaction_config()

    runtime_config = MagicMock()
    runtime_config.compaction = cfg

    store = MagicMock()
    store.sessions = AsyncMock()
    store.sessions.read_session = AsyncMock(return_value=list(session_records))
    store.sessions.clear_session = AsyncMock(return_value=True)
    store.sessions.append = AsyncMock()

    idempotency = MagicMock()
    session_factory = MagicMock()

    mock_service = AsyncMock()
    mock_service.summarize = AsyncMock(return_value="Summary of conversation.")

    with patch(
        "assistant.core.orchestrator.service.ChatCompactionService",
        return_value=mock_service,
    ):
        orch = Orchestrator(
            store=store,
            config=runtime_config,
            idempotency=idempotency,
            session_factory=session_factory,
        )

    return orch


# ---------------------------------------------------------------------------
# _should_compact_session tests
# ---------------------------------------------------------------------------


class TestShouldCompactSession:
    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self) -> None:
        cfg = _make_compaction_config(enabled=False)
        orch = _build_orchestrator([], compaction_config=cfg)
        # Disabled means compaction_service is None
        orch._compaction_service = None  # noqa: SLF001
        result = await orch._should_compact_session("s1")  # noqa: SLF001
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_below_threshold(self) -> None:
        # 1500 total tokens < 10_000 threshold
        records = [
            _assistant_with_usage(0, turn_id="t1", input_tokens=500, output_tokens=500),
            _assistant_with_usage(1, turn_id="t2", input_tokens=200, output_tokens=300),
            _terminal(2, turn_id="t1"),
            _terminal(3, turn_id="t2"),
        ]
        orch = _build_orchestrator(records)
        result = await orch._should_compact_session("s1")  # noqa: SLF001
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_too_few_turns(self) -> None:
        # Tokens above threshold but only 1 turn (min_turns_before_compact=2)
        records = [
            _assistant_with_usage(0, turn_id="t1", input_tokens=8000, output_tokens=5000),
            _terminal(1, turn_id="t1"),
        ]
        orch = _build_orchestrator(records)
        result = await orch._should_compact_session("s1")  # noqa: SLF001
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_records_when_compaction_needed(self) -> None:
        records = [
            _assistant_with_usage(0, turn_id="t1", input_tokens=5000, output_tokens=3000),
            _terminal(1, turn_id="t1"),
            _assistant_with_usage(2, turn_id="t2", input_tokens=5000, output_tokens=3000),
            _terminal(3, turn_id="t2"),
        ]
        orch = _build_orchestrator(records)
        result = await orch._should_compact_session("s1")  # noqa: SLF001
        assert result is not None
        assert len(result) == 4


# ---------------------------------------------------------------------------
# _compact_session tests
# ---------------------------------------------------------------------------


class TestCompactSession:
    @pytest.mark.asyncio
    async def test_max_compactions_enforcement(self) -> None:
        """When max_compactions is reached, _compact_session returns early without clearing."""
        cfg = _make_compaction_config(max_compactions=2)
        records = [
            _system(0),
            _compaction_summary(1, content="Pass 1", turn_id="compaction-1"),
            _compaction_summary(2, content="Pass 2", turn_id="compaction-2"),
            _rec(sequence=3, turn_id="t1"),
            _terminal(4, turn_id="t1"),
        ]
        orch = _build_orchestrator(records, compaction_config=cfg)

        with patch("assistant.core.orchestrator.service.logger") as mock_logger:
            await orch._compact_session("s1", "trace-1", "user-1", records=records)  # noqa: SLF001

            # Should have logged warning about limit
            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            assert "compaction_limit_reached" in call_args[0][0]

        # Session must NOT have been cleared
        orch._store.sessions.clear_session.assert_not_called()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_happy_path_preserves_system_and_summaries(self) -> None:
        """After compaction: system records preserved, previous summaries preserved,
        new summary appended, regular chat records cleared."""
        records = [
            _system(0),
            _compaction_summary(1, content="Pass 1", turn_id="compaction-1"),
            _rec(sequence=2, turn_id="t1", record_type=SessionRecordType.USER_MESSAGE),
            _assistant_with_usage(3, turn_id="t1"),
            _terminal(4, turn_id="t1"),
        ]
        orch = _build_orchestrator(records)

        await orch._compact_session("s1", "trace-1", "user-1", records=records)  # noqa: SLF001

        # Session should have been cleared
        orch._store.sessions.clear_session.assert_awaited_once_with("s1")  # noqa: SLF001

        # Verify the records restored via append
        append_call = orch._store.sessions.append  # noqa: SLF001
        append_call.assert_awaited_once()
        restored = append_call.call_args[0][0]

        restored_types = [r.record_type for r in restored]

        # All SYSTEM_MESSAGE records preserved
        system_count = restored_types.count(SessionRecordType.SYSTEM_MESSAGE)
        assert system_count == 1

        # All previous COMPACTION_SUMMARY records preserved
        summary_records = [
            r for r in restored if r.record_type == SessionRecordType.COMPACTION_SUMMARY
        ]
        # 1 previous + 1 new = 2 total
        assert len(summary_records) == 2

        # The old summary content is preserved
        assert summary_records[0].payload["content"] == "Pass 1"

        # New summary is appended
        assert "[Session Compacted - Pass 2]" in summary_records[1].payload["content"]

        # Regular chat records (USER_MESSAGE, ASSISTANT_MESSAGE, TURN_TERMINAL)
        # are NOT in the restored set
        assert SessionRecordType.USER_MESSAGE not in restored_types
        assert SessionRecordType.ASSISTANT_MESSAGE not in restored_types
        assert SessionRecordType.TURN_TERMINAL not in restored_types

    @pytest.mark.asyncio
    async def test_first_compaction_no_previous_summaries(self) -> None:
        """First compaction with no prior summaries creates exactly one new summary."""
        records = [
            _system(0),
            _rec(sequence=1, turn_id="t1"),
            _assistant_with_usage(2, turn_id="t1"),
            _terminal(3, turn_id="t1"),
        ]
        orch = _build_orchestrator(records)

        await orch._compact_session("s1", "trace-1", "user-1", records=records)  # noqa: SLF001

        orch._store.sessions.clear_session.assert_awaited_once()  # noqa: SLF001
        restored = orch._store.sessions.append.call_args[0][0]  # noqa: SLF001

        # system + 1 new summary
        assert len(restored) == 2
        assert restored[0].record_type == SessionRecordType.SYSTEM_MESSAGE
        assert restored[1].record_type == SessionRecordType.COMPACTION_SUMMARY
        assert "[Session Compacted - Pass 1]" in restored[1].payload["content"]

    @pytest.mark.asyncio
    async def test_returns_early_when_no_service(self) -> None:
        """_compact_session is a no-op when compaction service is None."""
        records = [_rec(sequence=0)]
        orch = _build_orchestrator(records)
        orch._compaction_service = None  # noqa: SLF001

        await orch._compact_session("s1", "trace-1", "user-1")  # noqa: SLF001

        orch._store.sessions.clear_session.assert_not_called()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_loads_records_when_not_passed(self) -> None:
        """When records kwarg is None, _compact_session loads from store."""
        records = [
            _system(0),
            _rec(sequence=1, turn_id="t1"),
            _terminal(2, turn_id="t1"),
        ]
        orch = _build_orchestrator(records)

        await orch._compact_session("s1", "trace-1", None)  # noqa: SLF001

        orch._store.sessions.read_session.assert_awaited_once_with("s1")  # noqa: SLF001
        orch._store.sessions.clear_session.assert_awaited_once()  # noqa: SLF001
