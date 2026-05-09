"""Tests for chat compaction feature (issue #43)."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.core.config.schemas import CompactionConfig
from assistant.core.orchestrator.compaction import SUMMARIZER_PROMPT, ChatCompactionService
from assistant.core.orchestrator.token_usage import calculate_session_total_tokens
from assistant.store.filesystem.replay import build_replay
from assistant.store.filesystem.session import FilesystemSessionStore
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


def _terminal(
    session_id: str = "s1",
    sequence: int = 99,
    turn_id: str = "t1",
    status: TurnTerminalStatus = TurnTerminalStatus.COMPLETED,
) -> SessionRecord:
    return _rec(
        session_id=session_id,
        sequence=sequence,
        turn_id=turn_id,
        record_type=SessionRecordType.TURN_TERMINAL,
        payload={"status": status.value},
    )


def _system(
    sequence: int,
    scope: SystemMessageScope = SystemMessageScope.SESSION,
    turn_id: str = "t0",
) -> SessionRecord:
    return _rec(
        sequence=sequence,
        turn_id=turn_id,
        record_type=SessionRecordType.SYSTEM_MESSAGE,
        payload={
            "message_id": f"sys-{sequence}",
            "content": "system prompt",
            "scope": scope.value,
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


# ---------------------------------------------------------------------------
# CompactionConfig tests
# ---------------------------------------------------------------------------


class TestCompactionConfig:
    def test_defaults(self) -> None:
        cfg = CompactionConfig()
        assert cfg.enabled is False
        assert cfg.token_threshold == 100_000
        assert cfg.summarizer_model_id == "claude-haiku-4-5"
        assert cfg.max_compactions == 3
        assert cfg.compaction_prompt == ""

    def test_custom_values(self) -> None:
        cfg = CompactionConfig(
            enabled=True,
            token_threshold=80_000,
            min_turns_before_compact=3,
        )
        assert cfg.enabled is True
        assert cfg.token_threshold == 80_000
        assert cfg.min_turns_before_compact == 3

    def test_token_threshold_minimum(self) -> None:
        with pytest.raises(ValueError):
            CompactionConfig(token_threshold=5_000)  # below minimum 10_000

    def test_custom_compaction_prompt(self) -> None:
        """compaction_prompt is stored as-is when provided."""
        custom = "Summarize only the key decisions made."
        cfg = CompactionConfig(compaction_prompt=custom)
        assert cfg.compaction_prompt == custom

    def test_empty_compaction_prompt_is_default(self) -> None:
        """Empty string is the default and signals use of built-in prompt."""
        cfg = CompactionConfig(compaction_prompt="")
        assert cfg.compaction_prompt == ""


# ---------------------------------------------------------------------------
# calculate_session_total_tokens tests
# ---------------------------------------------------------------------------


class TestCalculateSessionTotalTokens:
    @pytest.mark.asyncio
    async def test_empty_session(self) -> None:
        store = AsyncMock()
        store.read_session = AsyncMock(return_value=[])
        assert await calculate_session_total_tokens(store, "s1") == 0

    @pytest.mark.asyncio
    async def test_returns_last_record_input_tokens(self) -> None:
        """Returns input_tokens of the highest-sequence assistant message.

        input_tokens already reflects the full accumulated context, so we
        read the most recent value rather than summing across all records.
        """
        store = AsyncMock()
        store.read_session = AsyncMock(
            return_value=[
                _assistant_with_usage(0, input_tokens=1000, output_tokens=500),
                _assistant_with_usage(1, input_tokens=2000, output_tokens=800),
            ]
        )
        total = await calculate_session_total_tokens(store, "s1")
        assert total == 2000  # input_tokens of the last (highest-sequence) record

    @pytest.mark.asyncio
    async def test_ignores_non_assistant_records(self) -> None:
        store = AsyncMock()
        store.read_session = AsyncMock(
            return_value=[
                _rec(sequence=0),  # user_message — no usage
                _assistant_with_usage(1, input_tokens=500, output_tokens=200),
            ]
        )
        total = await calculate_session_total_tokens(store, "s1")
        assert total == 500  # only the assistant record's input_tokens

    @pytest.mark.asyncio
    async def test_handles_missing_usage(self) -> None:
        store = AsyncMock()
        store.read_session = AsyncMock(
            return_value=[
                _rec(
                    sequence=0,
                    record_type=SessionRecordType.ASSISTANT_MESSAGE,
                    payload={"message_id": "m", "content": "test"},
                ),
            ]
        )
        total = await calculate_session_total_tokens(store, "s1")
        assert total == 0


# ---------------------------------------------------------------------------
# build_replay with COMPACTION_SUMMARY tests
# ---------------------------------------------------------------------------


class TestBuildReplayWithCompaction:
    def test_compaction_summary_included_in_replay(self) -> None:
        """COMPACTION_SUMMARY records should appear in replay output."""
        records = [
            _compaction_summary(0),
            _rec(sequence=1, turn_id="t1"),
            _terminal(sequence=2, turn_id="t1"),
        ]
        result = build_replay(records, budget=20)
        types = [r.record_type for r in result]
        assert SessionRecordType.COMPACTION_SUMMARY in types

    def test_compaction_summary_appears_before_turns(self) -> None:
        """COMPACTION_SUMMARY should appear before regular turn records."""
        records = [
            _compaction_summary(0),
            _rec(sequence=1, turn_id="t1"),
            _terminal(sequence=2, turn_id="t1"),
        ]
        result = build_replay(records, budget=20)
        summary_idx = next(
            i for i, r in enumerate(result) if r.record_type == SessionRecordType.COMPACTION_SUMMARY
        )
        user_idx = next(
            i for i, r in enumerate(result) if r.record_type == SessionRecordType.USER_MESSAGE
        )
        assert summary_idx < user_idx

    def test_multiple_compaction_summaries_oldest_first(self) -> None:
        """Multiple COMPACTION_SUMMARY records appear oldest→newest."""
        records = [
            _compaction_summary(0, content="Pass 1"),
            _compaction_summary(1, content="Pass 2", turn_id="compaction-2"),
            _rec(sequence=2, turn_id="t1"),
            _terminal(sequence=3, turn_id="t1"),
        ]
        result = build_replay(records, budget=20)
        summaries = [r for r in result if r.record_type == SessionRecordType.COMPACTION_SUMMARY]
        assert len(summaries) == 2
        assert summaries[0].payload["content"] == "Pass 1"
        assert summaries[1].payload["content"] == "Pass 2"

    def test_system_message_before_compaction_summary(self) -> None:
        """System message appears first, then compaction summaries, then turns."""
        records = [
            _system(0),
            _compaction_summary(1),
            _rec(sequence=2, turn_id="t1"),
            _terminal(sequence=3, turn_id="t1"),
        ]
        result = build_replay(records, budget=20)
        assert result[0].record_type == SessionRecordType.SYSTEM_MESSAGE
        assert result[1].record_type == SessionRecordType.COMPACTION_SUMMARY

    def test_compaction_summary_counts_against_budget(self) -> None:
        """COMPACTION_SUMMARY records count against the budget."""
        records = [
            _compaction_summary(0),
            _rec(sequence=1, turn_id="t1"),
            _terminal(sequence=2, turn_id="t1"),
        ]
        # Budget=1: only room for compaction summary, not the turn
        result = build_replay(records, budget=1)
        assert len(result) == 1
        assert result[0].record_type == SessionRecordType.COMPACTION_SUMMARY


# ---------------------------------------------------------------------------
# records_to_messages with COMPACTION_SUMMARY
# ---------------------------------------------------------------------------


class TestRecordsToMessagesCompaction:
    def test_compaction_summary_becomes_user_message(self) -> None:
        from assistant.agent.interfaces import MessageRole
        from assistant.core.orchestrator.payloads import records_to_messages

        records = [
            _compaction_summary(0, content="[Session Compacted]\n\nSummary text."),
        ]
        msgs = records_to_messages(records)
        assert len(msgs) == 1
        assert msgs[0].role == MessageRole.USER
        assert "[Session Compacted]" in msgs[0].content


# ---------------------------------------------------------------------------
# ChatCompactionService tests
# ---------------------------------------------------------------------------


class TestChatCompactionService:
    def test_format_for_summary(self) -> None:
        from assistant.agent.interfaces import LLMMessage, MessageRole

        messages = [
            LLMMessage(role=MessageRole.USER, content="Hello"),
            LLMMessage(role=MessageRole.ASSISTANT, content="Hi there!"),
        ]
        formatted = ChatCompactionService._format_for_summary(messages)
        assert "USER: Hello" in formatted
        assert "ASSISTANT: Hi there!" in formatted

    def test_format_for_summary_with_tool_blocks(self) -> None:
        from assistant.agent.interfaces import LLMMessage, MessageRole

        messages = [
            LLMMessage(
                role=MessageRole.ASSISTANT,
                content="",
                content_blocks=[
                    {"type": "text", "text": "Let me check."},
                    {"type": "tool_use", "name": "memory_search", "id": "c1", "input": {}},
                ],
            ),
        ]
        formatted = ChatCompactionService._format_for_summary(messages)
        assert "Let me check." in formatted
        assert "[Tool call: memory_search]" in formatted

    def test_instantiates_with_default_prompt(self) -> None:
        """Service can be created without a custom prompt (uses built-in default)."""
        svc = ChatCompactionService(model_id="claude-haiku-4-5")
        assert svc is not None

    def test_instantiates_with_empty_prompt_string(self) -> None:
        """Explicit empty string falls back to built-in default prompt."""
        svc = ChatCompactionService(model_id="claude-haiku-4-5", prompt="")
        assert svc is not None

    def test_instantiates_with_custom_prompt(self) -> None:
        """Explicit non-empty prompt is accepted and the service is ready to use."""
        custom = "Only summarize the final decisions."
        svc = ChatCompactionService(model_id="claude-haiku-4-5", prompt=custom)
        assert svc is not None

    def test_whitespace_only_prompt_falls_back_to_default(self) -> None:
        """A prompt consisting solely of whitespace triggers fallback to built-in."""
        svc = ChatCompactionService(model_id="claude-haiku-4-5", prompt="   ")
        assert svc is not None

    def test_custom_prompt_propagated_to_agent(self) -> None:
        """Custom prompt is passed as system_prompt to the underlying pydantic-ai Agent."""
        custom_prompt = "Only summarize the key decisions made."
        svc = ChatCompactionService(model_id="claude-haiku-4-5", prompt=custom_prompt)
        assert svc._agent._system_prompts == (custom_prompt,)

    def test_empty_prompt_uses_default_summarizer_prompt(self) -> None:
        """Empty string prompt causes Agent to receive the built-in SUMMARIZER_PROMPT."""
        svc_empty = ChatCompactionService(model_id="claude-haiku-4-5", prompt="")
        assert svc_empty._agent._system_prompts == (SUMMARIZER_PROMPT,)

    def test_whitespace_prompt_uses_default_summarizer_prompt(self) -> None:
        """Whitespace-only prompt causes Agent to receive the built-in SUMMARIZER_PROMPT."""
        svc_ws = ChatCompactionService(model_id="claude-haiku-4-5", prompt="   ")
        assert svc_ws._agent._system_prompts == (SUMMARIZER_PROMPT,)


# ---------------------------------------------------------------------------
# Orchestrator._compact_session: graceful fallback on summarize error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_session_graceful_fallback_on_summarize_error() -> None:
    """_compact_session must not raise when summarize() fails; session stays unchanged."""
    from assistant.core.orchestrator.service import Orchestrator

    # Two real turns so min_turns_before_compact (=2) is satisfied.
    records = [
        _rec(sequence=0, turn_id="t1"),
        _terminal(sequence=1, turn_id="t1"),
        _rec(sequence=2, turn_id="t2"),
        _terminal(sequence=3, turn_id="t2"),
    ]

    store = MagicMock()
    store.sessions = AsyncMock()
    store.sessions.read_session = AsyncMock(return_value=records)
    store.sessions.replace_session = AsyncMock()

    compaction_cfg = CompactionConfig(enabled=True, min_turns_before_compact=2)

    # Build a bare Orchestrator instance bypassing __init__.
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._store = store
    orchestrator._compaction_config = compaction_cfg
    orchestrator._usage_service = None
    orchestrator._compaction_service = ChatCompactionService(model_id="claude-haiku-4-5")

    # Make summarize() raise so the fallback path is exercised.
    orchestrator._compaction_service.summarize = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("LLM unavailable")
    )

    # Must not raise.
    await orchestrator._compact_session("session-1", "trace-abc", None, records=records)

    # Session must NOT have been modified (replace_session never called).
    store.sessions.replace_session.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: FilesystemSessionStore with compaction records
# ---------------------------------------------------------------------------


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


@pytest.fixture
def session_store(sessions_dir: Path) -> FilesystemSessionStore:
    return FilesystemSessionStore(sessions_dir)


@pytest.mark.asyncio
async def test_compaction_summary_persisted_and_read(
    session_store: FilesystemSessionStore,
) -> None:
    """COMPACTION_SUMMARY records can be persisted and read back."""
    summary = _compaction_summary(0)
    await session_store.append([summary])
    records = await session_store.read_session("s1")
    assert len(records) == 1
    assert records[0].record_type == SessionRecordType.COMPACTION_SUMMARY
    assert "[Session Compacted" in records[0].payload["content"]


@pytest.mark.asyncio
async def test_replay_after_compaction(
    session_store: FilesystemSessionStore,
) -> None:
    """After compaction, replay returns system + summary + new turns."""
    # Simulate post-compaction state: system msg, compaction summary, new turn
    await session_store.append(
        [
            SessionRecord(
                session_id="s1",
                sequence=0,
                event_id="sys-1",
                turn_id="t0",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.SYSTEM_MESSAGE,
                payload={
                    "message_id": "sys-1",
                    "content": "You are an assistant.",
                    "scope": SystemMessageScope.SESSION.value,
                },
            ),
            SessionRecord(
                session_id="s1",
                sequence=1,
                event_id="compaction-abc",
                turn_id="compaction-2024-01-01",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.COMPACTION_SUMMARY,
                payload={
                    "message_id": "summary-abc",
                    "content": "[Session Compacted - Pass 1]\n\nUser discussed X.",
                    "scope": SystemMessageScope.SESSION.value,
                },
            ),
        ]
    )
    # Add a new complete turn after compaction
    await session_store.append(
        [
            SessionRecord(
                session_id="s1",
                sequence=2,
                event_id="e-user",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.USER_MESSAGE,
                payload={"message_id": "m-user", "content": "Continue please"},
            ),
            SessionRecord(
                session_id="s1",
                sequence=3,
                event_id="e-asst",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.ASSISTANT_MESSAGE,
                payload={"message_id": "m-asst", "content": "Sure!"},
            ),
            SessionRecord(
                session_id="s1",
                sequence=4,
                event_id="e-term",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.TURN_TERMINAL,
                payload={"status": TurnTerminalStatus.COMPLETED.value},
            ),
        ]
    )

    result = await session_store.replay_for_turn("s1", budget=50)
    types = [r.record_type for r in result]
    assert SessionRecordType.SYSTEM_MESSAGE in types
    assert SessionRecordType.COMPACTION_SUMMARY in types
    assert SessionRecordType.USER_MESSAGE in types
    # System msg should be first, then compaction summary, then turn records.
    sys_idx = types.index(SessionRecordType.SYSTEM_MESSAGE)
    compact_idx = types.index(SessionRecordType.COMPACTION_SUMMARY)
    user_idx = types.index(SessionRecordType.USER_MESSAGE)
    assert sys_idx < compact_idx < user_idx
