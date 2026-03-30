"""Tests for FilesystemSessionMetadataStore."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.core.session.metadata import (
    SessionMetadata,
    SessionState,
    SessionStatus,
)
from assistant.store.filesystem.metadata import FilesystemSessionMetadataStore


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    """Create a temporary storage directory."""
    return tmp_path / "session_metadata"


@pytest.fixture
def metadata_store(storage_dir: Path) -> FilesystemSessionMetadataStore:
    """Create a metadata store for testing."""
    return FilesystemSessionMetadataStore(storage_dir)


class TestFilesystemSessionMetadataStoreSaveLoad:
    """Tests for save and load operations."""

    @pytest.mark.asyncio
    async def test_save_and_load(self, metadata_store: FilesystemSessionMetadataStore) -> None:
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            session_id="test-session-123",
            context_id="telegram:12345",
            created_at=now,
            session_type="regular",
        )
        state = SessionState(
            status=SessionStatus.ACTIVE,
            last_activity_at=now,
            turn_count=5,
        )

        await metadata_store.save(metadata, state)
        result = await metadata_store.load("test-session-123")

        assert result is not None
        loaded_metadata, loaded_state = result
        assert loaded_metadata.session_id == "test-session-123"
        assert loaded_metadata.context_id == "telegram:12345"
        assert loaded_metadata.session_type == "regular"
        assert loaded_state.status == SessionStatus.ACTIVE
        assert loaded_state.turn_count == 5

    @pytest.mark.asyncio
    async def test_load_nonexistent(self, metadata_store: FilesystemSessionMetadataStore) -> None:
        result = await metadata_store.load("nonexistent-session")
        assert result is None

    @pytest.mark.asyncio
    async def test_save_overwrites(self, metadata_store: FilesystemSessionMetadataStore) -> None:
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            session_id="test-session",
            context_id="telegram:111",
            created_at=now,
        )
        state1 = SessionState(turn_count=1)
        state2 = SessionState(turn_count=10)

        await metadata_store.save(metadata, state1)
        await metadata_store.save(metadata, state2)
        result = await metadata_store.load("test-session")

        assert result is not None
        _, loaded_state = result
        assert loaded_state.turn_count == 10


class TestFilesystemSessionMetadataStoreUpdateState:
    """Tests for update_state operation."""

    @pytest.mark.asyncio
    async def test_update_state(self, metadata_store: FilesystemSessionMetadataStore) -> None:
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            session_id="test-session",
            context_id="telegram:123",
            created_at=now,
        )
        initial_state = SessionState(turn_count=0)
        await metadata_store.save(metadata, initial_state)

        new_state = SessionState(
            status=SessionStatus.SUSPENDED,
            turn_count=5,
        )
        result = await metadata_store.update_state("test-session", new_state)

        assert result is True
        loaded = await metadata_store.load("test-session")
        assert loaded is not None
        _, loaded_state = loaded
        assert loaded_state.status == SessionStatus.SUSPENDED
        assert loaded_state.turn_count == 5

    @pytest.mark.asyncio
    async def test_update_state_not_found(
        self, metadata_store: FilesystemSessionMetadataStore
    ) -> None:
        new_state = SessionState(turn_count=5)
        result = await metadata_store.update_state("nonexistent", new_state)
        assert result is False


class TestFilesystemSessionMetadataStoreDelete:
    """Tests for delete operation."""

    @pytest.mark.asyncio
    async def test_delete(self, metadata_store: FilesystemSessionMetadataStore) -> None:
        metadata = SessionMetadata(
            session_id="to-delete",
            context_id="telegram:123",
            created_at=datetime.now(UTC),
        )
        await metadata_store.save(metadata, SessionState())

        result = await metadata_store.delete("to-delete")

        assert result is True
        assert await metadata_store.load("to-delete") is None

    @pytest.mark.asyncio
    async def test_delete_not_found(self, metadata_store: FilesystemSessionMetadataStore) -> None:
        result = await metadata_store.delete("nonexistent")
        assert result is False


class TestFilesystemSessionMetadataStoreListByContext:
    """Tests for list_by_context operation."""

    @pytest.mark.asyncio
    async def test_list_by_context(self, metadata_store: FilesystemSessionMetadataStore) -> None:
        now = datetime.now(UTC)
        # Create sessions for different contexts
        for i in range(3):
            metadata = SessionMetadata(
                session_id=f"session-context1-{i}",
                context_id="telegram:111",
                created_at=now + timedelta(minutes=i),
            )
            await metadata_store.save(metadata, SessionState())

        for i in range(2):
            metadata = SessionMetadata(
                session_id=f"session-context2-{i}",
                context_id="telegram:222",
                created_at=now,
            )
            await metadata_store.save(metadata, SessionState())

        result = await metadata_store.list_by_context("telegram:111")

        assert len(result) == 3
        # Should be sorted by creation time, newest first
        assert result[0].session_id == "session-context1-2"

    @pytest.mark.asyncio
    async def test_list_by_context_empty(
        self, metadata_store: FilesystemSessionMetadataStore
    ) -> None:
        result = await metadata_store.list_by_context("nonexistent:context")
        assert result == []


class TestFilesystemSessionMetadataStoreListByStatus:
    """Tests for list_by_status operation."""

    @pytest.mark.asyncio
    async def test_list_by_status(self, metadata_store: FilesystemSessionMetadataStore) -> None:
        now = datetime.now(UTC)
        # Create sessions with different statuses
        metadata1 = SessionMetadata(
            session_id="active-1",
            context_id="telegram:123",
            created_at=now,
        )
        await metadata_store.save(metadata1, SessionState(status=SessionStatus.ACTIVE))

        metadata2 = SessionMetadata(
            session_id="archived-1",
            context_id="telegram:123",
            created_at=now,
        )
        await metadata_store.save(metadata2, SessionState(status=SessionStatus.ARCHIVED))

        result = await metadata_store.list_by_status("archived")

        assert len(result) == 1
        assert result[0].session_id == "archived-1"

    @pytest.mark.asyncio
    async def test_list_by_status_with_context_filter(
        self, metadata_store: FilesystemSessionMetadataStore
    ) -> None:
        now = datetime.now(UTC)
        for ctx_num in [123, 456]:
            metadata = SessionMetadata(
                session_id=f"session-{ctx_num}",
                context_id=f"telegram:{ctx_num}",
                created_at=now,
            )
            await metadata_store.save(metadata, SessionState(status=SessionStatus.ACTIVE))

        result = await metadata_store.list_by_status("active", context_id="telegram:123")

        assert len(result) == 1
        assert result[0].session_id == "session-123"


class TestFilesystemSessionMetadataStoreCleanup:
    """Tests for cleanup_archived operation."""

    @pytest.mark.asyncio
    async def test_cleanup_archived(self, metadata_store: FilesystemSessionMetadataStore) -> None:
        now = datetime.now(UTC)
        # Create old archived session
        old_metadata = SessionMetadata(
            session_id="old-archived",
            context_id="telegram:123",
            created_at=now - timedelta(days=60),
        )
        old_state = SessionState(
            status=SessionStatus.ARCHIVED,
            last_activity_at=now - timedelta(days=45),  # 45 days old
        )
        await metadata_store.save(old_metadata, old_state)

        # Create recent archived session
        recent_metadata = SessionMetadata(
            session_id="recent-archived",
            context_id="telegram:123",
            created_at=now - timedelta(days=10),
        )
        recent_state = SessionState(
            status=SessionStatus.ARCHIVED,
            last_activity_at=now - timedelta(days=5),  # 5 days old
        )
        await metadata_store.save(recent_metadata, recent_state)

        # Create active session (should not be affected)
        active_metadata = SessionMetadata(
            session_id="active-session",
            context_id="telegram:123",
            created_at=now - timedelta(days=60),
        )
        active_state = SessionState(
            status=SessionStatus.ACTIVE,
            last_activity_at=now - timedelta(days=60),
        )
        await metadata_store.save(active_metadata, active_state)

        removed_count = await metadata_store.cleanup_archived(max_age_days=30)

        assert removed_count == 1
        assert await metadata_store.load("old-archived") is None
        assert await metadata_store.load("recent-archived") is not None
        assert await metadata_store.load("active-session") is not None

    @pytest.mark.asyncio
    async def test_cleanup_archived_none_to_remove(
        self, metadata_store: FilesystemSessionMetadataStore
    ) -> None:
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            session_id="recent",
            context_id="telegram:123",
            created_at=now,
        )
        await metadata_store.save(
            metadata, SessionState(status=SessionStatus.ARCHIVED, last_activity_at=now)
        )

        removed_count = await metadata_store.cleanup_archived(max_age_days=30)

        assert removed_count == 0
