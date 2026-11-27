"""
Expiry of sub-agent questions relayed to Telegram (CMP_API_FASTAPI_GATEWAY +
CMP_AGENT_SUBAGENT_COORDINATOR): the question message is edited without buttons and a
late button tap resolves nothing.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.api.orchestrator_handler import _build_orchestrator_handler
from assistant.api.question_relay_handler import (
    RelayedQuestion,
    _build_question_expired_handler,
    _build_question_relay_handler,
)
from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.models import (
    CallbackQueryMeta,
    EventSource,
    EventType,
    NormalizedEvent,
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
    ToolDefaultParams,
    ToolDefinition,
    ToolsConfig,
)
from assistant.store.facade import StoreFacade
from assistant.store.models import TaskStatus
from assistant.subagents.contracts import DelegationResult, DelegationRun
from assistant.subagents.coordinator import (
    DelegationCoordinator,
    question_expired_answer,
    question_expired_notice,
)
from assistant.subagents.interfaces import DelegationBackendAdapterInterface

CHAT_ID = 123456
SESSION = f"tg:{CHAT_ID}"
QUESTION_MESSAGE_ID = 77


@pytest.fixture(autouse=True)
def _directory_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # Workspace resolution is not under test here (see test_workspaces.py).
    monkeypatch.setattr("assistant.subagents.coordinator._DIRECTORY_REQUIRED_BACKENDS", frozenset())


class _AskingBackend(DelegationBackendAdapterInterface):
    def __init__(self) -> None:
        self._relay: Callable[[str, list[str]], Awaitable[str]] | None = None
        self.answer: str | None = None

    @property
    def backend_id(self) -> str:
        return "claude_code_streaming"

    @property
    def supports_relay(self) -> bool:
        return True

    def register_relay(
        self, task_id: str, relay: Callable[[str, list[str]], Awaitable[str]]
    ) -> None:
        self._relay = relay

    def unregister_relay(self, task_id: str) -> None:
        self._relay = None

    async def execute(self, request: DelegationRun) -> DelegationResult:
        assert self._relay is not None
        self.answer = await self._relay("Which option?", ["A", "B"])
        return DelegationResult(ok=True, output_text="done")


def _adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(
        TelegramChannelConfig(enabled=True, bot_token="12345:test-token", allowlist=[CHAT_ID])
    )
    adapter.send_response_message_ids = AsyncMock(  # type: ignore[method-assign]
        return_value=[QUESTION_MESSAGE_ID]
    )
    adapter.edit_message_text = AsyncMock()  # type: ignore[method-assign]
    return adapter


def _config(data_root: Path) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root=str(data_root), timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id="claude-sonnet-4-5", model_allowlist=["claude-sonnet-4-5"]
        ),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["delegation_coding"], denied_capabilities=[]
        ),
        tools=ToolsConfig(
            tools=[
                ToolDefinition(
                    tool_id="delegate_subagent_task",
                    entrypoint="assistant.agent.tools.delegate_subagent_task:delegate_subagent_task",
                    default_params=ToolDefaultParams(delegation_question_timeout_seconds=1),
                )
            ]
        ),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


def _button_tap(callback_data: str) -> NormalizedEvent:
    return NormalizedEvent(
        event_id="ev-cb",
        event_type=EventType.USER_CALLBACK_QUERY,
        source=EventSource.TELEGRAM,
        session_id=SESSION,
        user_id=str(CHAT_ID),
        created_at=datetime.now(UTC),
        trace_id="trace-cb",
        callback_query=CallbackQueryMeta(
            callback_id="cq-1", callback_data=callback_data, origin_message_id=QUESTION_MESSAGE_ID
        ),
        metadata={"chat_id": CHAT_ID},
    )


@pytest.mark.asyncio
async def test_expired_question_message_is_edited_and_late_tap_resolves_nothing(
    tmp_path: Path,
) -> None:
    store = StoreFacade(data_root=tmp_path)
    await store.initialize()
    adapter = _adapter()
    backend = _AskingBackend()
    coordinator = DelegationCoordinator(store=store, config=_config(tmp_path), backends=[backend])
    coordinator.set_question_relay_callback(_build_question_relay_handler(adapter))
    coordinator.set_question_expired_callback(_build_question_expired_handler(adapter))
    await coordinator.start()
    try:
        accepted = await coordinator.enqueue_from_tool(
            session_id=SESSION,
            turn_id="turn-1",
            trace_id="trace-1",
            user_id="u1",
            request={"objective": "Ask", "backend": "claude_code_streaming"},
        )
        for _ in range(80):
            task = await coordinator.get_task(accepted["task_id"])
            if task is not None and task.status == TaskStatus.COMPLETED:
                break
            await asyncio.sleep(0.05)
        assert task is not None and task.status == TaskStatus.COMPLETED

        assert backend.answer == question_expired_answer(1)
        # The question message itself now says it expired; editing drops its keyboard.
        adapter.edit_message_text.assert_awaited_once_with(  # type: ignore[attr-defined]
            CHAT_ID,
            QUESTION_MESSAGE_ID,
            f"{question_expired_notice(1)}\n\n[Delegation task] Which option?",
        )

        # A tap on one of the (old) buttons after expiry resolves nothing.
        sent = adapter.send_response_message_ids.await_args.args[0]  # type: ignore[attr-defined]
        orchestrator = MagicMock()
        orchestrator.execute_turn = AsyncMock()
        handler = _build_orchestrator_handler(adapter, orchestrator, coordinator)
        response = await handler(_button_tap(sent.actions[0].callback_data))
        assert response is not None
        assert response.text == "The question has already been answered or timed out."
        orchestrator.execute_turn.assert_not_awaited()
        assert backend.answer == question_expired_answer(1)
    finally:
        await coordinator.stop()
        await store.shutdown()


@pytest.mark.asyncio
async def test_expired_handler_with_split_question_edits_only_last_message() -> None:
    adapter = _adapter()
    expired = _build_question_expired_handler(adapter)
    handle = RelayedQuestion(chat_id=CHAT_ID, message_ids=[1, 2], text="long question")

    await expired("dlg-1", SESSION, handle, "expired")

    adapter.edit_message_text.assert_awaited_once_with(CHAT_ID, 2, "expired")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_expired_handler_without_message_ids_edits_nothing() -> None:
    adapter = _adapter()
    expired = _build_question_expired_handler(adapter)

    await expired("dlg-1", SESSION, None, "expired")
    await expired("dlg-1", SESSION, RelayedQuestion(CHAT_ID, [], "q"), "expired")

    adapter.edit_message_text.assert_not_awaited()  # type: ignore[attr-defined]
