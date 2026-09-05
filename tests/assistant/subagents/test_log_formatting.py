"""Tests for the shared SDK-message-to-log-line formatting used by both
delegation backends (claude_code_streaming.py and claude_code.py)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from assistant.subagents.backends.log_formatting import (
    format_log_lines,
    truncate_for_log,
    write_log_lines,
)


def test_format_log_lines_assistant_text_and_tool_use() -> None:
    from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

    msg = AssistantMessage(
        content=[
            TextBlock(text="I'll check the failing test first."),
            ToolUseBlock(id="tu1", name="Bash", input={"command": "pytest -x"}),
        ],
        model="claude-sonnet-4-5",
    )
    lines = format_log_lines(msg)
    assert lines == [
        "[assistant] I'll check the failing test first.",
        '[tool_use] Bash({"command": "pytest -x"})',
    ]


def test_format_log_lines_thinking_block() -> None:
    from claude_agent_sdk import AssistantMessage, ThinkingBlock

    msg = AssistantMessage(
        content=[ThinkingBlock(thinking="Let me think about this.", signature="sig")],
        model="claude-sonnet-4-5",
    )
    assert format_log_lines(msg) == ["[thinking] Let me think about this."]


def test_format_log_lines_tool_result_and_error() -> None:
    from claude_agent_sdk import ToolResultBlock, UserMessage

    ok_msg = UserMessage(content=[ToolResultBlock(tool_use_id="tu1", content="1 passed")])
    assert format_log_lines(ok_msg) == ["[tool_result] 1 passed"]

    error_msg = UserMessage(
        content=[ToolResultBlock(tool_use_id="tu1", content="boom", is_error=True)]
    )
    assert format_log_lines(error_msg) == ["[tool_result_error] boom"]


def test_format_log_lines_user_string_content() -> None:
    from claude_agent_sdk import UserMessage

    assert format_log_lines(UserMessage(content="plain text")) == ["[user] plain text"]


def test_format_log_lines_system_and_result() -> None:
    from claude_agent_sdk import ResultMessage, SystemMessage

    system_msg = SystemMessage(subtype="init", data={})
    assert format_log_lines(system_msg) == ["[system] init"]

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.is_error = False
    result_msg.num_turns = 3
    result_msg.duration_ms = 1234
    assert format_log_lines(result_msg) == ["[result] ok (3 turns, 1234ms)"]


def test_format_log_lines_unknown_message_type_returns_empty() -> None:
    assert format_log_lines(object()) == []


def test_truncate_for_log_flattens_newlines_and_caps_length() -> None:
    assert truncate_for_log("line one\nline two") == "line one\\nline two"

    long_text = "x" * 3000
    truncated = truncate_for_log(long_text)
    assert len(truncated) < len(long_text)
    assert truncated.startswith("x" * 2000)
    assert "more chars truncated" in truncated


@pytest.mark.asyncio
async def test_write_log_lines_appends_formatted_lines(tmp_path: Path) -> None:
    from claude_agent_sdk import AssistantMessage, TextBlock

    log_path = tmp_path / "dlg-1.log"
    msg = AssistantMessage(content=[TextBlock(text="hi")], model="claude-sonnet-4-5")

    await write_log_lines(log_path, msg, "t1")

    assert log_path.read_text().strip() == "[assistant] hi"


@pytest.mark.asyncio
async def test_write_log_lines_swallows_formatting_errors(tmp_path: Path) -> None:
    """Both backends call this directly with no try/except of their own -
    it must be safe to call unconditionally regardless of what format_log_lines
    does with a given message."""
    log_path = tmp_path / "dlg-1.log"

    with patch(
        "assistant.subagents.backends.log_formatting.format_log_lines",
        side_effect=RuntimeError("boom"),
    ):
        await write_log_lines(log_path, object(), "t1")  # must not raise

    assert not log_path.exists()
