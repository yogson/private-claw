"""Verbose mode must show every tool call the agent makes (none may be skipped)."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart

from assistant.api.delegation_feedback_handler import _build_delegation_feedback_handler
from assistant.api.orchestrator_handler import _build_tool_call_notifier
from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.egress import TelegramSendError
from assistant.channels.telegram.models import ChannelResponse
from assistant.channels.telegram.verbose_state import VerboseStateService
from assistant.core.capabilities.text_safety import utf16_len
from assistant.core.config.schemas import TelegramChannelConfig
from assistant.core.orchestrator.models import OrchestratorResult
from assistant.store.models import TaskRecord, TaskStatus

CHAT_ID = 4242
SESSION = f"tg:{CHAT_ID}"


def _adapter(send: AsyncMock | None = None) -> TelegramAdapter:
    adapter = TelegramAdapter(
        TelegramChannelConfig(enabled=True, bot_token="12345:test-token", allowlist=[CHAT_ID])
    )
    adapter.send_response = send or AsyncMock(return_value=True)  # type: ignore[method-assign]
    return adapter


def _sent(adapter: TelegramAdapter) -> list[ChannelResponse]:
    return [c.args[0] for c in adapter.send_response.await_args_list]  # type: ignore[attr-defined]


def _verbose_on(tmp_path: Path) -> VerboseStateService:
    state = VerboseStateService(storage_path=tmp_path / "verbose.json")
    state.toggle(CHAT_ID)
    return state


def _task() -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        task_id="task-v",
        parent_session_id=SESSION,
        task_type="delegation",
        status=TaskStatus.COMPLETED,
        created_at=now,
        updated_at=now,
        result={"summary": "done"},
    )


async def test_failed_send_is_retried_as_plain_text_not_dropped() -> None:
    send = AsyncMock(side_effect=[TelegramSendError(CHAT_ID, 3, "Flood control exceeded"), True])
    adapter = _adapter(send)
    notifier = _build_tool_call_notifier(adapter, CHAT_ID, SESSION, "trace")

    await notifier("memory_search", '{"query": "tea"}')

    first, retry = _sent(adapter)
    assert first.parse_mode == "Markdown"
    assert retry.plain_text is True and retry.parse_mode is None
    assert "memory_search" in retry.text and '"query": "tea"' in retry.text


async def test_notifier_never_raises_when_telegram_keeps_failing() -> None:
    adapter = _adapter(AsyncMock(side_effect=TelegramSendError(CHAT_ID, 3, "boom")))
    notifier = _build_tool_call_notifier(adapter, CHAT_ID, SESSION, "trace")

    await notifier("memory_search", "{}")

    assert len(_sent(adapter)) == 2


async def test_huge_args_fit_one_telegram_message() -> None:
    adapter = _adapter()
    notifier = _build_tool_call_notifier(adapter, CHAT_ID, SESSION, "trace")

    await notifier("write_markdown_file", '{"content": "' + "😀" * 5000 + '"}')

    (response,) = _sent(adapter)
    assert utf16_len(response.text) <= 4096
    assert "[truncated]" in response.text


async def test_non_json_args_are_still_shown() -> None:
    adapter = _adapter()
    notifier = _build_tool_call_notifier(adapter, CHAT_ID, SESSION, "trace")

    await notifier("tavily_search", '{"query": "unterminated')

    (response,) = _sent(adapter)
    assert '{"query": "unterminated' in response.text


async def test_delegation_feedback_turn_shows_tool_calls_in_verbose_mode(tmp_path: Path) -> None:
    adapter = _adapter()
    orchestrator = MagicMock()

    async def _execute_turn(event: object, **kwargs: object) -> OrchestratorResult:
        notifier = kwargs.get("tool_call_notifier")
        assert callable(notifier)
        await notifier("memory_search", '{"query": "tea"}')
        return OrchestratorResult(text="")

    orchestrator.execute_turn = AsyncMock(side_effect=_execute_turn)
    handler = _build_delegation_feedback_handler(
        orchestrator, adapter, verbose_state=_verbose_on(tmp_path)
    )

    await handler(_task())

    (response,) = _sent(adapter)
    assert "memory_search" in response.text


async def test_delegation_feedback_turn_has_no_notifier_when_verbose_off(tmp_path: Path) -> None:
    adapter = _adapter()
    orchestrator = MagicMock()
    orchestrator.execute_turn = AsyncMock(return_value=OrchestratorResult(text=""))
    handler = _build_delegation_feedback_handler(
        orchestrator,
        adapter,
        verbose_state=VerboseStateService(storage_path=tmp_path / "verbose.json"),
    )

    await handler(_task())

    assert orchestrator.execute_turn.await_args.kwargs["tool_call_notifier"] is None


async def test_agent_reports_every_parallel_and_sequential_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: each ToolCallPart of every model response reaches the notifier, in order."""
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
    from assistant.agent.tools import TurnDeps
    from assistant.core.capabilities.catalog import CapabilityCatalog
    from assistant.core.config.loader import ConfigLoader

    config_dir = Path(__file__).resolve().parents[3] / "config"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Independent of a local .env: the Telegram channel config is not needed here.
    monkeypatch.setenv("ASSISTANT_CHANNEL_TELEGRAM_ENABLED", "false")
    config = ConfigLoader(config_dir).load()
    steps = [
        [
            ToolCallPart("check_subagent_status", {"task_id": "a"}, tool_call_id="c1"),
            ToolCallPart("check_subagent_status", {"task_id": "b"}, tool_call_id="c2"),
        ],
        [ToolCallPart("check_subagent_status", {"task_id": "c"}, tool_call_id="c3")],
    ]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if steps:
            return ModelResponse(parts=[TextPart(content="working"), *steps.pop(0)])
        return ModelResponse(parts=[TextPart(content="done")])

    adapter = PydanticAITurnAdapter(
        model_id="anthropic:claude-3-5-sonnet-20241022",
        max_tokens=256,
        config=config,
        snapshot=CapabilityCatalog(config_dir).current(),
    )
    seen: list[tuple[str, str]] = []

    async def notifier(name: str, args: str) -> None:
        seen.append((name, args))

    with adapter._agent.override(model=FunctionModel(respond)):
        await adapter.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            deps=TurnDeps(tool_call_notifier=notifier),
            trace_id="trace-1",
        )

    assert [args for _, args in seen] == [
        '{"task_id":"a"}',
        '{"task_id":"b"}',
        '{"task_id":"c"}',
    ]
