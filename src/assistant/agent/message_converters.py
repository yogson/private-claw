"""
Message conversion utilities for pydantic_ai agent.

Converts between raw LLM message dicts and pydantic_ai typed message objects
(ModelMessage, ModelRequest, ModelResponse, etc.).
"""

import base64
import binascii
import json
from typing import Any, cast

from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)


def _message_to_prompt_content(msg: dict[str, Any]) -> str | list[dict[str, Any]]:
    """Extract raw prompt content from a message dict for history conversion."""
    if msg.get("content_blocks"):
        return cast(list[dict[str, Any]], msg["content_blocks"])
    return str(msg.get("content", "") or "")


def _map_block_to_user_content(block: dict[str, Any]) -> UserContent | None:
    """Convert Anthropic-style block dict to pydantic_ai UserContent."""
    block_type = block.get("type")
    if block_type == "text":
        text = block.get("text", "")
        if isinstance(text, str) and text.strip():
            return text
        return None

    if block_type not in {"image", "document"}:
        return None
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    if source.get("type") != "base64":
        return None
    media_type = source.get("media_type")
    data_b64 = source.get("data")
    if not isinstance(media_type, str) or not media_type:
        return None
    if not isinstance(data_b64, str) or not data_b64:
        return None
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data:
        return None
    return BinaryContent(data=data, media_type=media_type)


def _message_to_user_prompt_content(msg: dict[str, Any]) -> str | list[UserContent]:
    """Extract current turn prompt content with typed multimodal items."""
    blocks = msg.get("content_blocks")
    if not isinstance(blocks, list) or not blocks:
        return str(msg.get("content", "") or "")

    converted: list[UserContent] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        item = _map_block_to_user_content(block)
        if item is not None:
            converted.append(item)

    if converted:
        return converted
    return str(msg.get("content", "") or "")


def _llm_messages_to_history(messages: list[dict[str, Any]]) -> list[ModelMessage]:
    """Convert LLM messages (incl. tool_use/tool_result content_blocks) to pydantic_ai format.

    Preserves tool calls and tool results for correct replay/restore of conversation context.
    """
    history: list[ModelMessage] = []
    for msg in messages:
        role = msg.get("role", "")
        content = _message_to_prompt_content(msg)
        if role == "user":
            if isinstance(content, str):
                if content.strip():
                    history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
            elif isinstance(content, list):
                tool_return_parts: list[ToolReturnPart] = []
                text_content = ""
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "tool_result":
                            tool_return_parts.append(
                                ToolReturnPart(
                                    tool_call_id=part.get("tool_use_id", ""),
                                    tool_name=part.get("tool_name", "unknown"),
                                    content=part.get("content", ""),
                                )
                            )
                        elif part.get("type") == "text":
                            text_content = part.get("text", "") or ""
                if tool_return_parts:
                    history.append(ModelRequest(parts=tool_return_parts))
                elif text_content.strip():
                    history.append(ModelRequest(parts=[UserPromptPart(content=text_content)]))
        elif role == "assistant":
            text_parts: list[str] = []
            tool_call_parts: list[ToolCallPart] = []
            if isinstance(content, str) and content.strip():
                text_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            t = part.get("text", "")
                            if t:
                                text_parts.append(t)
                        elif part.get("type") == "tool_use":
                            args = part.get("input")
                            if not isinstance(args, dict):
                                args = {}
                            tool_call_parts.append(
                                ToolCallPart(
                                    tool_call_id=part.get("id", ""),
                                    tool_name=part.get("name", ""),
                                    args=args,
                                )
                            )
            parts: list[TextPart | ToolCallPart] = []
            if text_parts:
                parts.append(TextPart(content="\n\n".join(text_parts)))
            parts.extend(tool_call_parts)
            if parts:
                history.append(ModelResponse(parts=parts))
    return history


def _parse_tool_result_content(content: str | Any) -> dict[str, Any]:
    """Parse tool return content to dict. Handles JSON string or dict."""
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            result: dict[str, Any] = json.loads(content)
            return result
        except json.JSONDecodeError:
            return {"status": "failed", "reason": f"invalid json: {content[:100]}"}
    return {"status": "failed", "reason": "unknown content type"}
