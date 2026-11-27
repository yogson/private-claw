"""Compaction keeps /usage totals intact (#123).

Real ``FilesystemSessionStore`` and ``UsageStatsService``; only the summarizer
LLM call is mocked. Summarized turns' usage must move to the archive, and the
turns compaction keeps verbatim must not be archived too (double counting).
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

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


@pytest.mark.asyncio
async def test_reset_during_compaction_is_serialized_and_archives_each_turn_once(
    tmp_path: Path,
) -> None:
    """A /reset arriving while compaction waits on the summarizer must wait for it.

    Otherwise /reset archives and clears every turn, compaction then restores the kept
    turns and archives the summarized ones a second time from its old snapshot.
    """
    from assistant.api.orchestrator_handler import _build_orchestrator_handler

    orch, sessions, usage = _build(tmp_path)
    seq = 0
    for i in range(4):
        await sessions.append(_turn(f"t{i}", seq, 20_000 + i))
        seq += 3
    before = await _today_and_month(usage)

    summarizing = asyncio.Event()
    release = asyncio.Event()

    async def _slow_summarize(*_: object) -> str:
        summarizing.set()
        await release.wait()
        return "Summary."

    orch._compaction_service.summarize = AsyncMock(side_effect=_slow_summarize)  # noqa: SLF001

    adapter = MagicMock()
    adapter.is_confirmation_callback.return_value = False
    adapter.is_stop_request.return_value = False
    adapter.is_verbose_request.return_value = False
    adapter.is_session_new_request.return_value = False
    adapter.is_session_reset_request.return_value = True
    adapter.is_session_reset_available.return_value = True

    async def _clear(event: NormalizedEvent) -> bool:
        return await sessions.clear_session(event.session_id)

    adapter.reset_session_context = AsyncMock(side_effect=_clear)
    handler = _build_orchestrator_handler(adapter, orch, usage_service=usage)
    reset_event = _usage_event().model_copy(
        update={"event_id": "ev-reset", "trace_id": "trace-reset", "text": "/reset"}
    )

    compact_task = asyncio.create_task(orch.compact_session(SESSION_ID, "trace-c", USER_ID))
    await summarizing.wait()
    reset_task = asyncio.create_task(handler(reset_event))
    # Fixed: /reset queues behind compaction (2 users of the slot). Unfixed: it runs now.
    while orch._turn_queue.depth(SESSION_ID) < 2 and not reset_task.done():  # noqa: SLF001
        await asyncio.sleep(0)
    assert not reset_task.done()

    release.set()
    assert await compact_task
    response = await reset_task

    assert response is not None
    assert response.text == "Session context reset. Starting fresh."
    assert await sessions.read_session(SESSION_ID) == []
    archive_rows = (tmp_path / "usage_archive" / f"{USER_ID}.jsonl").read_text().splitlines()
    assert len(archive_rows) == 4  # t0..t3, each exactly once
    assert await _today_and_month(usage) == before


@pytest.mark.asyncio
async def test_token_limit_auto_reset_is_serialized_with_compaction(tmp_path: Path) -> None:
    """The automatic reset after a token-limit error takes the turn slot like /reset.

    A turn queued behind the failing one may be compacting (waiting on its summarizer);
    an unserialized auto-reset would archive and clear, then compaction would archive the
    summarized turns a second time and restore the kept ones.
    """
    from pydantic_ai.exceptions import ModelHTTPError

    from assistant.api.orchestrator_handler import _build_orchestrator_handler

    orch, sessions, usage = _build(tmp_path)
    seq = 0
    for i in range(4):
        await sessions.append(_turn(f"t{i}", seq, 20_000 + i))
        seq += 3
    before = await _today_and_month(usage)

    summarizing = asyncio.Event()
    release = asyncio.Event()

    async def _slow_summarize(*_: object) -> str:
        summarizing.set()
        await release.wait()
        return "Summary."

    orch._compaction_service.summarize = AsyncMock(side_effect=_slow_summarize)  # noqa: SLF001
    orch.execute_turn = AsyncMock(  # type: ignore[method-assign]
        side_effect=ModelHTTPError(
            status_code=400,
            model_name="claude-opus-4-5",
            body={"error": {"message": "prompt is too long: 202847 tokens > 200000 maximum"}},
        )
    )

    adapter = MagicMock()
    adapter.is_confirmation_callback.return_value = False
    adapter.is_stop_request.return_value = False
    adapter.is_verbose_request.return_value = False
    adapter.is_session_new_request.return_value = False
    adapter.is_session_reset_request.return_value = False
    adapter.is_compact_request.return_value = False
    adapter.is_wtf_request.return_value = False
    adapter.is_session_resume_request.return_value = False
    adapter.is_session_resume_callback.return_value = False
    adapter.is_model_request.return_value = False
    adapter.is_model_callback_request.return_value = False
    adapter.is_capabilities_request.return_value = False
    adapter.is_capabilities_callback_request.return_value = False
    adapter.is_usage_request.return_value = False
    adapter.get_model_override.return_value = None
    adapter.get_capabilities_override.return_value = None
    adapter.is_session_reset_available.return_value = True

    async def _clear(event: NormalizedEvent) -> bool:
        return await sessions.clear_session(event.session_id)

    adapter.reset_session_context = AsyncMock(side_effect=_clear)
    handler = _build_orchestrator_handler(adapter, orch, usage_service=usage)
    event = _usage_event().model_copy(
        update={"event_id": "ev-overflow", "trace_id": "trace-overflow", "text": "hello"}
    )

    compact_task = asyncio.create_task(orch.compact_session(SESSION_ID, "trace-c", USER_ID))
    await summarizing.wait()
    handler_task = asyncio.create_task(handler(event))
    # Fixed: the auto-reset queues behind compaction (2 users of the slot).
    # Unfixed: it archives and clears right away.
    while orch._turn_queue.depth(SESSION_ID) < 2 and not handler_task.done():  # noqa: SLF001
        await asyncio.sleep(0)
    assert not handler_task.done()

    release.set()
    assert await compact_task
    response = await handler_task

    assert response is not None
    assert "Session has been reset" in response.text
    assert await sessions.read_session(SESSION_ID) == []
    archive_rows = (tmp_path / "usage_archive" / f"{USER_ID}.jsonl").read_text().splitlines()
    assert len(archive_rows) == 4  # t0..t3, each exactly once
    assert await _today_and_month(usage) == before


@pytest.mark.asyncio
async def test_compaction_archive_failure_logs_error(tmp_path: Path) -> None:
    """The usage of the summarized turns is lost here, so the log must carry the cause."""
    orch, sessions, usage = _build(tmp_path)
    seq = 0
    for i in range(3):
        await sessions.append(_turn(f"t{i}", seq, 20_000))
        seq += 3
    usage.archive_compacted_usage = AsyncMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]

    with capture_logs() as logs:
        assert await orch.compact_session(SESSION_ID, "trace-fail", USER_ID)

    (entry,) = [e for e in logs if e["event"] == "orchestrator.compaction_archive_failed"]
    assert entry["error"] == "disk full"
    assert entry["exc_info"] is True
