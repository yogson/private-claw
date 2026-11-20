"""Orchestrator-level tests for recap_session (/wtf, issue #78)."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from assistant.core.config.schemas import CompactionConfig, RecapConfig
from assistant.core.orchestrator.service import Orchestrator
from assistant.store.filesystem.lock import FilesystemLockCoordinator
from assistant.store.filesystem.session import FilesystemSessionStore
from assistant.store.interfaces import LockAcquisitionError
from assistant.store.models import SessionRecord, SessionRecordType

SESSION_ID = "tg:123"


def _record(sequence: int, record_type: SessionRecordType, content: str) -> SessionRecord:
    return SessionRecord(
        session_id=SESSION_ID,
        sequence=sequence,
        event_id=f"evt-{sequence}",
        turn_id="t1",
        timestamp=datetime.now(UTC),
        record_type=record_type,
        payload={"message_id": f"m-{sequence}", "content": content},
    )


def _orchestrator(tmp_path: Path, recap: RecapConfig) -> tuple[Orchestrator, MagicMock]:
    """Orchestrator over real filesystem sessions/locks; session_factory is a tripwire."""
    runtime_config = MagicMock()
    runtime_config.compaction = CompactionConfig(enabled=False)
    runtime_config.recap = recap
    store = MagicMock()
    store.sessions = FilesystemSessionStore(tmp_path / "sessions")
    store.locks = FilesystemLockCoordinator(tmp_path / "locks")
    session_factory = MagicMock()
    session_factory.resume = AsyncMock(side_effect=LockAcquisitionError("busy"))
    orch = Orchestrator(
        store=store,
        config=runtime_config,
        idempotency=MagicMock(),
        session_factory=session_factory,
    )
    return orch, store


def _stub_model(seen: list[str]) -> FunctionModel:
    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append("called")
        return ModelResponse(parts=[TextPart("🎯 Building /wtf")])

    return FunctionModel(_respond)


class TestRecapSession:
    async def test_disabled_returns_none(self, tmp_path: Path) -> None:
        orch, _ = _orchestrator(tmp_path, RecapConfig(enabled=False))
        assert orch.recap_enabled is False
        assert await orch.recap_session(SESSION_ID, "trace") is None

    async def test_missing_session_returns_none_without_llm(self, tmp_path: Path) -> None:
        orch, _ = _orchestrator(tmp_path, RecapConfig())
        seen: list[str] = []
        with orch._recap_service._agent.override(model=_stub_model(seen)):  # type: ignore[union-attr]  # noqa: SLF001
            assert await orch.recap_session(SESSION_ID, "trace") is None
        assert seen == []

    async def test_answers_while_turn_lock_is_held_and_persists_nothing(
        self, tmp_path: Path
    ) -> None:
        orch, store = _orchestrator(tmp_path, RecapConfig())
        await store.sessions.append(
            [
                _record(0, SessionRecordType.USER_MESSAGE, "add /wtf"),
                _record(1, SessionRecordType.ASSISTANT_MESSAGE, "on it"),
            ]
        )
        before = await store.sessions.read_session(SESSION_ID)
        session_file = next((tmp_path / "sessions").glob("*.jsonl"))
        raw_before = session_file.read_text()

        # A running turn holds the session lock (as SessionContext does).
        lock = await store.locks.acquire(f"session:{SESSION_ID}", "session_context:running-turn")
        assert lock is not None

        seen: list[str] = []
        with orch._recap_service._agent.override(model=_stub_model(seen)):  # type: ignore[union-attr]  # noqa: SLF001
            recap = await orch.recap_session(SESSION_ID, "trace")

        assert recap == "🎯 Building /wtf"
        assert seen == ["called"]
        orch._session_factory.resume.assert_not_awaited()  # noqa: SLF001
        # The running turn still owns its lock.
        held = await store.locks.acquire(f"session:{SESSION_ID}", "someone-else")
        assert held is None
        # Recap is never written to session history.
        assert await store.sessions.read_session(SESSION_ID) == before
        assert session_file.read_text() == raw_before

    async def test_llm_failure_propagates(self, tmp_path: Path) -> None:
        orch, store = _orchestrator(tmp_path, RecapConfig())
        await store.sessions.append([_record(0, SessionRecordType.USER_MESSAGE, "hi")])

        def _boom(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise RuntimeError("down")

        with (
            orch._recap_service._agent.override(model=FunctionModel(_boom)),  # type: ignore[union-attr]  # noqa: SLF001
            pytest.raises(RuntimeError, match="down"),
        ):
            await orch.recap_session(SESSION_ID, "trace")
