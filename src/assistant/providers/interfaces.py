"""
Component ID: CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER

LLM provider interface contract and shared request/response models.

All provider adapters must implement LLMProviderInterface so the orchestrator
can swap providers without changing call sites.
"""

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class LLMMessage(BaseModel):
    """A single message in a conversation turn."""

    role: MessageRole
    content: str


class LLMRequest(BaseModel):
    """Request contract for a single LLM completion call."""

    messages: list[LLMMessage]
    trace_id: str
    model_id: str | None = None
    system: str | None = None
    max_tokens: int | None = None


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


class LLMProviderInterface(Protocol):
    """Protocol that all LLM provider adapters must satisfy."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Execute a completion request and return the response."""
        ...
