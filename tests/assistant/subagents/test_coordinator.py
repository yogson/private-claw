"""Tests for delegation coordinator."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
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
from assistant.store.models import SessionRecordType, TaskRecord, TaskStatus
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
        records = await store.sessions.read_session("tg:123")
        updates = [
            r
            for r in records
            if r.turn_id == f"delegation-update-{task_id}"
            and r.record_type == SessionRecordType.SYSTEM_MESSAGE
        ]
        assert len(updates) == 1
        content = str(updates[0].payload.get("content", ""))
        assert content.startswith("[[DELEGATION_UPDATE]]\n")
        data = json.loads(content.split("\n", 1)[1])
        assert data["type"] == "delegation_update"
        assert data["task_id"] == task_id
        assert data["status"] == "completed"
        assert data["summary"] == "Implement it-ok"
        terminals = [
            r
            for r in records
            if r.turn_id == f"delegation-update-{task_id}"
            and r.record_type == SessionRecordType.TURN_TERMINAL
        ]
        assert len(terminals) == 1
        assert terminals[0].payload.get("status") == "completed"
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
        records = await store.sessions.read_session("tg:123")
        updates = [
            r
            for r in records
            if r.turn_id == f"delegation-update-{task_id}"
            and r.record_type == SessionRecordType.SYSTEM_MESSAGE
        ]
        assert len(updates) == 1
        content = str(updates[0].payload.get("content", ""))
        assert content.startswith("[[DELEGATION_UPDATE]]\n")
        data = json.loads(content.split("\n", 1)[1])
        assert data["status"] == "failed"
        assert "failed" in (data.get("error") or "")
        terminals = [
            r
            for r in records
            if r.turn_id == f"delegation-update-{task_id}"
            and r.record_type == SessionRecordType.TURN_TERMINAL
        ]
        assert len(terminals) == 1
        assert terminals[0].payload.get("status") == "failed"
    finally:
        await coordinator.stop()
        await store.shutdown()


@pytest.mark.asyncio
async def test_notify_completion_continues_when_session_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    completion_callback = AsyncMock()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        backends=[_FakeBackend()],
        completion_callback=completion_callback,
    )

    async def _raise_append(_task: TaskRecord) -> None:
        raise RuntimeError("append failed")

    monkeypatch.setattr(coordinator, "_append_delegation_update_message", _raise_append)
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
