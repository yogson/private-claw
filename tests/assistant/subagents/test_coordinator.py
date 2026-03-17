"""Tests for delegation coordinator."""

import asyncio
from pathlib import Path

import pytest

from assistant.core.capabilities.schemas import (
    CapabilityDefinition,
    CapabilityToolBinding,
    DelegationStageDefinition,
    DelegationWorkflowDefinition,
)
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
from assistant.store.models import TaskStatus
from assistant.subagents.contracts import DelegationStageResult, DelegationStageRun
from assistant.subagents.coordinator import DelegationCoordinator
from assistant.subagents.interfaces import DelegationBackendAdapterInterface


class _FakeBackend(DelegationBackendAdapterInterface):
    @property
    def backend_id(self) -> str:
        return "claude_code"

    async def execute_stage(self, stage: DelegationStageRun) -> DelegationStageResult:
        return DelegationStageResult(ok=True, output_text=f"{stage.stage_id}-ok")


class _FailingBackend(DelegationBackendAdapterInterface):
    @property
    def backend_id(self) -> str:
        return "claude_code"

    async def execute_stage(self, stage: DelegationStageRun) -> DelegationStageResult:
        return DelegationStageResult(ok=False, error=f"{stage.stage_id}-failed")


def _config(data_root: Path) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root=str(data_root), timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id="claude-sonnet-4-5",
            model_allowlist=["claude-sonnet-4-5"],
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


def _capabilities() -> dict[str, CapabilityDefinition]:
    workflow = DelegationWorkflowDefinition(
        workflow_id="coding_flow",
        backend="claude_code",
        stages=[
            DelegationStageDefinition(
                stage_id="implement",
                purpose="implement",
                model_id="claude-sonnet-4-5",
            )
        ],
    )
    cap = CapabilityDefinition(
        capability_id="delegation_coding",
        prompt="",
        tools=[CapabilityToolBinding(tool_id="delegate_subagent_task", enabled=True)],
        delegation=workflow,
    )
    return {"delegation_coding": cap}


@pytest.mark.asyncio
async def test_enqueue_and_execute_task(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        capability_definitions=_capabilities(),
        backends=[_FakeBackend()],
    )
    await coordinator.start()
    try:
        accepted = await coordinator.enqueue_from_tool(
            session_id="tg:123",
            turn_id="turn-1",
            trace_id="trace-1",
            user_id="u1",
            request={"objective": "Implement it", "workflow_id": "coding_flow", "tool_params": {}},
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
        assert task.result.get("summary") == "implement-ok"
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
        capability_definitions=_capabilities(),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={"objective": " ", "workflow_id": "coding_flow", "tool_params": {}},
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
        capability_definitions=_capabilities(),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={"objective": "Implement", "workflow_id": "unknown", "tool_params": {}},
    )
    assert accepted["accepted"] is False
    assert accepted["status"] == "rejected_invalid"
    await store.shutdown()


@pytest.mark.asyncio
async def test_reject_backend_not_allowlisted(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        capability_definitions=_capabilities(),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={
            "objective": "Implement",
            "workflow_id": "coding_flow",
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
        capability_definitions=_capabilities(),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={
            "objective": "Implement",
            "workflow_id": "coding_flow",
            "tool_params": {"delegation_model_allowlist": ["claude-opus-4-5"]},
        },
    )
    assert accepted["accepted"] is False
    assert accepted["status"] == "rejected_policy"
    await store.shutdown()


@pytest.mark.asyncio
async def test_reject_when_concurrency_limit_reached(tmp_path: Path) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=_config(tmp_path),
        capability_definitions=_capabilities(),
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
            "workflow_id": "coding_flow",
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
            "workflow_id": "coding_flow",
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
        capability_definitions=_capabilities(),
        backends=[_FakeBackend()],
    )
    accepted = await coordinator.enqueue_from_tool(
        session_id="tg:123",
        turn_id="turn-1",
        trace_id="trace-1",
        user_id="u1",
        request={
            "objective": "Implement",
            "workflow_id": "coding_flow",
            "max_tokens": 5000,
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
        capability_definitions=_capabilities(),
        backends=[_FailingBackend()],
    )
    await coordinator.start()
    try:
        accepted = await coordinator.enqueue_from_tool(
            session_id="tg:123",
            turn_id="turn-1",
            trace_id="trace-1",
            user_id="u1",
            request={"objective": "Implement it", "workflow_id": "coding_flow", "tool_params": {}},
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
