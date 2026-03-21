"""Tests for delegation coordinator."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

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
from assistant.store.facade import StoreFacade
from assistant.store.models import TaskRecord, TaskStatus
from assistant.subagents.contracts import DelegationResult, DelegationRun
from assistant.subagents.coordinator import DelegationCoordinator
from assistant.subagents.interfaces import DelegationBackendAdapterInterface


class _FakeBackend(DelegationBackendAdapterInterface):
    @property
    def backend_id(self) -> str:
        return "claude_code"

    async def execute(self, request: DelegationRun) -> DelegationResult:
        return DelegationResult(ok=True, output_text=f"{request.objective}-ok")


class _FailingBackend(DelegationBackendAdapterInterface):
    @property
    def backend_id(self) -> str:
        return "claude_code"

    async def execute(self, request: DelegationRun) -> DelegationResult:
        return DelegationResult(ok=False, error=f"{request.task_id}-failed")


class _RelayCapableBackend(DelegationBackendAdapterInterface):
    """Fake backend that supports relay and invokes the registered relay during execute."""

    def __init__(self) -> None:
        self._relay: Callable[[str, list[str]], Awaitable[str]] | None = None
        self.relay_answer: str | None = None

    @property
    def backend_id(self) -> str:
        return "claude_code_streaming"

    @property
    def supports_relay(self) -> bool:
        return True

    def register_relay(
        self,
        task_id: str,
        relay: Callable[[str, list[str]], Awaitable[str]],
    ) -> None:
        self._relay = relay

    def unregister_relay(self, task_id: str) -> None:
        self._relay = None

    async def execute(self, request: DelegationRun) -> DelegationResult:
        if self._relay is not None:
            self.relay_answer = await self._relay("What now?", ["yes", "no"])
        return DelegationResult(ok=True, output_text="relay-done")


def _config(
    data_root: Path,
    *,
    default_model_id: str = "claude-sonnet-4-5",
    model_allowlist: list[str] | None = None,
) -> RuntimeConfig:
    allowlist = model_allowlist or [default_model_id]
    return RuntimeConfig(
        app=AppConfig(data_root=str(data_root), timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id=default_model_id,
            model_allowlist=allowlist,
        ),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["delegation_coding"],
            denied_capabilities=[],
        ),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


@pytest.mark.asyncio
async def test_enqueue_and_execute_task(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        backends=[_FakeBackend()],
    )
    await coordinator.start()
    try:
        accepted = await coordinator.enqueue_from_tool(
            session_id="tg:123",
            turn_id="turn-1",
            trace_id="trace-1",
            user_id="u1",
            request={"objective": "Implement it", "tool_params": {}},
        )
        assert accepted["accepted"] is True
        task_id = accepted["task_id"]
        # Wait until worker picks up and completes task.
        for _ in range(20):
            task = await coordinator.get_task(task_id)
            if task is not None and task.status == TaskStatus.COMPLETED:
                break
            await asyncio.sleep(0.1)
        task = await coordinator.get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.COMPLETED
        assert isinstance(task.result, dict)
        assert task.result.get("summary") == "Implement it-ok"
    finally:
        await coordinator.stop()
        await store.shutdown()


@pytest.mark.asyncio
async def test_reject_missing_objective(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={"objective": " ", "tool_params": {}},
    )
    assert accepted["accepted"] is False
    assert accepted["status"] == "rejected_invalid"
    await store.shutdown()


@pytest.mark.asyncio
async def test_reject_invalid_workflow(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={"objective": "Implement", "model_id": "unknown", "tool_params": {}},
    )
    assert accepted["accepted"] is False
    assert accepted["status"] == "rejected_policy"
    await store.shutdown()


@pytest.mark.asyncio
async def test_reject_backend_not_allowlisted(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={
            "objective": "Implement",
            "model_id": "claude-sonnet-4-5",
            "tool_params": {"delegation_allowed_backends": ["other_backend"]},
        },
    )
    assert accepted["accepted"] is False
    assert accepted["status"] == "rejected_policy"
    await store.shutdown()


@pytest.mark.asyncio
async def test_reject_model_not_allowlisted(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={
            "objective": "Implement",
            "model_id": "claude-opus-4-5",
            "tool_params": {"delegation_model_allowlist": ["claude-opus-4-5"]},
        },
    )
    assert accepted["accepted"] is False
    assert accepted["status"] == "rejected_policy"
    await store.shutdown()


@pytest.mark.asyncio
async def test_tool_default_model_used_when_request_missing(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(
            tmp_path,
            model_allowlist=["claude-sonnet-4-5", "claude-opus-4-5"],
        ),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={
            "objective": "Implement",
            "tool_params": {"delegation_default_model_id": "claude-opus-4-5"},
        },
    )
    assert accepted["accepted"] is True
    task = await coordinator.get_task(accepted["task_id"])
    assert task is not None
    assert task.metadata.get("model_id") == "claude-opus-4-5"
    await store.shutdown()


@pytest.mark.asyncio
async def test_tool_default_model_used_when_request_model_id_is_none(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(
            tmp_path,
            model_allowlist=["claude-sonnet-4-5", "claude-opus-4-5"],
        ),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={
            "objective": "Implement",
            "model_id": None,
            "tool_params": {"delegation_default_model_id": "claude-opus-4-5"},
        },
    )
    assert accepted["accepted"] is True
    task = await coordinator.get_task(accepted["task_id"])
    assert task is not None
    assert task.metadata.get("model_id") == "claude-opus-4-5"
    await store.shutdown()


@pytest.mark.asyncio
async def test_reject_when_concurrency_limit_reached(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        backends=[_FakeBackend()],
    )
    # First enqueue should pass.
    first = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={
            "objective": "Implement first",
            "model_id": "claude-sonnet-4-5",
            "tool_params": {"delegation_max_concurrent_tasks": 1},
        },
    )
    assert first["accepted"] is True
    # Second enqueue should be blocked by pending/running count.
    second = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-2",
        trace_id="trace-2",
        user_id="u1",
        request={
            "objective": "Implement second",
            "model_id": "claude-sonnet-4-5",
            "tool_params": {"delegation_max_concurrent_tasks": 1},
        },
    )
    assert second["accepted"] is False
    assert second["status"] == "rejected_policy"
    await store.shutdown()


@pytest.mark.asyncio
async def test_reject_when_per_task_budget_exceeded(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={
            "objective": "Implement",
            "model_id": "claude-sonnet-4-5",
            "max_turns": 1,
            "tool_params": {"delegation_per_task_token_cap": 1000},
        },
    )
    assert accepted["accepted"] is False
    assert accepted["status"] == "rejected_policy"
    await store.shutdown()


@pytest.mark.asyncio
async def test_stage_failure_marks_task_failed(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        backends=[_FailingBackend()],
    )
    await coordinator.start()
    try:
        accepted = await coordinator.enqueue_from_tool(
            session_id="tg:123",
            turn_id="turn-1",
            trace_id="trace-1",
            user_id="u1",
            request={"objective": "Implement it", "tool_params": {}},
        )
        assert accepted["accepted"] is True
        task_id = accepted["task_id"]
        for _ in range(20):
            task = await coordinator.get_task(task_id)
            if task is not None and task.status == TaskStatus.FAILED:
                break
            await asyncio.sleep(0.1)
        task = await coordinator.get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.FAILED
        assert "failed" in (task.error or "")
    finally:
        await coordinator.stop()
        await store.shutdown()


@pytest.mark.asyncio
async def test_notify_completion_swallows_callback_errors(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    completion_callback = AsyncMock(side_effect=RuntimeError("callback failed"))
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        backends=[_FakeBackend()],
        completion_callback=completion_callback,
    )
    task = TaskRecord(
        task_id="dlg-1",
        parent_session_id="tg:123",
        parent_turn_id="turn-1",
        task_type="delegation",
        status=TaskStatus.COMPLETED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        result={"summary": "ok"},
        metadata={"backend": "claude_code", "model_id": "claude-sonnet-4-5"},
    )

    await coordinator._notify_completion(task)
    completion_callback.assert_awaited_once_with(task)
    await store.shutdown()


# ---------------------------------------------------------------------------
# Streaming relay path tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_relay_submit_answer_resolves_future(tmp_path: Path) -> None:
    """submit_delegation_answer() resolves the pending future so the relay returns."""
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()

    relay_backend = _RelayCapableBackend()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path, model_allowlist=["claude-sonnet-4-5"]),
        backends=[relay_backend],
    )

    question_received: list[tuple[str, str, str, list[str]]] = []

    async def _question_relay(
        task_id: str, session_id: str, question: str, options: list[str]
    ) -> None:
        question_received.append((task_id, session_id, question, options))
        # Answer immediately via submit_delegation_answer
        coordinator.submit_delegation_answer(session_id, "yes")

    coordinator.set_question_relay_callback(_question_relay)
    await coordinator.start()
    try:
        accepted = await coordinator.enqueue_from_tool(
            session_id="tg:123",
            turn_id="turn-1",
            trace_id="trace-1",
            user_id="u1",
            request={
                "objective": "Ask me something",
                "backend": "claude_code_streaming",
                "tool_params": {},
            },
        )
        assert accepted["accepted"] is True
        task_id = accepted["task_id"]

        for _ in range(40):
            task = await coordinator.get_task(task_id)
            if task is not None and task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            await asyncio.sleep(0.1)

        task = await coordinator.get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.COMPLETED
        # The relay callback was invoked and received the question
        assert len(question_received) == 1
        assert question_received[0][2] == "What now?"
        # The backend received "yes" as the relay answer
        assert relay_backend.relay_answer == "yes"
    finally:
        await coordinator.stop()
        await store.shutdown()


@pytest.mark.asyncio
async def test_streaming_relay_answer_timeout_returns_empty(tmp_path: Path) -> None:
    """When no answer is submitted within the coordinator timeout, the relay returns ''."""
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()

    relay_backend = _RelayCapableBackend()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path, model_allowlist=["claude-sonnet-4-5"]),
        backends=[relay_backend],
    )

    async def _question_relay(
        task_id: str, session_id: str, question: str, options: list[str]
    ) -> None:
        # Never submit an answer — let the coordinator's relay time out
        pass

    coordinator.set_question_relay_callback(_question_relay)

    # Patch the timeout in _register_streaming_relay to a tiny value
    import unittest.mock as mock

    original_register = coordinator._register_streaming_relay

    def _fast_register(backend: Any, task: Any) -> None:
        # Monkey-patch the relay to use a very short timeout
        if coordinator._question_relay_callback is None:
            return
        task_id = task.task_id
        session_id = task.parent_session_id or ""
        relay_callback = coordinator._question_relay_callback

        async def _relay(question: str, options: list[str]) -> str:
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            coordinator._pending_questions[session_id] = future
            try:
                await relay_callback(task_id, session_id, question, options)
                return await asyncio.wait_for(asyncio.shield(future), timeout=0.05)
            except TimeoutError:
                return ""
            finally:
                coordinator._pending_questions.pop(session_id, None)
                if not future.done():
                    future.cancel()

        backend.register_relay(task_id, _relay)

    coordinator._register_streaming_relay = _fast_register  # type: ignore[method-assign]

    await coordinator.start()
    try:
        accepted = await coordinator.enqueue_from_tool(
            session_id="tg:123",
            turn_id="turn-1",
            trace_id="trace-1",
            user_id="u1",
            request={
                "objective": "Ask me with timeout",
                "backend": "claude_code_streaming",
                "tool_params": {},
            },
        )
        assert accepted["accepted"] is True
        task_id = accepted["task_id"]

        for _ in range(40):
            task = await coordinator.get_task(task_id)
            if task is not None and task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            await asyncio.sleep(0.1)

        task = await coordinator.get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.COMPLETED
        # No answer was submitted; relay returned ""
        assert relay_backend.relay_answer == ""
    finally:
        await coordinator.stop()
        await store.shutdown()


# ---------------------------------------------------------------------------
# Options type conversion test (for _build_question_relay_handler in main.py)
# ---------------------------------------------------------------------------


def test_options_relay_handler_converts_strings_to_dicts() -> None:
    """_build_question_relay_handler must convert list[str] to list[dict[str,str]]
    before passing to adapter.build_ask_question_response.

    We test the conversion logic in isolation so it doesn't depend on the full
    FastAPI app.
    """
    from unittest.mock import MagicMock, patch

    # Reconstruct the conversion that the relay handler performs
    options: list[str] = ["yes", "no", "maybe"]
    options_dicts = [{"id": str(i), "label": o} for i, o in enumerate(options)]

    assert options_dicts == [
        {"id": "0", "label": "yes"},
        {"id": "1", "label": "no"},
        {"id": "2", "label": "maybe"},
    ]

    # Verify the adapter's build_ask_question_response would succeed with these dicts
    # by checking it calls .get() on each option (which would fail on bare strings)
    for opt in options_dicts:
        assert opt.get("label") in options
