"""
Unit tests for Telegram usage statistics service.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from genai_prices import Usage, calc_price

from assistant.channels.telegram.models import EventType, NormalizedEvent
from assistant.channels.telegram.usage import (
    UsageStatsService,
    _parse_session_chat_id,
    _record_date,
    _session_belongs_to_user,
    _turn_user_id_map,
)
from assistant.core.events.models import EventSource
from assistant.store.filesystem.session import FilesystemSessionStore
from assistant.store.models import SessionRecord, SessionRecordType


def test_parse_session_chat_id_extracts_chat_id() -> None:
    assert _parse_session_chat_id("tg:123") == 123
    assert _parse_session_chat_id("tg:123:abc") == 123
    assert _parse_session_chat_id("tg:99999") == 99999


def test_parse_session_chat_id_returns_none_for_invalid() -> None:
    assert _parse_session_chat_id("other:123") is None
    assert _parse_session_chat_id("tg:") is None
    assert _parse_session_chat_id("tg:abc") is None


def test_session_belongs_to_user_chat_derived() -> None:
    """Session tg:123 belongs to user 123 (private chat)."""
    records: list[SessionRecord] = []
    assert _session_belongs_to_user("tg:123", "123", records) is True
    assert _session_belongs_to_user("tg:123:abc", "123", records) is True


def test_session_belongs_to_user_user_message_payload() -> None:
    """Session belongs to user when user_message has matching user_id."""
    records = [
        SessionRecord(
            session_id="tg:456",
            sequence=0,
            event_id="ev-1",
            turn_id="turn-1",
            timestamp=datetime.now(UTC),
            record_type=SessionRecordType.USER_MESSAGE,
            payload={"user_id": "999", "message_id": "m1", "content": "hi"},
        ),
    ]
    assert _session_belongs_to_user("tg:456", "999", records) is True
    assert _session_belongs_to_user("tg:456", "123", records) is False


def test_record_date_extracts_date() -> None:
    ts = datetime(2025, 3, 13, 14, 30, 0, tzinfo=UTC)
    record = SessionRecord(
        session_id="tg:123",
        sequence=0,
        event_id="ev-1",
        turn_id="turn-1",
        timestamp=ts,
        record_type=SessionRecordType.ASSISTANT_MESSAGE,
        payload={"message_id": "m1", "content": "hi"},
    )
    assert _record_date(record) == ts.date()


@pytest.mark.asyncio
async def test_build_usage_response_empty_store_returns_no_usage_message(
    tmp_path: Path,
) -> None:
    """When no usage is recorded, returns 'No usage recorded yet' message."""
    store = FilesystemSessionStore(tmp_path / "sessions")
    service = UsageStatsService(session_store=store)
    event = NormalizedEvent(
        event_id="ev-1",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-1",
        text="/usage",
        metadata={"chat_id": 123},
    )
    response = await service.build_usage_response(event)
    assert response is not None
    assert "No usage recorded yet" in response.text


def test_turn_user_id_map_builds_from_user_messages() -> None:
    """_turn_user_id_map extracts turn_id -> user_id from user_message records."""
    records = [
        SessionRecord(
            session_id="tg:123",
            sequence=0,
            event_id="ev-1",
            turn_id="turn-a",
            timestamp=datetime.now(UTC),
            record_type=SessionRecordType.USER_MESSAGE,
            payload={"user_id": "999", "message_id": "m1", "content": "hi"},
        ),
        SessionRecord(
            session_id="tg:123",
            sequence=1,
            event_id="ev-2",
            turn_id="turn-b",
            timestamp=datetime.now(UTC),
            record_type=SessionRecordType.USER_MESSAGE,
            payload={"user_id": "111", "message_id": "m2", "content": "bye"},
        ),
    ]
    assert _turn_user_id_map(records) == {"turn-a": "999", "turn-b": "111"}


@pytest.mark.asyncio
async def test_archive_session_usage_persists_after_reset(tmp_path: Path) -> None:
    """Archived usage appears in Today/Month after session is cleared."""
    store = FilesystemSessionStore(tmp_path / "sessions")
    archive_dir = tmp_path / "usage_archive"
    service = UsageStatsService(
        session_store=store,
        archive_dir=archive_dir,
    )
    await store.append(
        [
            SessionRecord(
                session_id="tg:123",
                sequence=0,
                event_id="ev-1",
                turn_id="turn-1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.USER_MESSAGE,
                payload={"user_id": "123", "message_id": "m1", "content": "hi"},
            ),
            SessionRecord(
                session_id="tg:123",
                sequence=1,
                event_id="msg-1",
                turn_id="turn-1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.ASSISTANT_MESSAGE,
                payload={
                    "message_id": "msg-1",
                    "content": "reply",
                    "usage": {"input_tokens": 50, "output_tokens": 20},
                    "user_id": "123",
                },
            ),
        ]
    )
    await service.archive_session_usage("tg:123", "123")
    await store.clear_session("tg:123")

    event = NormalizedEvent(
        event_id="ev-usage",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-1",
        text="/usage",
        metadata={"chat_id": 123},
    )
    response = await service.build_usage_response(event)
    assert response is not None
    assert "No usage recorded" not in response.text
    assert "50 in / 20 out" in response.text


@pytest.mark.asyncio
async def test_build_usage_response_excludes_other_users_usage(tmp_path: Path) -> None:
    """Usage from assistant records attributed to another user is not counted."""
    store = FilesystemSessionStore(tmp_path / "sessions")
    await store.append(
        [
            SessionRecord(
                session_id="tg:123",
                sequence=0,
                event_id="turn-1",
                turn_id="turn-1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.USER_MESSAGE,
                payload={"user_id": "999", "message_id": "m1", "content": "hi"},
            ),
            SessionRecord(
                session_id="tg:123",
                sequence=1,
                event_id="msg-1",
                turn_id="turn-1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.ASSISTANT_MESSAGE,
                payload={
                    "message_id": "msg-1",
                    "content": "reply",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "user_id": "999",
                },
            ),
        ]
    )
    service = UsageStatsService(session_store=store)
    event = NormalizedEvent(
        event_id="ev-usage",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-1",
        text="/usage",
        metadata={"chat_id": 123},
    )
    response = await service.build_usage_response(event)
    assert response is not None
    assert "0 in / 0 out" in response.text or "No usage recorded" in response.text


# ---------------------------------------------------------------------------
# Cache-aware pricing (#123)
# ---------------------------------------------------------------------------

_MODEL = "claude-sonnet-5-5"
# input_tokens is the run total and includes cache reads/writes (pydantic-ai convention).
_CACHED_USAGE = {
    "input_tokens": 200_000,
    "output_tokens": 2_000,
    "cache_read_tokens": 150_000,
    "cache_write_tokens": 20_000,
    "cache_write_1h_tokens": 0,
}


def _usage_event(user_id: str = "123") -> NormalizedEvent:
    return NormalizedEvent(
        event_id="ev-usage",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=f"tg:{user_id}",
        user_id=user_id,
        created_at=datetime.now(UTC),
        trace_id="trace-1",
        text="/usage",
        metadata={"chat_id": int(user_id)},
    )


def _turn(
    turn_id: str, sequence: int, usage: dict, user_id: str = "123", session_id: str = "tg:123"
) -> list[SessionRecord]:
    return [
        SessionRecord(
            session_id=session_id,
            sequence=sequence,
            event_id=f"ev-{turn_id}",
            turn_id=turn_id,
            timestamp=datetime.now(UTC),
            record_type=SessionRecordType.USER_MESSAGE,
            payload={"user_id": user_id, "message_id": f"m-{turn_id}", "content": "hi"},
        ),
        SessionRecord(
            session_id=session_id,
            sequence=sequence + 1,
            event_id=f"msg-{turn_id}",
            turn_id=turn_id,
            timestamp=datetime.now(UTC),
            record_type=SessionRecordType.ASSISTANT_MESSAGE,
            payload={
                "message_id": f"msg-{turn_id}",
                "content": "reply",
                "model_id": _MODEL,
                "usage": usage,
                "user_id": user_id,
            },
        ),
    ]


def _genai_cost(**tokens: int) -> float:
    return float(calc_price(Usage(**tokens), _MODEL, provider_id="anthropic").total_price)


@pytest.mark.asyncio
async def test_cached_turn_cost_uses_cache_rates(tmp_path: Path) -> None:
    """Cache reads/writes are priced at genai-prices cache rates, not as base input."""
    store = FilesystemSessionStore(tmp_path / "sessions")
    await store.append(_turn("t1", 0, _CACHED_USAGE))
    service = UsageStatsService(session_store=store)

    text = (await service.build_usage_response(_usage_event())).text

    expected = _genai_cost(**_CACHED_USAGE)
    uncached = _genai_cost(input_tokens=200_000, output_tokens=2_000)
    assert round(expected, 4) != round(uncached, 4)
    assert text.count(f"Cost: ${expected:.4f}") == 3  # session, today, month
    assert "200000 in / 2000 out" in text


@pytest.mark.asyncio
async def test_legacy_usage_without_cache_fields_is_priced_as_uncached(tmp_path: Path) -> None:
    """Records written before the cache split keep today's (all-uncached) pricing."""
    store = FilesystemSessionStore(tmp_path / "sessions")
    await store.append(_turn("t1", 0, {"input_tokens": 200_000, "output_tokens": 2_000}))
    service = UsageStatsService(session_store=store)

    text = (await service.build_usage_response(_usage_event())).text

    expected = _genai_cost(input_tokens=200_000, output_tokens=2_000)
    assert text.count(f"Cost: ${expected:.4f}") == 3


@pytest.mark.asyncio
async def test_archived_rows_keep_cache_aware_cost(tmp_path: Path) -> None:
    """The cost frozen into the archive (/reset) uses cache rates and stores the split."""
    store = FilesystemSessionStore(tmp_path / "sessions")
    archive_dir = tmp_path / "usage_archive"
    await store.append(_turn("t1", 0, _CACHED_USAGE))
    service = UsageStatsService(session_store=store, archive_dir=archive_dir)

    await service.archive_session_usage("tg:123", "123")
    await store.clear_session("tg:123")
    text = (await service.build_usage_response(_usage_event())).text

    expected = _genai_cost(**_CACHED_USAGE)
    assert text.count(f"Cost: ${expected:.4f}") == 2  # today, month (session is empty)
    row = json.loads((archive_dir / "123.jsonl").read_text())
    assert row["cache_read_tokens"] == 150_000
    assert row["cache_write_tokens"] == 20_000


@pytest.mark.asyncio
async def test_archive_compacted_usage_archives_only_removed_turns_for_every_user(
    tmp_path: Path,
) -> None:
    """Only the summarized turns are archived (kept turns stay live), for each owner."""
    store = FilesystemSessionStore(tmp_path / "sessions")
    archive_dir = tmp_path / "usage_archive"
    usage = {"input_tokens": 100, "output_tokens": 10}
    records = _turn("t1", 0, usage) + _turn("t2", 2, usage, user_id="999") + _turn("t3", 4, usage)
    service = UsageStatsService(session_store=store, archive_dir=archive_dir)

    await service.archive_compacted_usage("tg:123", records, {"t1", "t2"})

    rows_123 = (archive_dir / "123.jsonl").read_text().strip().splitlines()
    rows_999 = (archive_dir / "999.jsonl").read_text().strip().splitlines()
    assert len(rows_123) == 1  # t1 only; kept t3 is not archived
    assert len(rows_999) == 1  # t2 belongs to another user of the session
