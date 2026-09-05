"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Formats claude-agent-sdk stream messages into human-readable task-log lines.

Shared by both delegation backends so their logs read the same way:
claude_code_streaming.py gets Message objects directly from the SDK's
query(); claude_code.py parses the same object types out of raw
--output-format stream-json CLI lines via the SDK's own message_parser
(see claude_code.py's _LOG_PARSING_AVAILABLE guard).
"""

import json
from pathlib import Path

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from assistant.store.filesystem.task_log import append_log_line

logger = structlog.get_logger(__name__)

LOG_LINE_MAX_CHARS = 2000


def truncate_for_log(text: str) -> str:
    """Collapse a block's text to one single-line, length-capped log entry."""
    flat = text.replace("\n", "\\n")
    if len(flat) > LOG_LINE_MAX_CHARS:
        omitted = len(flat) - LOG_LINE_MAX_CHARS
        return f"{flat[:LOG_LINE_MAX_CHARS]}... [{omitted} more chars truncated]"
    return flat


def format_log_lines(msg: object) -> list[str]:
    """Format one SDK stream message into zero or more human-readable log lines."""
    lines: list[str] = []
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                lines.append(f"[assistant] {truncate_for_log(block.text)}")
            elif isinstance(block, ToolUseBlock):
                args = json.dumps(block.input, default=str)
                lines.append(f"[tool_use] {block.name}({truncate_for_log(args)})")
            elif isinstance(block, ThinkingBlock):
                lines.append(f"[thinking] {truncate_for_log(block.thinking)}")
    elif isinstance(msg, UserMessage):
        content = msg.content
        if isinstance(content, str):
            lines.append(f"[user] {truncate_for_log(content)}")
        else:
            for block in content:
                if isinstance(block, ToolResultBlock):
                    raw = block.content
                    text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
                    tag = "tool_result_error" if block.is_error else "tool_result"
                    lines.append(f"[{tag}] {truncate_for_log(text or '')}")
                elif isinstance(block, TextBlock):
                    lines.append(f"[user] {truncate_for_log(block.text)}")
    elif isinstance(msg, SystemMessage):
        lines.append(f"[system] {msg.subtype}")
    elif isinstance(msg, ResultMessage):
        outcome = "error" if msg.is_error else "ok"
        lines.append(f"[result] {outcome} ({msg.num_turns} turns, {msg.duration_ms}ms)")
    return lines


async def write_log_lines(log_path: Path, msg: object, task_id: str) -> None:
    """Format and append one SDK message's log lines - shared by both backends.

    Logging is observability, not part of the delegated run itself: any
    failure here (a malformed message shape, a disk error) is swallowed and
    logged rather than propagated, so a bug in logging can never fail the
    task it's merely trying to describe.
    """
    try:
        for line in format_log_lines(msg):
            await append_log_line(log_path, line)
    except Exception:
        logger.exception("subagent.log.format_failed", task_id=task_id)
