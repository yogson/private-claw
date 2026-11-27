"""Compaction keeps /usage totals intact (#123).

Real ``FilesystemSessionStore`` and ``UsageStatsService``; only the summarizer
LLM call is mocked. Summarized turns' usage must move to the archive, and the
turns compaction keeps verbatim must not be archived too (double counting).
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from assistant.channels.telegram.models import EventType, NormalizedEvent
from assistant.channels.telegram.usage import UsageStatsService
from assistant.core.config.schemas import CompactionConfig
from assistant.core.events.models import EventSource
from assistant.core.orchestrator.service import Orchestrator
from assistant.store.filesystem.session import FilesystemSessionStore
from assistant.store.models import SessionRecord, SessionRecordType, TurnTerminalStatus

SESSION_ID = "tg:42"
USER_ID = "42"


def _turn(turn_id: str, sequence: int, input_tokens: int) -> list[SessionRecord]:
    now = datetime.now(UTC)
    base = {"session_id": SESSION_ID, "turn_id": turn_id, "timestamp": now}
    return [
        SessionRecord(
            **base,
            sequence=sequence,
            event_id=f"u-{turn_id}",
            record_type=SessionRecordType.USER_MESSAGE,
            payload={"message_id": f"u-{turn_id}", "content": "hi", "user_id": USER_ID},
        ),
        SessionRecord(
            **base,
            sequence=sequence + 1,
            event_id=f"a-{turn_id}",
            record_type=SessionRecordType.ASSISTANT_MESSAGE,
            payload={
                "message_id": f"a-{turn_id}",
                "content": "reply",
                "model_id": "claude-sonnet-5-5",
                "user_id": USER_ID,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": 100,
                    "cache_read_tokens": input_tokens // 2,
                    "cache_write_tokens": 50,
                    "last_request_input_tokens": input_tokens,
                },
            },
        ),
        SessionRecord(
            **base,
            sequence=sequence + 2,
            event_id=f"t-{turn_id}",
            record_type=SessionRecordType.TURN_TERMINAL,
            payload={"status": TurnTerminalStatus.COMPLETED.value},
        ),
    ]


def _usage_event() -> NormalizedEvent:
    return NormalizedEvent(
        event_id="ev-usage",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=SESSION_ID,
        user_id=USER_ID,
        created_at=datetime.now(UTC),
        trace_id="trace-usage",
        text="/usage",
        metadata={"chat_id": 42},
    )


async def _today_and_month(usage: UsageStatsService) -> str:
    """The Today and This month blocks of /usage (the session block is per-session)."""
    text = (await usage.build_usage_response(_usage_event())).text
    return text.split("*Today (UTC)*", 1)[1]


def _build(tmp_path: Path) -> tuple[Orchestrator, FilesystemSessionStore, UsageStatsService]:
    sessions = FilesystemSessionStore(tmp_path / "sessions")
    usage = UsageStatsService(
        session_store=sessions,
        archive_dir=tmp_path / "usage_archive",
        default_model_id="claude-sonnet-5-5",
    )
    store = MagicMock()
    store.sessions = sessions
    runtime_config = MagicMock()
    runtime_config.compaction = CompactionConfig(
        enabled=True,
        token_threshold=10_000,
        min_turns_before_compact=2,
        max_compactions=5,
        keep_recent_turns=2,
    )
    summarizer = AsyncMock()
    summarizer.summarize = AsyncMock(return_value="Summary.")
    with patch(
        "assistant.core.orchestrator.service.ChatCompactionService", return_value=summarizer
    ):
        orch = Orchestrator(
            store=store,
            config=runtime_config,
            idempotency=MagicMock(),
            session_factory=MagicMock(),
            usage_service=usage,
        )
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=None)
    orch._session_factory.resume = AsyncMock(return_value=ctx)  # noqa: SLF001
    return orch, sessions, usage


@pytest.mark.asyncio
async def test_usage_totals_survive_repeated_compaction(tmp_path: Path) -> None:
    orch, sessions, usage = _build(tmp_path)
    seq = 0
    for i in range(4):
        await sessions.append(_turn(f"t{i}", seq, 20_000 + i))
        seq += 3
    before = await _today_and_month(usage)

    # Automatic path: the turn passes the snapshot read by _should_compact_session.
    snapshot = await orch._should_compact_session(SESSION_ID)  # noqa: SLF001
    assert snapshot is not None
    assert await orch._compact_session(  # noqa: SLF001
        SESSION_ID, "trace-auto", USER_ID, records=snapshot
    )
    assert await _today_and_month(usage) == before

    # More turns, then the /compact command path. The two turns kept by the first
    # compaction are summarized now and must be archived exactly once.
    seq = max(r.sequence for r in await sessions.read_session(SESSION_ID)) + 1
    for i in range(4, 7):
        await sessions.append(_turn(f"t{i}", seq, 30_000 + i))
        seq += 3
    before = await _today_and_month(usage)
    assert await orch.compact_session(SESSION_ID, "trace-manual", USER_ID)
    after = await _today_and_month(usage)
    assert after == before

    live_turns = {
        r.turn_id
        for r in await sessions.read_session(SESSION_ID)
        if r.record_type == SessionRecordType.ASSISTANT_MESSAGE
    }
    assert live_turns == {"t5", "t6"}  # keep_recent_turns=2
    archive_rows = (tmp_path / "usage_archive" / f"{USER_ID}.jsonl").read_text().splitlines()
    assert len(archive_rows) == 5  # t0..t4, each once


@pytest.mark.asyncio
async def test_compaction_triggered_without_user_still_archives_owner_usage(
    tmp_path: Path,
) -> None:
    """A compaction not attributed to the session's user (e.g. a system-triggered turn)
    must not drop that user's summarized usage."""
    orch, sessions, usage = _build(tmp_path)
    seq = 0
    for i in range(3):
        await sessions.append(_turn(f"t{i}", seq, 20_000))
        seq += 3
    before = await _today_and_month(usage)

    assert await orch.compact_session(SESSION_ID, "trace-sys", None)

    assert await _today_and_month(usage) == before
