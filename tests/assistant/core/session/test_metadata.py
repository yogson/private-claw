"""Tests for session metadata models."""

from datetime import UTC, datetime

from assistant.core.session.metadata import (
    SessionMetadata,
    SessionState,
    SessionStatus,
    SessionType,
)


class TestSessionMetadata:
    """Tests for SessionMetadata dataclass."""

    def test_create_metadata(self) -> None:
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            session_id="test-session-123",
            context_id="telegram:12345",
            created_at=now,
            session_type=SessionType.REGULAR,
        )

        assert metadata.session_id == "test-session-123"
        assert metadata.context_id == "telegram:12345"
        assert metadata.created_at == now
        assert metadata.session_type == SessionType.REGULAR

    def test_metadata_to_dict(self) -> None:
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            session_id="test-session",
            context_id="telegram:999",
            created_at=now,
            session_type=SessionType.LONG_RUNNING,
        )

        data = metadata.to_dict()

        assert data["session_id"] == "test-session"
        assert data["context_id"] == "telegram:999"
        assert data["created_at"] == now.isoformat()
        assert data["session_type"] == "long_running"

    def test_metadata_from_dict(self) -> None:
        now = datetime.now(UTC)
        data = {
            "session_id": "restored-session",
            "context_id": "telegram:555",
            "created_at": now.isoformat(),
            "session_type": "regular",
        }

        metadata = SessionMetadata.from_dict(data)

        assert metadata.session_id == "restored-session"
        assert metadata.context_id == "telegram:555"
        assert metadata.created_at == now
        assert metadata.session_type == SessionType.REGULAR

    def test_metadata_from_dict_default_session_type(self) -> None:
        now = datetime.now(UTC)
        data = {
            "session_id": "old-session",
            "context_id": "telegram:111",
            "created_at": now.isoformat(),
        }

        metadata = SessionMetadata.from_dict(data)

        assert metadata.session_type == SessionType.REGULAR

    def test_metadata_immutable(self) -> None:
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            session_id="test",
            context_id="telegram:1",
            created_at=now,
        )

        # SessionMetadata is frozen, so we can use it as dict key
        d = {metadata: "value"}
        assert d[metadata] == "value"


class TestSessionState:
    """Tests for SessionState dataclass."""

    def test_create_state_defaults(self) -> None:
        state = SessionState()

        assert state.status == SessionStatus.ACTIVE
        assert state.turn_count == 0
        assert state.last_activity_at is not None

    def test_create_state_with_values(self) -> None:
        now = datetime.now(UTC)
        state = SessionState(
            status=SessionStatus.SUSPENDED,
            last_activity_at=now,
            turn_count=5,
        )

        assert state.status == SessionStatus.SUSPENDED
        assert state.last_activity_at == now
        assert state.turn_count == 5

    def test_state_to_dict(self) -> None:
        now = datetime.now(UTC)
        state = SessionState(
            status=SessionStatus.ARCHIVED,
            last_activity_at=now,
            turn_count=10,
        )

        data = state.to_dict()

        assert data["status"] == "archived"
        assert data["last_activity_at"] == now.isoformat()
        assert data["turn_count"] == 10

    def test_state_from_dict(self) -> None:
        now = datetime.now(UTC)
        data = {
            "status": "suspended",
            "last_activity_at": now.isoformat(),
            "turn_count": 3,
        }

        state = SessionState.from_dict(data)

        assert state.status == SessionStatus.SUSPENDED
        assert state.last_activity_at == now
        assert state.turn_count == 3

    def test_state_from_dict_defaults(self) -> None:
        data = {}

        state = SessionState.from_dict(data)

        assert state.status == SessionStatus.ACTIVE
        assert state.turn_count == 0
        assert state.last_activity_at is not None

    def test_state_mutable(self) -> None:
        state = SessionState()

        state.status = SessionStatus.ARCHIVED
        state.turn_count = 100

        assert state.status == SessionStatus.ARCHIVED
        assert state.turn_count == 100


class TestSessionStatus:
    """Tests for SessionStatus enum."""

    def test_status_values(self) -> None:
        assert SessionStatus.ACTIVE.value == "active"
        assert SessionStatus.SUSPENDED.value == "suspended"
        assert SessionStatus.ARCHIVED.value == "archived"

    def test_status_from_string(self) -> None:
        assert SessionStatus("active") == SessionStatus.ACTIVE
        assert SessionStatus("suspended") == SessionStatus.SUSPENDED
        assert SessionStatus("archived") == SessionStatus.ARCHIVED


class TestSessionType:
    """Tests for SessionType enum."""

    def test_type_values(self) -> None:
        assert SessionType.REGULAR.value == "regular"
        assert SessionType.LONG_RUNNING.value == "long_running"

    def test_type_from_string(self) -> None:
        assert SessionType("regular") == SessionType.REGULAR
        assert SessionType("long_running") == SessionType.LONG_RUNNING
