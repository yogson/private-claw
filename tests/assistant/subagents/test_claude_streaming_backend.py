"""Tests for ClaudeCodeStreamingBackendAdapter."""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from assistant.subagents.backends.claude_code_streaming import (
    ClaudeCodeStreamingBackendAdapter,
)
from assistant.subagents.contracts import DelegationRun


def _make_request(**kwargs: Any) -> DelegationRun:
    defaults: dict[str, Any] = {
        "task_id": "t1",
        "objective": "Do something",
        "model_id": "claude-sonnet-4-5",
    }
    defaults.update(kwargs)
    return DelegationRun(**defaults)


def _make_result_msg(
    *,
    result: str = "done",
    is_error: bool = False,
    usage: dict[str, Any] | None = None,
) -> Any:
    from unittest.mock import MagicMock

    from claude_agent_sdk import ResultMessage

    msg = MagicMock(spec=ResultMessage)
    msg.result = result
    msg.is_error = is_error
    msg.usage = usage or {}
    msg.num_turns = 1
    msg.duration_ms = 100
    return msg


async def _async_iter(*items: Any) -> AsyncGenerator[Any, None]:
    for item in items:
        yield item


def _patch_query(return_value: Any) -> Any:
    """Patch query at module level in the backend module."""
    return patch(
        "assistant.subagents.backends.claude_code_streaming.query",
        return_value=return_value,
    )


def _patch_query_side_effect(side_effect: Any) -> Any:
    return patch(
        "assistant.subagents.backends.claude_code_streaming.query",
        side_effect=side_effect,
    )


@pytest.mark.asyncio
async def test_execute_returns_ok_on_success() -> None:
    result_msg = _make_result_msg(result="task complete", usage={"total_tokens": 42})

    with _patch_query(_async_iter(result_msg)):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request())

    assert result.ok is True
    assert result.output_text == "task complete"
    assert result.usage == {"total_tokens": 42}


@pytest.mark.asyncio
async def test_execute_returns_error_on_is_error() -> None:
    result_msg = _make_result_msg(result="something went wrong", is_error=True)

    with _patch_query(_async_iter(result_msg)):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request())

    assert result.ok is False
    assert "something went wrong" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_returns_error_on_sdk_exception() -> None:
    with _patch_query_side_effect(RuntimeError("subprocess not found")):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request())

    assert result.ok is False
    assert "execution failed" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_returns_error_when_sdk_missing() -> None:
    with patch(
        "assistant.subagents.backends.claude_code_streaming._SDK_AVAILABLE",
        False,
    ):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request())

    assert result.ok is False
    assert "not installed" in (result.error or "")


@pytest.mark.asyncio
async def test_relay_is_called_for_ask_user_question() -> None:
    """When a relay is registered, the AskUserQuestion can_use_tool callback
    should call it and inject the answer via updated_input."""
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    relay_called_with: list[tuple[str, list[str]]] = []

    async def _relay(question: str, options: list[str]) -> str:
        relay_called_with.append((question, options))
        return "user said yes"

    captured_can_use_tool: list[Any] = []

    async def _fake_query(
        *,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncGenerator[Any, None]:
        captured_can_use_tool.append(options.can_use_tool)
        yield _make_result_msg(result="all done")

    with _patch_query_side_effect(_fake_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        adapter.register_relay("t1", _relay)
        await adapter.execute(_make_request(task_id="t1"))

    assert len(captured_can_use_tool) == 1
    can_use_tool_fn = captured_can_use_tool[0]

    # Simulate Claude calling AskUserQuestion
    context = ToolPermissionContext(signal=None, suggestions=[])
    result = await can_use_tool_fn(
        "AskUserQuestion",
        {
            "questions": [
                {
                    "question": "Are you sure?",
                    "header": "Confirm",
                    "options": [
                        {"label": "yes", "description": "go ahead"},
                        {"label": "no", "description": ""},
                    ],
                    "multiSelect": False,
                }
            ]
        },
        context,
    )

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["answers"] == {"Are you sure?": "user said yes"}
    assert "questions" in result.updated_input
    assert relay_called_with == [("Are you sure?\n• yes — go ahead", ["yes", "no"])]


@pytest.mark.asyncio
async def test_ask_user_question_maps_numeric_and_label_replies_to_labels() -> None:
    from claude_agent_sdk import ToolPermissionContext

    replies = iter(["2", "BLUE", "something else"])

    async def _relay(question: str, options: list[str]) -> str:
        return next(replies)

    captured: list[Any] = []

    async def _fake_query(
        *, prompt: Any, options: Any, transport: Any = None
    ) -> AsyncGenerator[Any, None]:
        captured.append(options.can_use_tool)
        yield _make_result_msg()

    with _patch_query_side_effect(_fake_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        adapter.register_relay("t1", _relay)
        await adapter.execute(_make_request(task_id="t1"))

    opts = [{"label": "Red"}, {"label": "Blue"}]
    questions = [{"question": f"q{i}", "header": "h", "options": opts} for i in range(3)]
    result = await captured[0](
        "AskUserQuestion",
        {"questions": questions},
        ToolPermissionContext(signal=None, suggestions=[]),
    )
    assert result.updated_input["answers"] == {"q0": "Blue", "q1": "Blue", "q2": "something else"}


@pytest.mark.asyncio
async def test_other_tools_are_auto_approved() -> None:
    """Tools other than AskUserQuestion should be auto-approved without relay."""
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    captured_can_use_tool: list[Any] = []

    async def _fake_query(
        *,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncGenerator[Any, None]:
        captured_can_use_tool.append(options.can_use_tool)
        yield _make_result_msg()

    with _patch_query_side_effect(_fake_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        await adapter.execute(_make_request())

    can_use_tool_fn = captured_can_use_tool[0]
    context = ToolPermissionContext(signal=None, suggestions=[])
    result = await can_use_tool_fn("Bash", {"command": "ls"}, context)

    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_relay_lifecycle_register_unregister() -> None:
    adapter = ClaudeCodeStreamingBackendAdapter()

    async def _dummy(q: str, opts: list[str]) -> str:
        return "ok"

    adapter.register_relay("task-abc", _dummy)
    assert "task-abc" in adapter._task_relays

    adapter.unregister_relay("task-abc")
    assert "task-abc" not in adapter._task_relays


@pytest.mark.asyncio
async def test_relay_answer_is_forwarded_to_updated_input() -> None:
    """The relay's return value is injected into updated_input['answer'].

    Timeout handling is the coordinator's responsibility — the backend simply
    awaits whatever the relay callable returns.
    """
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    captured_can_use_tool: list[Any] = []

    async def _fake_query(
        *,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncGenerator[Any, None]:
        captured_can_use_tool.append(options.can_use_tool)
        yield _make_result_msg(result="done")

    async def _instant_relay(question: str, options: list[str]) -> str:
        return "relay_answer"

    with _patch_query_side_effect(_fake_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        adapter.register_relay("t1", _instant_relay)
        await adapter.execute(_make_request(task_id="t1"))

    assert len(captured_can_use_tool) == 1
    can_use_tool_fn = captured_can_use_tool[0]
    context = ToolPermissionContext(signal=None, suggestions=[])

    result = await can_use_tool_fn(
        "AskUserQuestion",
        {"questions": [{"question": "Hello?", "header": "h", "options": []}]},
        context,
    )
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["answers"] == {"Hello?": "relay_answer"}


@pytest.mark.asyncio
async def test_backend_params_passed_to_options() -> None:
    """Verify that effort, permission_mode, add_dirs and cwd are forwarded."""
    captured_options: list[Any] = []

    async def _fake_query(
        *,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncGenerator[Any, None]:
        captured_options.append(options)
        yield _make_result_msg()

    with _patch_query_side_effect(_fake_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        await adapter.execute(
            _make_request(
                backend_params={
                    "effort": "high",
                    "permission_mode": "bypassPermissions",
                    "add_dirs": ["src", "tests"],
                    "directory": "/tmp/project",
                    "plugin_dirs": ["/path/to/plugin-a"],
                }
            )
        )

    opts = captured_options[0]
    assert opts.effort == "high"
    assert opts.permission_mode == "bypassPermissions"
    assert opts.add_dirs == ["src", "tests"]
    assert str(opts.cwd) == "/tmp/project"
    assert opts.plugins == [{"type": "local", "path": "/path/to/plugin-a"}]


def test_backend_id() -> None:
    adapter = ClaudeCodeStreamingBackendAdapter()
    assert adapter.backend_id == "claude_code_streaming"


def test_supports_relay() -> None:
    adapter = ClaudeCodeStreamingBackendAdapter()
    assert adapter.supports_relay is True


@pytest.mark.asyncio
async def test_execute_times_out_after_timeout_seconds() -> None:
    """execute() should return an error result when the query exceeds timeout_seconds."""

    async def _raise_timeout(**kwargs: Any) -> AsyncGenerator[Any, None]:
        raise TimeoutError
        yield  # make it a generator

    with _patch_query_side_effect(_raise_timeout):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request())

    assert result.ok is False
    assert "timed out" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_timeout_via_shield_cancel_path() -> None:
    """execute() handles timeout via the shield/cancel pattern (real asyncio.wait_for path)."""

    async def _slow_query(
        *,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncGenerator[Any, None]:
        await asyncio.sleep(10)
        yield _make_result_msg()

    with _patch_query_side_effect(_slow_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request(timeout_seconds=1))

    assert result.ok is False
    assert "timed out" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_returns_ok_when_no_result_message() -> None:
    """When query yields no ResultMessage, execute() returns a failure."""

    async def _no_result_query(
        *,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncGenerator[Any, None]:
        return
        yield  # make it a generator

    with _patch_query_side_effect(_no_result_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request())

    assert result.ok is False
    assert "ResultMessage" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_returns_ok_with_empty_output() -> None:
    """An agent that produces no text output (e.g. only edits files) still succeeds."""
    result_msg = _make_result_msg(result="")

    with _patch_query(_async_iter(result_msg)):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request())

    assert result.ok is True
    assert result.output_text == ""


@pytest.mark.asyncio
async def test_ask_user_question_no_relay_is_denied_with_guidance() -> None:
    """With no relay the agent is told to decide itself instead of waiting forever."""
    from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

    captured_can_use_tool: list[Any] = []

    async def _fake_query(
        *,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncGenerator[Any, None]:
        captured_can_use_tool.append(options.can_use_tool)
        yield _make_result_msg(result="done")

    with _patch_query_side_effect(_fake_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        # No relay registered
        await adapter.execute(_make_request(task_id="t-no-relay"))

    can_use_tool_fn = captured_can_use_tool[0]
    context = ToolPermissionContext(signal=None, suggestions=[])
    result = await can_use_tool_fn(
        "AskUserQuestion",
        {"questions": [{"question": "What now?", "header": "h", "options": []}]},
        context,
    )

    assert isinstance(result, PermissionResultDeny)
    assert "choose the most reasonable option" in result.message


@pytest.mark.asyncio
async def test_outer_cancellation_terminates_inner_query_task() -> None:
    """When execute() is cancelled by the caller, the shielded _run_query task
    must be cancelled and awaited too — otherwise the claude-agent-sdk
    subprocess orphans and accumulates memory until OOM."""
    inner_started = asyncio.Event()
    inner_finally_ran = asyncio.Event()

    async def _hanging_query(
        *,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncGenerator[Any, None]:
        inner_started.set()
        try:
            await asyncio.sleep(60)  # would block forever without cleanup
        finally:
            # The SDK's real subprocess cleanup runs here. We surface it via
            # an event so the test can verify cancellation propagated inside.
            inner_finally_ran.set()
        yield _make_result_msg()  # unreachable

    with _patch_query_side_effect(_hanging_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        outer = asyncio.create_task(adapter.execute(_make_request(timeout_seconds=300)))
        # Let the inner query start before cancelling the outer.
        await asyncio.wait_for(inner_started.wait(), timeout=2.0)
        outer.cancel()
        with contextlib_suppress_cancelled():
            await outer

    # The inner task ran its finally — subprocess teardown completed,
    # nothing leaked.
    assert inner_finally_ran.is_set()


def contextlib_suppress_cancelled() -> Any:
    """Local helper: suppress CancelledError without polluting top-of-file imports."""
    import contextlib

    return contextlib.suppress(asyncio.CancelledError)


def test_build_options_includes_capability_mcp_servers() -> None:
    servers = {"ui-skills": {"type": "http", "url": "https://www.ui-skills.com/mcp"}}
    adapter = ClaudeCodeStreamingBackendAdapter(mcp_servers=servers)

    async def _noop(_tool: str, _input: dict[str, Any], _ctx: Any) -> Any:
        return None

    options = adapter._build_options(_make_request(), _noop)
    assert options.mcp_servers == servers
    # Defensive copy: mutating the options must not corrupt the adapter's config.
    options.mcp_servers.clear()
    assert adapter._mcp_servers == servers


def test_build_options_without_mcp_servers_is_empty() -> None:
    adapter = ClaudeCodeStreamingBackendAdapter()

    async def _noop(_tool: str, _input: dict[str, Any], _ctx: Any) -> Any:
        return None

    assert adapter._build_options(_make_request(), _noop).mcp_servers == {}


# ---------------------------------------------------------------------------
# Activity log writing (format_log_lines/truncate_for_log unit tests live in
# tests/assistant/subagents/test_log_formatting.py - shared with claude_code.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_writes_log_lines_when_log_path_set(tmp_path: Path) -> None:
    from claude_agent_sdk import AssistantMessage, TextBlock

    log_path = tmp_path / "dlg-1.log"

    async def _fake_query(
        *,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncGenerator[Any, None]:
        yield AssistantMessage(content=[TextBlock(text="working on it")], model="claude-sonnet-4-5")
        yield _make_result_msg(result="done")

    with _patch_query_side_effect(_fake_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request(log_path=str(log_path)))

    assert result.ok is True
    content = log_path.read_text()
    assert "[assistant] working on it" in content
    assert "[result] ok" in content


@pytest.mark.asyncio
async def test_execute_survives_log_formatting_error(tmp_path: Path) -> None:
    """A bug in log formatting is observability breakage, not a run failure -
    it must not surface as the delegated task itself having failed."""
    log_path = tmp_path / "dlg-1.log"

    async def _fake_query(
        *,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncGenerator[Any, None]:
        yield object()
        yield _make_result_msg(result="done")

    with (
        patch(
            "assistant.subagents.backends.log_formatting.format_log_lines",
            side_effect=RuntimeError("boom"),
        ),
        _patch_query_side_effect(_fake_query),
    ):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request(log_path=str(log_path)))

    assert result.ok is True
    assert result.output_text == "done"


@pytest.mark.asyncio
async def test_execute_without_log_path_writes_no_file(tmp_path: Path) -> None:
    result_msg = _make_result_msg(result="done")

    with _patch_query(_async_iter(result_msg)):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request())

    assert result.ok is True
    assert list(tmp_path.iterdir()) == []


def _task_started(task_id: str) -> Any:
    from claude_agent_sdk import TaskStartedMessage

    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description="bg",
        uuid="u",
        session_id="s",
    )


def _task_notification(task_id: str) -> Any:
    from claude_agent_sdk import TaskNotificationMessage

    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status="completed",
        output_file="/tmp/out",
        summary="done",
        uuid="u",
        session_id="s",
    )


@pytest.mark.asyncio
async def test_prompt_stays_open_until_background_tasks_finish() -> None:
    """stdin must stay open across the bg-task wait, and the final result wins."""
    prompt_closed_at_first_result: list[bool] = []
    prompt_closed_at_end: list[bool] = []

    async def _fake_query(
        *, prompt: Any, options: Any, transport: Any = None
    ) -> AsyncGenerator[Any, None]:
        prompt_done = asyncio.Event()

        async def _drain() -> None:
            async for _ in prompt:
                pass
            prompt_done.set()

        drain = asyncio.create_task(_drain())
        yield _task_started("bg1")
        yield _make_result_msg(result="waiting")
        await asyncio.sleep(0.05)
        prompt_closed_at_first_result.append(prompt_done.is_set())
        yield _task_notification("bg1")
        yield _make_result_msg(result="FINAL")
        await asyncio.wait_for(drain, timeout=1)
        prompt_closed_at_end.append(prompt_done.is_set())

    with _patch_query_side_effect(_fake_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request())

    assert prompt_closed_at_first_result == [False]
    assert prompt_closed_at_end == [True]
    assert result.ok is True
    assert result.output_text == "FINAL"


@pytest.mark.asyncio
async def test_execute_fails_when_stream_ends_with_pending_background_tasks() -> None:
    async def _fake_query(
        *, prompt: Any, options: Any, transport: Any = None
    ) -> AsyncGenerator[Any, None]:
        yield _task_started("bg1")
        yield _make_result_msg(result="waiting for the watch")

    with _patch_query_side_effect(_fake_query):
        adapter = ClaudeCodeStreamingBackendAdapter()
        result = await adapter.execute(_make_request())

    assert result.ok is False
    assert "background task" in (result.error or "")
    assert "waiting for the watch" in (result.error or "")
