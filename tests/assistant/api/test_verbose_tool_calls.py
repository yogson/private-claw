"""
Verbose mode must show every tool call the agent makes.

Covers the cases where tool calls were skipped:
- /verbose toggled while a turn runs: the state was read once at turn start, so the
  remaining tool calls of that turn (e.g. a final ``delegate_subagent_task``) were hidden;
- delegation follow-up turns (run when a sub-agent task finishes) had no notifier at all;
- a failed send was swallowed without a trace.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.api.delegation_feedback_handler import _build_delegation_feedback_handler
from assistant.api.orchestrator_handler import (
    _build_orchestrator_handler,
    _build_tool_call_notifier,
)
from assistant.channels.telegram.models import NormalizedEvent
from assistant.channels.telegram.verbose_state import VerboseStateService
from assistant.core.events.models import EventSource, EventType
from assistant.core.orchestrator.models import OrchestratorResult
from assistant.store.models import TaskRecord, TaskStatus

CHAT_ID = 123
DELEGATE_ARGS = '{"objective":"Fix the `verbose` bug","task_type":"coding"}'

Notifier = Callable[[str, str], Awaitable[None]]


def _verbose(tmp_path: Path, *, enabled: bool) -> VerboseStateService:
    state = VerboseStateService(storage_path=tmp_path / "verbose_state.json")
    if enabled:
        state.toggle(CHAT_ID)
    return state


def _sent_texts(adapter: MagicMock) -> list[str]:
    return [call.args[0].text for call in adapter.send_response.await_args_list]


def _telegram_adapter() -> MagicMock:
    adapter = MagicMock()
    for name in (
        "is_stop_request",
        "is_verbose_request",
        "is_session_new_request",
        "is_session_reset_request",
        "is_compact_request",
        "is_wtf_request",
        "is_session_resume_request",
        "is_session_resume_callback",
        "is_model_request",
        "is_model_callback_request",
        "is_capabilities_request",
        "is_capabilities_callback_request",
        "is_usage_request",
        "is_confirmation_callback",
    ):
        getattr(adapter, name).return_value = False
    adapter.get_model_override.return_value = None
    adapter.get_capabilities_override.return_value = None
    adapter.send_response = AsyncMock(return_value=True)
    return adapter


def _user_event() -> NormalizedEvent:
    return NormalizedEvent(
        event_id="ev-1",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=f"tg:{CHAT_ID}",
        user_id=str(CHAT_ID),
        created_at=datetime.now(UTC),
        trace_id="trace-1",
        text="fix it",
        metadata={"chat_id": CHAT_ID},
    )


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


@pytest.mark.asyncio
async def test_notifier_follows_verbose_toggles_between_calls(tmp_path: Path) -> None:
    verbose = _verbose(tmp_path, enabled=False)
    adapter = _telegram_adapter()
    notifier = _build_tool_call_notifier(adapter, CHAT_ID, "tg:123", "trace-1", verbose)

    await notifier("memory_search", '{"query":"a"}')
    verbose.toggle(CHAT_ID)  # on
    await notifier("delegate_subagent_task", DELEGATE_ARGS)
    verbose.toggle(CHAT_ID)  # off
    await notifier("check_subagent_status", '{"task_id":"dlg-1"}')

    texts = _sent_texts(adapter)
    assert len(texts) == 1
    assert texts[0].startswith("⚙️ `delegate_subagent_task`")
    assert "Fix the `verbose` bug" in texts[0]


@pytest.mark.asyncio
async def test_verbose_enabled_mid_turn_shows_remaining_tool_calls(tmp_path: Path) -> None:
    """/verbose sent while a turn runs: the turn's later tool calls are shown."""
    verbose = _verbose(tmp_path, enabled=False)
    adapter = _telegram_adapter()

    async def _turn(_event: Any, **kwargs: Any) -> OrchestratorResult:
        notifier: Notifier | None = kwargs["tool_call_notifier"]
        assert notifier is not None
        await notifier("cancel_subagent_task", '{"task_id":"dlg-0"}')
        verbose.toggle(CHAT_ID)  # the user enables verbose mode during the turn
        await notifier("delegate_subagent_task", DELEGATE_ARGS)
        return OrchestratorResult(text="Task is running, I will report back.")

    orchestrator = MagicMock()
    orchestrator.execute_turn = AsyncMock(side_effect=_turn)
    handler = _build_orchestrator_handler(adapter, orchestrator, None, verbose_state=verbose)

    response = await handler(_user_event())

    assert response is not None and response.text == "Task is running, I will report back."
    texts = _sent_texts(adapter)
    assert len(texts) == 1
    assert texts[0].startswith("⚙️ `delegate_subagent_task`")


@pytest.mark.asyncio
async def test_verbose_disabled_mid_turn_hides_remaining_tool_calls(tmp_path: Path) -> None:
    verbose = _verbose(tmp_path, enabled=True)
    adapter = _telegram_adapter()

    async def _turn(_event: Any, **kwargs: Any) -> OrchestratorResult:
        notifier: Notifier = kwargs["tool_call_notifier"]
        await notifier("memory_search", '{"query":"a"}')
        verbose.toggle(CHAT_ID)
        await notifier("delegate_subagent_task", DELEGATE_ARGS)
        return OrchestratorResult(text="done")

    orchestrator = MagicMock()
    orchestrator.execute_turn = AsyncMock(side_effect=_turn)
    handler = _build_orchestrator_handler(adapter, orchestrator, None, verbose_state=verbose)

    await handler(_user_event())

    texts = _sent_texts(adapter)
    assert len(texts) == 1
    assert texts[0].startswith("⚙️ `memory_search`")


@pytest.mark.asyncio
async def test_delegation_follow_up_turn_shows_tool_calls_in_verbose_mode(
    tmp_path: Path,
) -> None:
    """A sub-agent finished: the follow-up turn's tool calls (e.g. the next
    delegate_subagent_task) are shown before its final text."""
    verbose = _verbose(tmp_path, enabled=True)
    adapter = _telegram_adapter()

    async def _turn(_event: Any, **kwargs: Any) -> OrchestratorResult:
        notifier: Notifier | None = kwargs.get("tool_call_notifier")
        assert notifier is not None
        await notifier("check_subagent_status", '{"task_id":"dlg-1"}')
        await notifier("delegate_subagent_task", DELEGATE_ARGS)
        return OrchestratorResult(text="Started the next step, I will report back.")

    orchestrator = MagicMock()
    orchestrator.execute_turn = AsyncMock(side_effect=_turn)
    handler = _build_delegation_feedback_handler(orchestrator, adapter, verbose_state=verbose)

    await handler(_finished_task())

    texts = _sent_texts(adapter)
    assert len(texts) == 3
    assert texts[0].startswith("⚙️ `check_subagent_status`")
    assert texts[1].startswith("⚙️ `delegate_subagent_task`")
    assert texts[2] == "Started the next step, I will report back."
    assert all(call.kwargs["chat_id"] == CHAT_ID for call in adapter.send_response.await_args_list)


@pytest.mark.asyncio
async def test_delegation_follow_up_turn_hides_tool_calls_when_verbose_off(
    tmp_path: Path,
) -> None:
    verbose = _verbose(tmp_path, enabled=False)
    adapter = _telegram_adapter()

    async def _turn(_event: Any, **kwargs: Any) -> OrchestratorResult:
        await kwargs["tool_call_notifier"]("delegate_subagent_task", DELEGATE_ARGS)
        return OrchestratorResult(text="done")

    orchestrator = MagicMock()
    orchestrator.execute_turn = AsyncMock(side_effect=_turn)
    handler = _build_delegation_feedback_handler(orchestrator, adapter, verbose_state=verbose)

    await handler(_finished_task())

    assert _sent_texts(adapter) == ["done"]


@pytest.mark.asyncio
async def test_failed_tool_call_send_is_logged_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant.api import orchestrator_handler

    verbose = _verbose(tmp_path, enabled=True)
    adapter = _telegram_adapter()
    adapter.send_response = AsyncMock(side_effect=RuntimeError("Too Many Requests"))
    log = MagicMock()
    monkeypatch.setattr(orchestrator_handler, "logger", log)
    notifier = _build_tool_call_notifier(adapter, CHAT_ID, "tg:123", "trace-1", verbose)

    await notifier("delegate_subagent_task", DELEGATE_ARGS)

    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "telegram.verbose_tool_call.send_failed"
    assert log.warning.call_args.kwargs["tool_name"] == "delegate_subagent_task"
