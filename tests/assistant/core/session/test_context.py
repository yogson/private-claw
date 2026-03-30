"""Tests for SessionContext class."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.core.session.context import SessionContext
from assistant.core.session.metadata import (
    SessionMetadata,
    SessionState,
    SessionStatus,
)
from assistant.store.models import SessionRecord, SessionRecordType


@pytest.fixture
def mock_store() -> MagicMock:
    """Create a mock store facade."""
    store = MagicMock()
    store.sessions = MagicMock()
    store.sessions.replay_for_turn = AsyncMock(return_value=[])
    store.sessions.append = AsyncMock()
    store.locks = MagicMock()
    store.locks.acquire = AsyncMock(return_value=MagicMock())
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
        session_type="regular",
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
            session_type="long_running",
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
        mock_capability_context.get_capabilities.assert_called_once_with("test-session-abc")

    def test_set_capabilities(
        self, session_context: SessionContext, mock_capability_context: MagicMock
    ) -> None:
        session_context.set_capabilities(["cap1", "cap3"])

        mock_capability_context.set_capabilities.assert_called_once_with(
            "test-session-abc", ["cap1", "cap3"]
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
            mock_store.locks.acquire.assert_called_once()

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
        mock_store.locks.acquire.return_value = None

        from assistant.store.interfaces import LockAcquisitionError

        with pytest.raises(LockAcquisitionError):
            async with session_context:
                pass


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
