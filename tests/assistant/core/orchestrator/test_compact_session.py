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
        # Tests pre-date the kept-window feature — default to 0 so they keep
        # exercising the "summarize everything" path unless they opt in.
        "keep_recent_turns": 0,
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
    store.sessions.replace_session = AsyncMock()

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
        # calculate_session_total_tokens uses the last assistant record's input_tokens,
        # so the most-recent record must exceed the threshold (10_000) on its own.
        records = [
            _assistant_with_usage(0, turn_id="t1", input_tokens=5000, output_tokens=3000),
            _terminal(1, turn_id="t1"),
            _assistant_with_usage(2, turn_id="t2", input_tokens=12_000, output_tokens=3000),
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

        # Session must NOT have been replaced
        orch._store.sessions.replace_session.assert_not_called()  # noqa: SLF001

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

        # Session should have been atomically replaced (not clear + append)
        orch._store.sessions.clear_session.assert_not_called()  # noqa: SLF001
        replace_call = orch._store.sessions.replace_session  # noqa: SLF001
        replace_call.assert_awaited_once()
        assert replace_call.call_args[0][0] == "s1"
        restored = replace_call.call_args[0][1]

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

        orch._store.sessions.replace_session.assert_awaited_once()  # noqa: SLF001
        restored = orch._store.sessions.replace_session.call_args[0][1]  # noqa: SLF001

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

        orch._store.sessions.replace_session.assert_not_called()  # noqa: SLF001

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
        orch._store.sessions.replace_session.assert_awaited_once()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self) -> None:
        """_compact_session returns True after a successful compaction."""
        records = [
            _system(0),
            _rec(sequence=1, turn_id="t1"),
            _terminal(2, turn_id="t1"),
        ]
        orch = _build_orchestrator(records)

        result = await orch._compact_session("s1", "trace-1", "user-1", records=records)  # noqa: SLF001

        assert result is True

    @pytest.mark.asyncio
    async def test_summarizer_input_excludes_prior_summaries_and_system(self) -> None:
        """The summarizer must receive raw chat only — no prior COMPACTION_SUMMARY
        records (which would cause re-summarization and drift) and no SYSTEM_MESSAGE
        records (the summarizer has its own system prompt)."""
        prior_summary = "[System Context Summary]\n[Session Compacted - Pass 1]\n\nOld stuff."
        records = [
            _system(0),
            _compaction_summary(1, content=prior_summary, turn_id="compaction-1"),
            _rec(
                sequence=2,
                turn_id="t1",
                record_type=SessionRecordType.USER_MESSAGE,
                payload={"message_id": "m-2", "content": "fresh user turn"},
            ),
            _assistant_with_usage(3, turn_id="t1"),
            _terminal(4, turn_id="t1"),
        ]
        orch = _build_orchestrator(records)

        await orch._compact_session("s1", "trace-1", "user-1", records=records)  # noqa: SLF001

        summarize_mock = orch._compaction_service.summarize  # noqa: SLF001
        summarize_mock.assert_awaited_once()
        passed_messages = summarize_mock.call_args[0][0]

        def _flatten(msg: object) -> str:
            text = getattr(msg, "content", "") or ""
            for block in getattr(msg, "content_blocks", None) or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += "\n" + block.get("text", "")
            return text

        joined = "\n".join(_flatten(m) for m in passed_messages)
        assert "[Session Compacted - Pass 1]" not in joined
        assert "[System Context Summary]" not in joined
        assert "system prompt" not in joined
        assert "fresh user turn" in joined

    @pytest.mark.asyncio
    async def test_returns_false_on_summarize_failure(self) -> None:
        """_compact_session returns False when summarize raises (graceful fallback)."""
        records = [
            _system(0),
            _rec(sequence=1, turn_id="t1"),
            _terminal(2, turn_id="t1"),
        ]
        orch = _build_orchestrator(records)
        orch._compaction_service.summarize.side_effect = RuntimeError("LLM unavailable")  # noqa: SLF001

        result = await orch._compact_session("s1", "trace-1", "user-1", records=records)  # noqa: SLF001

        assert result is False
        orch._store.sessions.replace_session.assert_not_called()  # noqa: SLF001


# ---------------------------------------------------------------------------
# Compaction notifier tests – exercise the _run_turn wiring
# ---------------------------------------------------------------------------


def _make_run_turn_records() -> list[SessionRecord]:
    """Minimal session records that trigger compaction in _run_turn."""
    return [
        _system(0),
        _rec(sequence=1, turn_id="t1"),
        _terminal(2, turn_id="t1"),
    ]


def _build_event(session_id: str = "s1") -> MagicMock:
    event = MagicMock()
    event.session_id = session_id
    event.event_id = "evt-1"
    event.trace_id = "trace-1"
    event.user_id = "user-1"
    event.capabilities_override = None
    event.model_id_override = None
    event.text = "hello"
    event.voice = None
    event.attachment = None
    event.attachments = []
    event.metadata = {}
    return event


class TestCompactionNotifier:
    """Tests that the compaction_notifier callback is wired correctly in _run_turn."""

    def _build_orchestrator_for_run_turn(
        self,
        compact_return: bool,
    ) -> Orchestrator:
        """Build an orchestrator whose _compact_session and _run_turn_pydantic_ai
        are fully mocked so we can test notifier dispatch in isolation."""
        from assistant.core.orchestrator.models import OrchestratorResult

        records = _make_run_turn_records()
        orch = _build_orchestrator(records)

        # _should_compact_session → return records (compaction needed)
        orch._should_compact_session = AsyncMock(return_value=records)  # noqa: SLF001
        # _compact_session → return compact_return
        orch._compact_session = AsyncMock(return_value=compact_return)  # noqa: SLF001
        # replay_for_turn (called again after compaction) → same records
        orch._store.sessions.replay_for_turn = AsyncMock(return_value=records)  # noqa: SLF001
        # _run_turn_pydantic_ai → dummy result
        orch._run_turn_pydantic_ai = AsyncMock(  # noqa: SLF001
            return_value=OrchestratorResult(text="ok")
        )
        return orch

    @pytest.mark.asyncio
    async def test_notifier_called_after_successful_compaction(self) -> None:
        """compaction_notifier is awaited exactly once when _compact_session returns True."""
        orch = self._build_orchestrator_for_run_turn(compact_return=True)
        notifier = AsyncMock()
        event = _build_event()

        with (
            patch("assistant.core.orchestrator.service.set_trace_id"),
            patch("assistant.core.orchestrator.service.reset_trace_id"),
            patch("assistant.core.orchestrator.service.records_to_messages", return_value=[]),
            patch(
                "assistant.core.orchestrator.service.build_user_content_blocks",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("assistant.core.orchestrator.service.extract_user_text", return_value="hello"),
            patch("assistant.core.orchestrator.service.gather_attachments", return_value=[]),
            patch("assistant.core.orchestrator.service.format_attachment_context", return_value=""),
            patch(
                "assistant.core.orchestrator.service.extract_raw_text_for_multimodal",
                return_value="hello",
            ),
        ):
            await orch._run_turn(event, compaction_notifier=notifier)  # noqa: SLF001

        notifier.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_notifier_not_called_on_summarize_failure(self) -> None:
        """compaction_notifier is NOT called when _compact_session returns False."""
        orch = self._build_orchestrator_for_run_turn(compact_return=False)
        notifier = AsyncMock()
        event = _build_event()

        with (
            patch("assistant.core.orchestrator.service.set_trace_id"),
            patch("assistant.core.orchestrator.service.reset_trace_id"),
            patch("assistant.core.orchestrator.service.records_to_messages", return_value=[]),
            patch(
                "assistant.core.orchestrator.service.build_user_content_blocks",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("assistant.core.orchestrator.service.extract_user_text", return_value="hello"),
            patch("assistant.core.orchestrator.service.gather_attachments", return_value=[]),
            patch("assistant.core.orchestrator.service.format_attachment_context", return_value=""),
            patch(
                "assistant.core.orchestrator.service.extract_raw_text_for_multimodal",
                return_value="hello",
            ),
        ):
            await orch._run_turn(event, compaction_notifier=notifier)  # noqa: SLF001

        notifier.assert_not_awaited()


# ---------------------------------------------------------------------------
# keep_recent_turns tests
# ---------------------------------------------------------------------------


class TestKeepRecentTurns:
    @pytest.mark.asyncio
    async def test_keeps_last_n_turns_verbatim(self) -> None:
        """With keep_recent_turns=2, the last 2 complete turns survive
        compaction verbatim and only the older turns are summarized."""
        cfg = _make_compaction_config(keep_recent_turns=2)
        records = [
            _system(0),
            # turn t1 — should be summarized (older)
            _rec(sequence=1, turn_id="t1", payload={"message_id": "m-1", "content": "old q"}),
            _assistant_with_usage(2, turn_id="t1"),
            _terminal(3, turn_id="t1"),
            # turn t2 — should be summarized (older)
            _rec(sequence=4, turn_id="t2", payload={"message_id": "m-4", "content": "old q2"}),
            _assistant_with_usage(5, turn_id="t2"),
            _terminal(6, turn_id="t2"),
            # turn t3 — KEEP
            _rec(sequence=7, turn_id="t3", payload={"message_id": "m-7", "content": "recent q1"}),
            _assistant_with_usage(8, turn_id="t3"),
            _terminal(9, turn_id="t3"),
            # turn t4 — KEEP
            _rec(sequence=10, turn_id="t4", payload={"message_id": "m-10", "content": "recent q2"}),
            _assistant_with_usage(11, turn_id="t4"),
            _terminal(12, turn_id="t4"),
        ]
        orch = _build_orchestrator(records, compaction_config=cfg)

        await orch._compact_session("s1", "trace-1", "user-1", records=records)  # noqa: SLF001

        # Summarizer only saw turns t1 and t2.
        summarize_mock = orch._compaction_service.summarize  # noqa: SLF001
        summarize_mock.assert_awaited_once()
        passed_messages = summarize_mock.call_args[0][0]

        def _flatten(msg: object) -> str:
            text = getattr(msg, "content", "") or ""
            for block in getattr(msg, "content_blocks", None) or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += "\n" + block.get("text", "")
            return text

        joined = "\n".join(_flatten(m) for m in passed_messages)
        assert "old q" in joined
        assert "old q2" in joined
        assert "recent q1" not in joined
        assert "recent q2" not in joined

        # Restored session contains system + new summary + ALL records of t3, t4.
        restored = orch._store.sessions.replace_session.call_args[0][1]  # noqa: SLF001
        restored_turn_ids = {r.turn_id for r in restored}
        assert "t3" in restored_turn_ids
        assert "t4" in restored_turn_ids
        assert "t1" not in restored_turn_ids
        assert "t2" not in restored_turn_ids

        # Kept records appear AFTER the new summary in the restored list,
        # so their original sequences flow into future appends correctly.
        types_in_order = [r.record_type for r in restored]
        summary_idx = types_in_order.index(SessionRecordType.COMPACTION_SUMMARY)
        kept_indices = [
            i for i, r in enumerate(restored) if r.turn_id in {"t3", "t4"}
        ]
        assert all(i > summary_idx for i in kept_indices)

    @pytest.mark.asyncio
    async def test_skips_when_all_turns_within_keep_window(self) -> None:
        """If keep_recent_turns covers every turn, _compact_session is a no-op
        rather than producing an empty summary."""
        cfg = _make_compaction_config(keep_recent_turns=5)
        records = [
            _system(0),
            _rec(sequence=1, turn_id="t1"),
            _assistant_with_usage(2, turn_id="t1"),
            _terminal(3, turn_id="t1"),
            _rec(sequence=4, turn_id="t2"),
            _assistant_with_usage(5, turn_id="t2"),
            _terminal(6, turn_id="t2"),
        ]
        orch = _build_orchestrator(records, compaction_config=cfg)

        result = await orch._compact_session(  # noqa: SLF001
            "s1", "trace-1", "user-1", records=records
        )

        assert result is False
        orch._compaction_service.summarize.assert_not_awaited()  # noqa: SLF001
        orch._store.sessions.replace_session.assert_not_called()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_should_compact_requires_min_plus_keep_turns(self) -> None:
        """_should_compact_session must wait until there are enough turns to
        both summarize (min_turns_before_compact) AND keep (keep_recent_turns)."""
        cfg = _make_compaction_config(
            token_threshold=10_000,
            min_turns_before_compact=2,
            keep_recent_turns=3,
        )
        # 4 turns total — not enough (need 2 + 3 = 5).
        records = [
            _rec(sequence=0, turn_id="t1"),
            _assistant_with_usage(1, turn_id="t1", input_tokens=12_000, output_tokens=100),
            _terminal(2, turn_id="t1"),
            _rec(sequence=3, turn_id="t2"),
            _assistant_with_usage(4, turn_id="t2", input_tokens=12_000, output_tokens=100),
            _terminal(5, turn_id="t2"),
            _rec(sequence=6, turn_id="t3"),
            _assistant_with_usage(7, turn_id="t3", input_tokens=12_000, output_tokens=100),
            _terminal(8, turn_id="t3"),
            _rec(sequence=9, turn_id="t4"),
            _assistant_with_usage(10, turn_id="t4", input_tokens=12_000, output_tokens=100),
            _terminal(11, turn_id="t4"),
        ]
        orch = _build_orchestrator(records, compaction_config=cfg)
        assert await orch._should_compact_session("s1") is None  # noqa: SLF001

        # Add a 5th turn — now we have min(2) + keep(3) = 5, should trigger.
        records.extend(
            [
                _rec(sequence=12, turn_id="t5"),
                _assistant_with_usage(13, turn_id="t5", input_tokens=12_000, output_tokens=100),
                _terminal(14, turn_id="t5"),
            ]
        )
        orch._store.sessions.read_session = AsyncMock(return_value=records)  # noqa: SLF001
        assert await orch._should_compact_session("s1") is not None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_kept_window_excludes_compaction_summaries(self) -> None:
        """The last-N window counts real turns only — prior compaction-* turns
        do not consume a slot."""
        cfg = _make_compaction_config(keep_recent_turns=2)
        records = [
            _system(0),
            _compaction_summary(1, content="Pass 1", turn_id="compaction-1"),
            # Older turn — should be summarized.
            _rec(sequence=2, turn_id="t1", payload={"message_id": "m-2", "content": "old"}),
            _assistant_with_usage(3, turn_id="t1"),
            _terminal(4, turn_id="t1"),
            # Recent — keep.
            _rec(sequence=5, turn_id="t2", payload={"message_id": "m-5", "content": "kept-a"}),
            _assistant_with_usage(6, turn_id="t2"),
            _terminal(7, turn_id="t2"),
            # Recent — keep.
            _rec(sequence=8, turn_id="t3", payload={"message_id": "m-8", "content": "kept-b"}),
            _assistant_with_usage(9, turn_id="t3"),
            _terminal(10, turn_id="t3"),
        ]
        orch = _build_orchestrator(records, compaction_config=cfg)

        await orch._compact_session("s1", "trace-1", "user-1", records=records)  # noqa: SLF001

        restored_turn_ids = {
            r.turn_id for r in orch._store.sessions.replace_session.call_args[0][1]  # noqa: SLF001
        }
        # Both real recent turns survive; the old turn does not.
        assert {"t2", "t3"} <= restored_turn_ids
        assert "t1" not in restored_turn_ids
        # Prior compaction summary is also preserved.
        assert "compaction-1" in restored_turn_ids
