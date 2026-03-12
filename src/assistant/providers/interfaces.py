"""
Component ID: CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER

Shared provider message/response models.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class LLMMessage(BaseModel):
    """A single message in a conversation turn.

    For text-only: use content (str). For multimodal (text + images/PDFs):
    use content_blocks (list of Anthropic-style blocks). When content_blocks
    is set, it takes precedence over content.
    """

    role: MessageRole
    content: str = ""
    content_blocks: list[dict[str, Any]] | None = None


class LLMUsage(BaseModel):
    """Token usage reported by the LLM provider."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class LLMResponse(BaseModel):
    """Response contract returned by a provider adapter."""

    text: str
    model_id: str
    trace_id: str
    usage: LLMUsage | None = None
    tool_calls: list["LLMToolCall"] = Field(default_factory=list)
    assistant_content_blocks: list[dict[str, Any]] | None = None


class LLMToolCall(BaseModel):
    """Tool call emitted by provider."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
