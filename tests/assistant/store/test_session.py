"""Tests for FilesystemSessionStore."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.store.filesystem.session import FilesystemSessionStore
from assistant.store.models import SessionRecord, SessionRecordType


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


@pytest.fixture
def session_store(sessions_dir: Path) -> FilesystemSessionStore:
    return FilesystemSessionStore(sessions_dir)


def make_record(
    session_id: str,
    record_type: SessionRecordType = SessionRecordType.USER_MESSAGE,
    turn_id: str = "turn-1",
    event_id: str | None = None,
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        sequence=0,
        event_id=event_id or f"event-{datetime.now(UTC).timestamp()}",
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        record_type=record_type,
        payload={"message_id": "msg-1", "content": "test"},
    )


@pytest.mark.asyncio
async def test_append_single_record(session_store: FilesystemSessionStore) -> None:
    record = make_record("session-1")
    await session_store.append([record])
    records = await session_store.read_session("session-1")
    assert len(records) == 1
    assert records[0].sequence == 0


@pytest.mark.asyncio
async def test_append_multiple_records(session_store: FilesystemSessionStore) -> None:
    records = [make_record("session-1", event_id=f"e-{i}") for i in range(3)]
    await session_store.append(records)
    stored = await session_store.read_session("session-1")
    assert len(stored) == 3
    assert [r.sequence for r in stored] == [0, 1, 2]


@pytest.mark.asyncio
async def test_append_assigns_sequence(session_store: FilesystemSessionStore) -> None:
    await session_store.append([make_record("session-1", event_id="e-1")])
    await session_store.append([make_record("session-1", event_id="e-2")])
    records = await session_store.read_session("session-1")
    assert records[0].sequence == 0
    assert records[1].sequence == 1


@pytest.mark.asyncio
async def test_append_mixed_session_ids_raises(
    session_store: FilesystemSessionStore,
) -> None:
    records = [make_record("session-1"), make_record("session-2")]
    with pytest.raises(ValueError, match="same session_id"):
        await session_store.append(records)


@pytest.mark.asyncio
async def test_read_empty_session(session_store: FilesystemSessionStore) -> None:
    records = await session_store.read_session("nonexistent")
    assert records == []


@pytest.mark.asyncio
async def test_read_window(session_store: FilesystemSessionStore) -> None:
    records = [make_record("session-1", event_id=f"e-{i}") for i in range(5)]
    await session_store.append(records)
    window = await session_store.read_window("session-1", 3)
    assert len(window) == 3
    assert window[0].sequence == 2
    assert window[2].sequence == 4


@pytest.mark.asyncio
async def test_get_next_sequence(session_store: FilesystemSessionStore) -> None:
    assert await session_store.get_next_sequence("session-1") == 0
    await session_store.append([make_record("session-1")])
    assert await session_store.get_next_sequence("session-1") == 1


@pytest.mark.asyncio
async def test_session_exists(session_store: FilesystemSessionStore) -> None:
    assert not await session_store.session_exists("session-1")
    await session_store.append([make_record("session-1")])
    assert await session_store.session_exists("session-1")


@pytest.mark.asyncio
async def test_list_sessions(session_store: FilesystemSessionStore) -> None:
    assert await session_store.list_sessions() == []
    await session_store.append([make_record("session-1")])
    await session_store.append([make_record("session-2")])
    sessions = await session_store.list_sessions()
    assert set(sessions) == {"session-1", "session-2"}


@pytest.mark.asyncio
async def test_append_duplicate_event_id_skipped(
    session_store: FilesystemSessionStore,
) -> None:
    """Idempotency: repeated append with same event_id must not duplicate records."""
    record = make_record("session-1", event_id="event-123")
    await session_store.append([record])
    await session_store.append([record])
    await session_store.append([record])
    records = await session_store.read_session("session-1")
    assert len(records) == 1
    assert records[0].event_id == "event-123"


@pytest.mark.asyncio
async def test_append_mixed_new_and_duplicate_event_ids(
    session_store: FilesystemSessionStore,
) -> None:
    """Only new event_ids are appended; duplicates are silently skipped."""
    await session_store.append([make_record("session-1", event_id="e-1")])
    await session_store.append(
        [
            make_record("session-1", event_id="e-1"),
            make_record("session-1", event_id="e-2"),
            make_record("session-1", event_id="e-3"),
        ]
    )
    records = await session_store.read_session("session-1")
    assert len(records) == 3
    assert [r.event_id for r in records] == ["e-1", "e-2", "e-3"]


@pytest.mark.asyncio
async def test_append_is_truly_append_only(
    session_store: FilesystemSessionStore, sessions_dir: Path
) -> None:
    """File-level append: new records are appended, not rewritten."""
    await session_store.append([make_record("session-1", event_id="e-1")])
    path = sessions_dir / "session-1.jsonl"
    size_after_first = path.stat().st_size

    await session_store.append([make_record("session-1", event_id="e-2")])
    size_after_second = path.stat().st_size

    assert size_after_second > size_after_first
    content = path.read_text()
    lines = [line for line in content.strip().split("\n") if line]
    assert len(lines) == 2


@pytest.mark.asyncio
async def test_append_raw_bypasses_idempotency(
    session_store: FilesystemSessionStore,
) -> None:
    """append_raw allows duplicate event_ids (for recovery repairs)."""
    now = datetime.now(UTC)
    record1 = SessionRecord(
        session_id="session-1",
        sequence=0,
        event_id="event-same",
        turn_id="turn-1",
        timestamp=now,
        record_type=SessionRecordType.USER_MESSAGE,
        payload={"message_id": "m1", "content": "first"},
    )
    record2 = SessionRecord(
        session_id="session-1",
        sequence=1,
        event_id="event-same",
        turn_id="turn-1",
        timestamp=now,
        record_type=SessionRecordType.USER_MESSAGE,
        payload={"message_id": "m2", "content": "second"},
    )
    await session_store.append_raw([record1])
    await session_store.append_raw([record2])
    records = await session_store.read_session("session-1")
    assert len(records) == 2
