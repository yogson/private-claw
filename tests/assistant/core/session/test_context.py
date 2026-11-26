"""Tests for SessionContext class."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.core.session.context import SessionContext
from assistant.core.session.metadata import (
    SessionMetadata,
    SessionState,
    SessionStatus,
    SessionType,
)
from assistant.store.filesystem.lock import FilesystemLockCoordinator
from assistant.store.interfaces import LockAcquisitionError
from assistant.store.models import LockRecord, SessionRecord, SessionRecordType


@pytest.fixture
def mock_store() -> MagicMock:
    """Create a mock store facade."""
    store = MagicMock()
    store.sessions = MagicMock()
    store.sessions.replay_for_turn = AsyncMock(return_value=[])
    store.sessions.append = AsyncMock()
    store.locks = MagicMock()
    now = datetime.now(UTC)
    lock_record = LockRecord(
        lock_key="session:test-session-abc",
        owner_id="owner",
        acquired_at=now,
        expires_at=now + timedelta(seconds=30),
    )
    store.locks.acquire_wait = AsyncMock(return_value=lock_record)
    store.locks.refresh = AsyncMock(return_value=lock_record)
    store.locks.get_lock_info = AsyncMock(return_value=None)
    store.locks.release = AsyncMock(return_value=True)
    return store


@pytest.fixture
def mock_model_context() -> MagicMock:
    """Create a mock model context service."""
    ctx = MagicMock()
    ctx.get_model_override = MagicMock(return_value=None)
    ctx.set_model_override = MagicMock()
    ctx.clear_model_override = MagicMock()
    return ctx


@pytest.fixture
def mock_capability_context() -> MagicMock:
    """Create a mock capability context service."""
    ctx = MagicMock()
    ctx.get_capabilities = MagicMock(return_value=None)
    ctx.set_capabilities = MagicMock()
    ctx.clear_capabilities = MagicMock()
    return ctx


@pytest.fixture
def metadata() -> SessionMetadata:
    """Create sample session metadata."""
    return SessionMetadata(
        session_id="test-session-abc",
        context_id="telegram:12345",
        created_at=datetime.now(UTC),
        session_type=SessionType.REGULAR,
    )


@pytest.fixture
def state() -> SessionState:
    """Create sample session state."""
    return SessionState(
        status=SessionStatus.ACTIVE,
        last_activity_at=datetime.now(UTC),
        turn_count=0,
    )


@pytest.fixture
def session_context(
    metadata: SessionMetadata,
    state: SessionState,
    mock_store: MagicMock,
    mock_model_context: MagicMock,
    mock_capability_context: MagicMock,
) -> SessionContext:
    """Create a SessionContext for testing."""
    return SessionContext(
        metadata=metadata,
        state=state,
        store=mock_store,
        model_context=mock_model_context,
        capability_context=mock_capability_context,
    )


class TestSessionContextProperties:
    """Tests for SessionContext properties."""

    def test_session_id(self, session_context: SessionContext) -> None:
        assert session_context.session_id == "test-session-abc"

    def test_context_id(self, session_context: SessionContext) -> None:
        assert session_context.context_id == "telegram:12345"

    def test_metadata(self, session_context: SessionContext, metadata: SessionMetadata) -> None:
        assert session_context.metadata == metadata

    def test_state(self, session_context: SessionContext, state: SessionState) -> None:
        assert session_context.state == state

    def test_is_long_running_false(self, session_context: SessionContext) -> None:
        assert session_context.is_long_running is False

    def test_is_long_running_true(
        self,
        mock_store: MagicMock,
        mock_model_context: MagicMock,
        mock_capability_context: MagicMock,
        state: SessionState,
    ) -> None:
        lrs_metadata = SessionMetadata(
            session_id="lrs-session",
            context_id="telegram:999",
            created_at=datetime.now(UTC),
            session_type=SessionType.LONG_RUNNING,
        )
        ctx = SessionContext(
            metadata=lrs_metadata,
            state=state,
            store=mock_store,
            model_context=mock_model_context,
            capability_context=mock_capability_context,
        )
        assert ctx.is_long_running is True

    def test_is_active(self, session_context: SessionContext) -> None:
        assert session_context.is_active is True

    def test_turn_count(self, session_context: SessionContext) -> None:
        assert session_context.turn_count == 0


class TestSessionContextResourceAccess:
    """Tests for SessionContext resource access methods."""

    @pytest.mark.asyncio
    async def test_get_history(
        self, session_context: SessionContext, mock_store: MagicMock
    ) -> None:
        expected_records = [MagicMock()]
        mock_store.sessions.replay_for_turn.return_value = expected_records

        history = await session_context.get_history(budget=50)

        assert history == expected_records
        mock_store.sessions.replay_for_turn.assert_called_once_with("test-session-abc", budget=50)

    def test_get_model_override(
        self, session_context: SessionContext, mock_model_context: MagicMock
    ) -> None:
        mock_model_context.get_model_override.return_value = "claude-3-opus"

        result = session_context.get_model_override()

        assert result == "claude-3-opus"
        mock_model_context.get_model_override.assert_called_once_with("telegram:12345")

    def test_set_model_override(
        self, session_context: SessionContext, mock_model_context: MagicMock
    ) -> None:
        session_context.set_model_override("claude-3-sonnet")

        mock_model_context.set_model_override.assert_called_once_with(
            "telegram:12345", "claude-3-sonnet"
        )

    def test_get_capabilities(
        self, session_context: SessionContext, mock_capability_context: MagicMock
    ) -> None:
        mock_capability_context.get_capabilities.return_value = ["cap1", "cap2"]

        result = session_context.get_capabilities()

        assert result == ["cap1", "cap2"]
        mock_capability_context.get_capabilities.assert_called_once_with("telegram:12345")

    def test_set_capabilities(
        self, session_context: SessionContext, mock_capability_context: MagicMock
    ) -> None:
        session_context.set_capabilities(["cap1", "cap3"])

        mock_capability_context.set_capabilities.assert_called_once_with(
            "telegram:12345", ["cap1", "cap3"]
        )


class TestSessionContextPersistence:
    """Tests for SessionContext persistence methods."""

    @pytest.mark.asyncio
    async def test_append_record(
        self, session_context: SessionContext, mock_store: MagicMock
    ) -> None:
        record = SessionRecord(
            session_id="test-session-abc",
            sequence=0,
            event_id="event-1",
            turn_id="turn-1",
            timestamp=datetime.now(UTC),
            record_type=SessionRecordType.USER_MESSAGE,
            payload={"message_id": "m1", "content": "hello"},
        )
        original_activity = session_context.state.last_activity_at

        await session_context.append_record(record)

        mock_store.sessions.append.assert_called_once_with([record])
        assert session_context.state.last_activity_at >= original_activity

    @pytest.mark.asyncio
    async def test_append_records(
        self, session_context: SessionContext, mock_store: MagicMock
    ) -> None:
        records = [
            SessionRecord(
                session_id="test-session-abc",
                sequence=i,
                event_id=f"event-{i}",
                turn_id="turn-1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.USER_MESSAGE,
                payload={"message_id": f"m{i}", "content": f"msg {i}"},
            )
            for i in range(3)
        ]

        await session_context.append_records(records)

        mock_store.sessions.append.assert_called_once_with(records)

    def test_increment_turn_count(self, session_context: SessionContext) -> None:
        assert session_context.turn_count == 0

        session_context.increment_turn_count()

        assert session_context.turn_count == 1


class TestSessionContextLifecycle:
    """Tests for SessionContext lifecycle management."""

    @pytest.mark.asyncio
    async def test_context_manager_acquire_lock(
        self, session_context: SessionContext, mock_store: MagicMock
    ) -> None:
        async with session_context as ctx:
            assert ctx is session_context
            mock_store.locks.acquire_wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager_release_lock(
        self, session_context: SessionContext, mock_store: MagicMock
    ) -> None:
        async with session_context:
            pass

        mock_store.locks.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager_release_on_exception(
        self, session_context: SessionContext, mock_store: MagicMock
    ) -> None:
        with pytest.raises(ValueError):
            async with session_context:
                raise ValueError("Test error")

        mock_store.locks.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager_lock_failure(
        self, session_context: SessionContext, mock_store: MagicMock
    ) -> None:
        mock_store.locks.acquire_wait.return_value = None

        with pytest.raises(LockAcquisitionError):
            async with session_context:
                pass
        mock_store.locks.release.assert_not_called()


class TestSessionContextStatusManagement:
    """Tests for SessionContext status management."""

    def test_suspend(self, session_context: SessionContext) -> None:
        session_context.suspend()

        assert session_context.state.status == SessionStatus.SUSPENDED

    def test_archive(self, session_context: SessionContext) -> None:
        session_context.archive()

        assert session_context.state.status == SessionStatus.ARCHIVED

    def test_activate(self, session_context: SessionContext) -> None:
        session_context.state.status = SessionStatus.SUSPENDED

        session_context.activate()

        assert session_context.state.status == SessionStatus.ACTIVE


class TestSessionContextLocking:
    """Session lock semantics over the real filesystem lock coordinator."""

    @staticmethod
    def _context(
        metadata: SessionMetadata,
        locks: FilesystemLockCoordinator,
        lock_wait_timeout_seconds: float = 5.0,
    ) -> SessionContext:
        store = MagicMock()
        store.locks = locks
        return SessionContext(
            metadata=metadata,
            state=SessionState(),
            store=store,
            model_context=MagicMock(),
            capability_context=MagicMock(),
            lock_wait_timeout_seconds=lock_wait_timeout_seconds,
        )

    @pytest.fixture
    def locks(self, tmp_path: Path) -> FilesystemLockCoordinator:
        return FilesystemLockCoordinator(tmp_path / "locks", default_ttl_seconds=30)

    def test_lock_owner_unique_per_context(
        self, metadata: SessionMetadata, locks: FilesystemLockCoordinator
    ) -> None:
        first = self._context(metadata, locks)
        second = self._context(metadata, locks)

        assert first._lock_owner != second._lock_owner  # noqa: SLF001
        assert first._lock_owner.startswith(f"session_context:{metadata.session_id}:")  # noqa: SLF001

    async def test_second_context_waits_until_first_exits(
        self, metadata: SessionMetadata, locks: FilesystemLockCoordinator
    ) -> None:
        first = self._context(metadata, locks)
        second = self._context(metadata, locks)
        events: list[str] = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def _first_turn() -> None:
            async with first:
                events.append("first:enter")
                first_entered.set()
                await release_first.wait()
                events.append("first:exit")

        async def _second_turn() -> None:
            async with second:
                events.append("second:enter")

        first_task = asyncio.create_task(_first_turn())
        await first_entered.wait()
        second_task = asyncio.create_task(_second_turn())
        await asyncio.sleep(0.2)
        # The second turn is blocked while the first holds the lock.
        assert events == ["first:enter"]
        assert not second_task.done()

        release_first.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=5)
        assert events == ["first:enter", "first:exit", "second:enter"]
        assert not await locks.is_locked(f"session:{metadata.session_id}")

    async def test_wait_timeout_raises_and_keeps_holder_lock(
        self, metadata: SessionMetadata, locks: FilesystemLockCoordinator
    ) -> None:
        first = self._context(metadata, locks)
        second = self._context(metadata, locks, lock_wait_timeout_seconds=0.2)
        lock_key = f"session:{metadata.session_id}"

        async with first:
            with pytest.raises(LockAcquisitionError, match="waited"):
                async with second:
                    pytest.fail("second context must not enter while the first holds the lock")
            info = await locks.get_lock_info(lock_key)
            assert info is not None
            assert info.owner_id == first._lock_owner  # noqa: SLF001
        assert not await locks.is_locked(lock_key)

    async def test_exit_never_releases_another_contexts_lock(
        self, metadata: SessionMetadata, locks: FilesystemLockCoordinator
    ) -> None:
        lock_key = f"session:{metadata.session_id}"
        ctx = self._context(metadata, locks)

        async with ctx:
            # Simulate the lock expiring and another turn taking it over mid-turn.
            await locks.release(lock_key, ctx._lock_owner)  # noqa: SLF001
            assert await locks.acquire(lock_key, "session_context:other-turn") is not None

        info = await locks.get_lock_info(lock_key)
        assert info is not None
        assert info.owner_id == "session_context:other-turn"

    async def test_heartbeat_keeps_lock_beyond_ttl(
        self, metadata: SessionMetadata, tmp_path: Path
    ) -> None:
        locks = FilesystemLockCoordinator(tmp_path / "locks", default_ttl_seconds=1)
        lock_key = f"session:{metadata.session_id}"
        ctx = self._context(metadata, locks)

        async with ctx:
            await asyncio.sleep(1.6)
            # Past the 1 s TTL: still held by this context, another owner is refused.
            assert await locks.is_locked(lock_key)
            assert await locks.acquire(lock_key, "session_context:other-turn") is None
        assert not await locks.is_locked(lock_key)

    async def test_lock_expires_without_heartbeat(
        self, metadata: SessionMetadata, tmp_path: Path
    ) -> None:
        locks = FilesystemLockCoordinator(tmp_path / "locks", default_ttl_seconds=1)
        lock_key = f"session:{metadata.session_id}"
        ctx = self._context(metadata, locks)

        async with ctx:
            # A crashed holder stops refreshing: model that by stopping the heartbeat.
            assert ctx._lock_refresh_task is not None  # noqa: SLF001
            ctx._lock_refresh_task.cancel()  # noqa: SLF001
            await asyncio.sleep(1.1)
            assert not await locks.is_locked(lock_key)
            # The stale lock is taken over by the next turn.
            assert await locks.acquire(lock_key, "session_context:other-turn") is not None
        # Exiting the stale context leaves the new holder's lock in place.
        assert await locks.is_locked(lock_key)
