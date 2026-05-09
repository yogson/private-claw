"""Tests for FilesystemSessionStore."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

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
async def test_clear_session_removes_existing(session_store: FilesystemSessionStore) -> None:
    await session_store.append([make_record("session-1")])
    assert "session-1" in session_store._locks
    assert await session_store.session_exists("session-1")
    assert await session_store.clear_session("session-1") is True
    assert "session-1" in session_store._locks
    assert not await session_store.session_exists("session-1")
    assert await session_store.read_session("session-1") == []


@pytest.mark.asyncio
async def test_clear_session_missing_returns_false(session_store: FilesystemSessionStore) -> None:
    assert await session_store.clear_session("missing-session") is False


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


# ---------------------------------------------------------------------------
# replace_session tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_session_replaces_content(session_store: FilesystemSessionStore) -> None:
    """replace_session atomically swaps the session file contents."""
    # Seed with original records.
    await session_store.append([make_record("session-1", event_id="e-old")])
    original = await session_store.read_session("session-1")
    assert len(original) == 1

    # Replace with new records.
    new_records = [
        make_record("session-1", event_id="e-new-1"),
        make_record("session-1", event_id="e-new-2"),
    ]
    await session_store.replace_session("session-1", new_records)

    stored = await session_store.read_session("session-1")
    assert len(stored) == 2
    assert stored[0].event_id == "e-new-1"
    assert stored[1].event_id == "e-new-2"


@pytest.mark.asyncio
async def test_replace_session_creates_file_if_missing(
    session_store: FilesystemSessionStore,
) -> None:
    """replace_session works even when no session file exists yet."""
    new_records = [make_record("session-new", event_id="e-1")]
    await session_store.replace_session("session-new", new_records)

    stored = await session_store.read_session("session-new")
    assert len(stored) == 1
    assert stored[0].event_id == "e-1"


@pytest.mark.asyncio
async def test_replace_session_preserves_old_on_write_failure(
    session_store: FilesystemSessionStore,
) -> None:
    """If the temp-file write fails, the original session data is untouched."""
    await session_store.append([make_record("session-1", event_id="e-original")])

    with (
        patch("assistant.store.filesystem.session.os.write", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        await session_store.replace_session(
            "session-1", [make_record("session-1", event_id="e-new")]
        )

    # Original data should be intact.
    stored = await session_store.read_session("session-1")
    assert len(stored) == 1
    assert stored[0].event_id == "e-original"


@pytest.mark.asyncio
async def test_replace_session_holds_lock_across_operation(
    session_store: FilesystemSessionStore,
) -> None:
    """The session lock is held for the entire replace_session call, preventing
    concurrent interleaving between clear and write."""
    lock = session_store._get_lock("session-1")  # noqa: SLF001
    lock_was_held = False

    original_os_replace = __import__("os").replace

    def os_replace_spy(src: str, dst: str) -> None:
        """Intercept os.replace to verify the lock is held at the critical moment."""
        nonlocal lock_was_held
        lock_was_held = lock.locked()
        original_os_replace(src, dst)

    new_records = [make_record("session-1", event_id="e-1")]

    with patch("assistant.store.filesystem.session.os.replace", side_effect=os_replace_spy):
        await session_store.replace_session("session-1", new_records)

    # Lock was held during the atomic rename.
    assert lock_was_held
    # Lock is released after the call.
    assert not lock.locked()

    # Verify data was written correctly.
    stored = await session_store.read_session("session-1")
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_replace_session_cleans_up_temp_on_failure(
    session_store: FilesystemSessionStore, sessions_dir: Path
) -> None:
    """Temp file is cleaned up if an error occurs during write."""
    with (
        patch("assistant.store.filesystem.session.os.write", side_effect=OSError("fail")),
        pytest.raises(OSError),
    ):
        await session_store.replace_session("session-1", [make_record("session-1", event_id="e-1")])

    # No temp files should remain.
    tmp_files = list(sessions_dir.glob(".tmp_*"))
    assert tmp_files == []
