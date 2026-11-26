"""
Delegation feedback turns must run on the model selected for the chat.

The feedback handler used to copy the capabilities override but not the model override,
so every feedback turn silently ran on the default model (and with a cold prompt cache).
Both turn producers now resolve per-chat settings through ``_apply_chat_overrides``.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
from assistant.api.delegation_feedback_handler import _build_delegation_feedback_handler
from assistant.api.orchestrator_handler import _apply_chat_overrides
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
from assistant.core.events.models import EventSource, EventType, OrchestratorEvent
from assistant.core.orchestrator.models import OrchestratorResult
from assistant.core.orchestrator.service import Orchestrator
from assistant.store.models import TaskRecord, TaskStatus

CHAT_ID = 123
DEFAULT_MODEL = "claude-sonnet-5-5"
SELECTED_MODEL = "claude-opus-5-5"


def _adapter(model_override: str | None) -> MagicMock:
    adapter = MagicMock()
    adapter.get_model_override.side_effect = lambda chat_id: (
        model_override if chat_id == CHAT_ID else None
    )
    adapter.get_capabilities_override.return_value = None
    adapter.send_response = AsyncMock(return_value=True)
    return adapter


def _finished_task() -> TaskRecord:
    return TaskRecord(
        task_id="dlg-1",
        parent_session_id=f"tg:{CHAT_ID}:abc",
        parent_turn_id="turn-1",
        task_type="delegation",
        status=TaskStatus.COMPLETED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        metadata={"trace_id": "trace-1", "requested_by_user_id": str(CHAT_ID)},
    )


async def _feedback_event(model_override: str | None) -> OrchestratorEvent:
    """Run the feedback handler against a mocked orchestrator; return the event it built."""
    orchestrator = MagicMock()
    orchestrator.execute_turn = AsyncMock(return_value=OrchestratorResult(text=""))
    handler = _build_delegation_feedback_handler(orchestrator, _adapter(model_override))
    await handler(_finished_task())
    event: OrchestratorEvent = orchestrator.execute_turn.await_args.args[0]
    return event


@pytest.mark.asyncio
async def test_feedback_event_carries_selected_model() -> None:
    event = await _feedback_event(SELECTED_MODEL)
    assert event.model_id_override == SELECTED_MODEL


@pytest.mark.asyncio
async def test_feedback_event_without_selection_has_no_override() -> None:
    event = await _feedback_event(None)
    assert event.model_id_override is None


@pytest.mark.asyncio
async def test_feedback_event_keeps_capabilities_override() -> None:
    adapter = _adapter(SELECTED_MODEL)
    adapter.get_capabilities_override.return_value = ["assistant"]
    orchestrator = MagicMock()
    orchestrator.execute_turn = AsyncMock(return_value=OrchestratorResult(text=""))
    await _build_delegation_feedback_handler(orchestrator, adapter)(_finished_task())
    event = orchestrator.execute_turn.await_args.args[0]
    assert event.capabilities_override == ["assistant"]
    assert event.model_id_override == SELECTED_MODEL


def _user_event() -> OrchestratorEvent:
    return OrchestratorEvent(
        event_id="ev-1",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=f"tg:{CHAT_ID}:abc",
        user_id=str(CHAT_ID),
        created_at=datetime.now(UTC),
        trace_id="trace-1",
        text="hi",
    )


@pytest.mark.asyncio
async def test_feedback_and_user_events_resolve_overrides_identically() -> None:
    adapter = _adapter(SELECTED_MODEL)
    user_event = _apply_chat_overrides(_user_event(), adapter, CHAT_ID, f"tg:{CHAT_ID}:abc")
    feedback_event = await _feedback_event(SELECTED_MODEL)
    assert feedback_event.model_id_override == user_event.model_id_override == SELECTED_MODEL
    assert feedback_event.capabilities_override == user_event.capabilities_override


def _runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id=DEFAULT_MODEL,
            model_allowlist=[DEFAULT_MODEL, SELECTED_MODEL],
            max_tokens_default=1024,
        ),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["assistant"], denied_capabilities=[]
        ),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(lock_ttl_seconds=30, idempotency_retention_seconds=86400),
        memory=MemoryConfig(api_key="test"),
    )


async def _model_used_by_feedback_turn(model_override: str | None) -> Any:
    """Run the feedback handler through a real Orchestrator; return the model_id of run_turn."""
    store = MagicMock()
    store.sessions.replay_for_turn = AsyncMock(return_value=[])
    store.sessions.session_exists = AsyncMock(return_value=False)
    store.sessions.get_next_sequence = AsyncMock(return_value=0)
    store.sessions.append = AsyncMock()
    store.locks.lock = MagicMock()
    store.locks.lock.return_value.__aenter__ = AsyncMock(return_value=None)
    store.locks.lock.return_value.__aexit__ = AsyncMock(return_value=None)
    idempotency = MagicMock()
    idempotency.build_key = lambda s, e: f"{s}:{e}"
    idempotency.check_and_register = AsyncMock(return_value=(False, None))
    pydantic_adapter = MagicMock(spec=PydanticAITurnAdapter)
    pydantic_adapter.run_turn = AsyncMock(return_value=("ok", [], None))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=None)
    ctx.increment_turn_count = MagicMock()
    factory = MagicMock()
    factory.resume = AsyncMock(return_value=ctx)
    factory.persist_state = AsyncMock(return_value=True)
    orchestrator = Orchestrator(
        store=store,
        config=_runtime_config(),
        idempotency=idempotency,
        pydantic_ai_adapter=pydantic_adapter,
        session_factory=factory,
    )
    handler = _build_delegation_feedback_handler(orchestrator, _adapter(model_override))
    await handler(_finished_task())
    pydantic_adapter.run_turn.assert_awaited_once()
    return pydantic_adapter.run_turn.await_args.kwargs["model_id"]


@pytest.mark.asyncio
async def test_feedback_turn_runs_on_selected_model() -> None:
    assert await _model_used_by_feedback_turn(SELECTED_MODEL) == SELECTED_MODEL


@pytest.mark.asyncio
async def test_feedback_turn_without_selection_runs_on_default_model() -> None:
    assert await _model_used_by_feedback_turn(None) == DEFAULT_MODEL


@pytest.mark.asyncio
async def test_feedback_turn_falls_back_to_default_for_unavailable_model() -> None:
    """Same fallback as the user-message path: ids outside the allowlist use the default."""
    assert await _model_used_by_feedback_turn("claude-retired-1") == DEFAULT_MODEL
