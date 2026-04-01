"""Tests for SessionContextFactory class."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.core.session.factory import SessionContextFactory
from assistant.core.session.interfaces import SessionNotFoundError
from assistant.core.session.metadata import (
    SessionMetadata,
    SessionState,
    SessionStatus,
    SessionType,
)


@pytest.fixture
def mock_store() -> MagicMock:
    """Create a mock store facade."""
    store = MagicMock()
    store.sessions = MagicMock()
    store.locks = MagicMock()
    return store


@pytest.fixture
def mock_active_context() -> MagicMock:
    """Create a mock active session context service."""
    ctx = MagicMock()
    ctx.get_active_session = MagicMock(return_value=None)
    ctx.set_active_session = MagicMock()
    ctx.clear_active_session = MagicMock()
    return ctx


@pytest.fixture
def mock_model_context() -> MagicMock:
    """Create a mock model context service."""
    return MagicMock()


@pytest.fixture
def mock_capability_context() -> MagicMock:
    """Create a mock capability context service."""
    return MagicMock()


@pytest.fixture
def mock_metadata_store() -> MagicMock:
    """Create a mock metadata store."""
    store = MagicMock()
    store.save = AsyncMock()
    store.load = AsyncMock(return_value=None)
    store.update_state = AsyncMock(return_value=True)
    store.delete = AsyncMock(return_value=True)
    store.list_by_context = AsyncMock(return_value=[])
    return store


@pytest.fixture
def factory(
    mock_store: MagicMock,
    mock_active_context: MagicMock,
    mock_model_context: MagicMock,
    mock_capability_context: MagicMock,
    mock_metadata_store: MagicMock,
) -> SessionContextFactory:
    """Create a SessionContextFactory for testing."""
    return SessionContextFactory(
        store=mock_store,
        active_context=mock_active_context,
        model_context=mock_model_context,
        capability_context=mock_capability_context,
        metadata_store=mock_metadata_store,
    )


class TestSessionContextFactoryCreate:
    """Tests for SessionContextFactory.create()."""

    @pytest.mark.asyncio
    async def test_create_regular_session(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
        mock_active_context: MagicMock,
    ) -> None:
        ctx = await factory.create("telegram:12345")

        assert ctx.context_id == "telegram:12345"
        assert ctx.is_long_running is False
        assert ctx.metadata.session_type == "regular"
        mock_metadata_store.save.assert_called_once()
        mock_active_context.set_active_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_long_running_session(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
    ) -> None:
        ctx = await factory.create("telegram:12345", session_type=SessionType.LONG_RUNNING)

        assert ctx.is_long_running is True
        assert ctx.metadata.session_type == SessionType.LONG_RUNNING

    @pytest.mark.asyncio
    async def test_create_generates_unique_session_id(
        self,
        factory: SessionContextFactory,
    ) -> None:
        ctx1 = await factory.create("telegram:12345")
        ctx2 = await factory.create("telegram:12345")

        assert ctx1.session_id != ctx2.session_id

    @pytest.mark.asyncio
    async def test_create_sets_initial_state(
        self,
        factory: SessionContextFactory,
    ) -> None:
        ctx = await factory.create("telegram:12345")

        assert ctx.state.status == SessionStatus.ACTIVE
        assert ctx.state.turn_count == 0

    @pytest.mark.asyncio
    async def test_create_with_explicit_session_id(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
        mock_active_context: MagicMock,
    ) -> None:
        ctx = await factory.create("telegram:12345", session_id="tg:12345:custom_abc")

        assert ctx.session_id == "tg:12345:custom_abc"
        assert ctx.context_id == "telegram:12345"
        mock_metadata_store.save.assert_called_once()
        saved_metadata = mock_metadata_store.save.call_args[0][0]
        assert saved_metadata.session_id == "tg:12345:custom_abc"
        mock_active_context.set_active_session.assert_called_once_with(
            "telegram:12345", "tg:12345:custom_abc"
        )

    @pytest.mark.asyncio
    async def test_create_explicit_id_overrides_generated(
        self,
        factory: SessionContextFactory,
    ) -> None:
        ctx1 = await factory.create("telegram:12345", session_id="fixed-id")
        ctx2 = await factory.create("telegram:12345", session_id="fixed-id")

        assert ctx1.session_id == "fixed-id"
        assert ctx2.session_id == "fixed-id"


class TestSessionContextFactoryResume:
    """Tests for SessionContextFactory.resume()."""

    @pytest.mark.asyncio
    async def test_resume_existing_session(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
    ) -> None:
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            session_id="existing-session",
            context_id="telegram:999",
            created_at=now,
            session_type=SessionType.REGULAR,
        )
        state = SessionState(
            status=SessionStatus.SUSPENDED,
            last_activity_at=now,
            turn_count=5,
        )
        mock_metadata_store.load.return_value = (metadata, state)

        ctx = await factory.resume("existing-session")

        assert ctx.session_id == "existing-session"
        assert ctx.turn_count == 5
        # Suspended sessions should be reactivated
        assert ctx.state.status == SessionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_resume_not_found(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
    ) -> None:
        mock_metadata_store.load.return_value = None

        with pytest.raises(SessionNotFoundError) as exc_info:
            await factory.resume("nonexistent-session")

        assert exc_info.value.session_id == "nonexistent-session"


class TestSessionContextFactoryGetOrCreate:
    """Tests for SessionContextFactory.get_or_create()."""

    @pytest.mark.asyncio
    async def test_get_or_create_returns_existing(
        self,
        factory: SessionContextFactory,
        mock_active_context: MagicMock,
        mock_metadata_store: MagicMock,
    ) -> None:
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            session_id="active-session",
            context_id="telegram:12345",
            created_at=now,
        )
        state = SessionState(status=SessionStatus.ACTIVE)
        mock_active_context.get_active_session.return_value = "active-session"
        mock_metadata_store.load.return_value = (metadata, state)

        ctx = await factory.get_or_create("telegram:12345")

        assert ctx.session_id == "active-session"

    @pytest.mark.asyncio
    async def test_get_or_create_creates_when_no_active(
        self,
        factory: SessionContextFactory,
        mock_active_context: MagicMock,
        mock_metadata_store: MagicMock,
    ) -> None:
        mock_active_context.get_active_session.return_value = None

        ctx = await factory.get_or_create("telegram:12345")

        assert ctx.context_id == "telegram:12345"
        mock_metadata_store.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_or_create_creates_when_session_not_found(
        self,
        factory: SessionContextFactory,
        mock_active_context: MagicMock,
        mock_metadata_store: MagicMock,
    ) -> None:
        mock_active_context.get_active_session.return_value = "stale-session"
        mock_metadata_store.load.return_value = None  # Session not found

        ctx = await factory.get_or_create("telegram:12345")

        # Should create new session when active session doesn't exist
        assert ctx.context_id == "telegram:12345"
        assert ctx.session_id != "stale-session"


class TestSessionContextFactoryMetadata:
    """Tests for SessionContextFactory metadata access methods."""

    @pytest.mark.asyncio
    async def test_get_metadata(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
    ) -> None:
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            session_id="test-session",
            context_id="telegram:123",
            created_at=now,
        )
        state = SessionState()
        mock_metadata_store.load.return_value = (metadata, state)

        result = await factory.get_metadata("test-session")

        assert result == metadata

    @pytest.mark.asyncio
    async def test_get_metadata_not_found(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
    ) -> None:
        mock_metadata_store.load.return_value = None

        result = await factory.get_metadata("nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_state(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
    ) -> None:
        metadata = SessionMetadata(
            session_id="test-session",
            context_id="telegram:123",
            created_at=datetime.now(UTC),
        )
        state = SessionState(turn_count=10)
        mock_metadata_store.load.return_value = (metadata, state)

        result = await factory.get_state("test-session")

        assert result is not None
        assert result.turn_count == 10

    @pytest.mark.asyncio
    async def test_list_sessions(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
    ) -> None:
        metadata1 = SessionMetadata(
            session_id="s1", context_id="telegram:123", created_at=datetime.now(UTC)
        )
        metadata2 = SessionMetadata(
            session_id="s2", context_id="telegram:123", created_at=datetime.now(UTC)
        )
        mock_metadata_store.list_by_context.return_value = [metadata1, metadata2]

        result = await factory.list_sessions("telegram:123")

        assert len(result) == 2
        mock_metadata_store.list_by_context.assert_called_once_with("telegram:123")


class TestSessionContextFactoryArchive:
    """Tests for SessionContextFactory.archive_session()."""

    @pytest.mark.asyncio
    async def test_archive_session(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
        mock_active_context: MagicMock,
    ) -> None:
        metadata = SessionMetadata(
            session_id="to-archive",
            context_id="telegram:123",
            created_at=datetime.now(UTC),
        )
        state = SessionState(status=SessionStatus.ACTIVE)
        mock_metadata_store.load.return_value = (metadata, state)
        mock_active_context.get_active_session.return_value = "to-archive"

        result = await factory.archive_session("to-archive")

        assert result is True
        mock_metadata_store.update_state.assert_called_once()
        # Should clear active session if it was the active one
        mock_active_context.clear_active_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_archive_session_not_found(
        self,
        factory: SessionContextFactory,
        mock_metadata_store: MagicMock,
    ) -> None:
        mock_metadata_store.load.return_value = None

        result = await factory.archive_session("nonexistent")

        assert result is False
