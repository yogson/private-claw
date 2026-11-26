"""Turns of one session run one at a time, in arrival order, on fresh history (#99)."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesPolicyConfig,
    McpServersConfig,
    MemoryConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
    ToolsConfig,
)
from assistant.core.events.models import (
    SYSTEM_USER_ID,
    EventSource,
    EventType,
    OrchestratorEvent,
)
from assistant.core.orchestrator import Orchestrator
from assistant.core.session.factory import SessionContextFactory
from assistant.store.filesystem.lock import FilesystemLockCoordinator
from assistant.store.filesystem.metadata import FilesystemSessionMetadataStore
from assistant.store.filesystem.session import FilesystemSessionStore

_MODEL = "claude-3-5-sonnet-20241022"
_SESSION = "tg:239146894:7781ab789009"


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id=_MODEL, model_allowlist=[_MODEL], max_tokens_default=256
        ),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=["assistant"]),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(lock_ttl_seconds=30, idempotency_retention_seconds=86400),
        memory=MemoryConfig(api_key="test"),
    )


def _user_event(event_id: str, text: str, session_id: str = _SESSION) -> OrchestratorEvent:
    return OrchestratorEvent(
        event_id=event_id,
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=session_id,
        user_id="239146894",
        created_at=datetime.now(UTC),
        trace_id=f"trace-{event_id}",
        text=text,
    )


def _feedback_event(task_id: str, session_id: str = _SESSION) -> OrchestratorEvent:
    payload = {"type": "delegation_completion", "task_id": task_id, "status": "completed"}
    return OrchestratorEvent(
        event_id=f"delegation-feedback-{task_id}-completed",
        event_type=EventType.SYSTEM_CONTROL_EVENT,
        source=EventSource.SYSTEM,
        session_id=session_id,
        user_id=SYSTEM_USER_ID,
        created_at=datetime.now(UTC),
        trace_id=f"trace-{task_id}",
        text="[[DELEGATION_COMPLETED]]\n" + json.dumps(payload),
        metadata={"delegation_feedback": True, "task_id": task_id},
    )


async def _settle() -> None:
    """Let every ready task run until it blocks (file I/O runs in threads)."""
    for _ in range(5):
        await asyncio.sleep(0.005)


class _FakeModel:
    """Model whose turns block until released; records the messages each turn saw."""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.seen: dict[str, list[str]] = {}
        self.started: list[str] = []
        self.finished: list[str] = []
        self.running = 0
        self.max_running = 0
        self.fail: set[str] = set()

    def gate(self, trace_id: str) -> asyncio.Event:
        return self.gates.setdefault(trace_id, asyncio.Event())

    async def run_turn(self, **kwargs: Any) -> tuple[str, list[Any], None]:
        trace_id = kwargs["trace_id"]
        self.seen[trace_id] = [str(m["content"]) for m in kwargs["messages"]]
        self.started.append(trace_id)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await self.gate(trace_id).wait()
            if trace_id in self.fail:
                raise RuntimeError(f"model failure in {trace_id}")
        finally:
            self.running -= 1
        self.finished.append(trace_id)
        return f"reply to {trace_id}", [], None


@pytest.fixture
def model() -> _FakeModel:
    return _FakeModel()


@pytest.fixture
def sessions(tmp_path: Path) -> FilesystemSessionStore:
    return FilesystemSessionStore(tmp_path / "sessions")


@pytest.fixture
def session_factory() -> MagicMock:
    """Factory whose contexts do not lock, isolating the in-process turn queue."""
    factory = MagicMock()
    factory.calls = []

    async def _resume(session_id: str) -> MagicMock:
        factory.calls.append(("resume", session_id))
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    async def _persist(ctx: MagicMock) -> bool:
        factory.calls.append(("persist", None))
        return True

    factory.resume = AsyncMock(side_effect=_resume)
    factory.persist_state = AsyncMock(side_effect=_persist)
    return factory


def _orchestrator(
    sessions: FilesystemSessionStore,
    model: _FakeModel,
    session_factory: Any,
    locks: Any = None,
) -> Orchestrator:
    store = MagicMock()
    store.sessions = sessions
    if locks is not None:
        store.locks = locks
    idempotency = MagicMock()
    idempotency.build_key = lambda s, e: f"{s}:{e}"
    idempotency.check_and_register = AsyncMock(return_value=(False, None))
    adapter = MagicMock(spec=PydanticAITurnAdapter)
    adapter.run_turn = AsyncMock(side_effect=model.run_turn)
    return Orchestrator(
        store=store,
        config=_config(),
        idempotency=idempotency,
        pydantic_ai_adapter=adapter,
        session_factory=session_factory,
    )


async def test_feedback_turn_during_user_turn_sees_the_users_answer(
    sessions: FilesystemSessionStore, model: _FakeModel, session_factory: MagicMock
) -> None:
    """The incident: a delegation completion arrives while the user's turn is running."""
    orch = _orchestrator(sessions, model, session_factory)
    user_turn = asyncio.create_task(
        orch.execute_turn(_user_event("u1", "Принять всё по рекомендациям"))
    )
    await _settle()
    assert model.started == ["trace-u1"]

    feedback_turn = asyncio.create_task(orch.execute_turn(_feedback_event("dlg-142df53ddbc3")))
    await _settle()
    # The feedback turn waits; it has not read history or reached the model yet.
    assert model.started == ["trace-u1"]

    model.gate("trace-u1").set()
    model.gate("trace-dlg-142df53ddbc3").set()
    async with asyncio.timeout(5):
        await asyncio.gather(user_turn, feedback_turn)

    seen = model.seen["trace-dlg-142df53ddbc3"]
    assert "Принять всё по рекомендациям" in seen
    assert "reply to trace-u1" in seen
    assert seen[-1].startswith("[[DELEGATION_COMPLETED]]")
    # Persisted order is processing order: the user turn is stored before the feedback.
    stored = await sessions.read_session(_SESSION)
    u1_seq = max(r.sequence for r in stored if r.event_id == "u1")
    feedback_seq = min(
        r.sequence for r in stored if r.event_id == "delegation-feedback-dlg-142df53ddbc3-completed"
    )
    assert u1_seq < feedback_seq


async def test_overlapping_turns_run_sequentially_in_arrival_order(
    sessions: FilesystemSessionStore, model: _FakeModel, session_factory: MagicMock
) -> None:
    orch = _orchestrator(sessions, model, session_factory)
    events = [
        _user_event("u1", "first"),
        _feedback_event("dlg-1"),
        _user_event("u2", "second"),
        _feedback_event("dlg-2"),
    ]
    tasks = []
    for event in events:
        tasks.append(asyncio.create_task(orch.execute_turn(event)))
        await asyncio.sleep(0)
    await _settle()
    # Release in reverse order: completion order must still follow arrival order.
    for event in reversed(events):
        model.gate(event.trace_id).set()
    async with asyncio.timeout(5):
        await asyncio.gather(*tasks)

    expected = [e.trace_id for e in events]
    assert model.started == expected
    assert model.finished == expected
    assert model.max_running == 1
    # Each turn read session state only after the previous turn persisted it.
    assert session_factory.calls == [("resume", _SESSION), ("persist", None)] * 4
    # Each turn saw every earlier turn's reply.
    assert model.seen["trace-u2"][-1] == "second"
    assert "reply to trace-dlg-1" in model.seen["trace-u2"]
    assert "reply to trace-u2" in model.seen["trace-dlg-2"]


async def test_different_sessions_are_not_serialized(
    sessions: FilesystemSessionStore, model: _FakeModel, session_factory: MagicMock
) -> None:
    orch = _orchestrator(sessions, model, session_factory)
    a = asyncio.create_task(orch.execute_turn(_user_event("a1", "hi", session_id="tg:1:a")))
    b = asyncio.create_task(orch.execute_turn(_user_event("b1", "hi", session_id="tg:2:b")))
    await _settle()
    assert sorted(model.started) == ["trace-a1", "trace-b1"]
    assert model.running == 2
    model.gate("trace-b1").set()
    async with asyncio.timeout(5):
        await b
    model.gate("trace-a1").set()
    async with asyncio.timeout(5):
        await a


async def test_failed_turn_releases_the_slot(
    sessions: FilesystemSessionStore, model: _FakeModel, session_factory: MagicMock
) -> None:
    orch = _orchestrator(sessions, model, session_factory)
    model.fail.add("trace-u1")
    first = asyncio.create_task(orch.execute_turn(_user_event("u1", "first")))
    await _settle()
    second = asyncio.create_task(orch.execute_turn(_feedback_event("dlg-1")))
    await _settle()
    assert model.started == ["trace-u1"]
    model.gate("trace-u1").set()
    model.gate("trace-dlg-1").set()
    with pytest.raises(RuntimeError, match="model failure"):
        await first
    async with asyncio.timeout(5):
        assert await second is not None
    assert model.started == ["trace-u1", "trace-dlg-1"]


async def test_stopped_turn_releases_the_slot(
    sessions: FilesystemSessionStore, model: _FakeModel, session_factory: MagicMock
) -> None:
    """/stop cancels the running turn; the turn queued behind it runs next."""
    orch = _orchestrator(sessions, model, session_factory)
    first = asyncio.create_task(orch.execute_turn(_user_event("u1", "first")))
    await _settle()
    second = asyncio.create_task(orch.execute_turn(_feedback_event("dlg-1")))
    await _settle()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    model.gate("trace-dlg-1").set()
    async with asyncio.timeout(5):
        assert await second is not None
    assert model.started == ["trace-u1", "trace-dlg-1"]


async def test_cancelled_waiting_turn_does_not_block_the_next_one(
    sessions: FilesystemSessionStore, model: _FakeModel, session_factory: MagicMock
) -> None:
    orch = _orchestrator(sessions, model, session_factory)
    first = asyncio.create_task(orch.execute_turn(_user_event("u1", "first")))
    await _settle()
    waiting = asyncio.create_task(orch.execute_turn(_user_event("u2", "second")))
    third = asyncio.create_task(orch.execute_turn(_feedback_event("dlg-1")))
    await _settle()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    model.gate("trace-u1").set()
    model.gate("trace-dlg-1").set()
    async with asyncio.timeout(5):
        await asyncio.gather(first, third)
    assert model.started == ["trace-u1", "trace-dlg-1"]
    # The cancelled turn never read state and never ran.
    assert session_factory.calls.count(("resume", _SESSION)) == 2


async def test_queued_turn_reads_session_state_after_previous_turn_persisted_it(
    tmp_path: Path, sessions: FilesystemSessionStore, model: _FakeModel
) -> None:
    """With the real session context and file lock, no turn's state update is lost."""
    store = MagicMock()
    store.sessions = sessions
    store.locks = FilesystemLockCoordinator(tmp_path / "locks")
    factory = SessionContextFactory(
        store=store,
        active_context=MagicMock(),
        model_context=MagicMock(),
        capability_context=MagicMock(),
        metadata_store=FilesystemSessionMetadataStore(tmp_path / "meta"),
    )
    await factory.create("telegram:239146894", session_id=_SESSION)
    orch = _orchestrator(sessions, model, factory, locks=store.locks)

    first = asyncio.create_task(orch.execute_turn(_user_event("u1", "first")))
    await _settle()
    second = asyncio.create_task(orch.execute_turn(_feedback_event("dlg-1")))
    await _settle()
    model.gate("trace-u1").set()
    model.gate("trace-dlg-1").set()
    async with asyncio.timeout(5):
        await asyncio.gather(first, second)

    state = await factory.get_state(_SESSION)
    assert state is not None
    assert state.turn_count == 2
    assert "reply to trace-u1" in model.seen["trace-dlg-1"]
    assert not await store.locks.is_locked(f"session:{_SESSION}")
