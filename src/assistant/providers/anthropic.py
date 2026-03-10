"""
Component ID: CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER

Anthropic LLM provider adapter.

Wraps the Anthropic async client, enforces model allowlist validation,
and propagates the trace ID from every LLMRequest through structured logs.
Error handling follows CMP_ERROR_PROVIDER_UNAVAILABLE with bounded retry via tenacity.
"""

import logging
from typing import cast

import anthropic
from anthropic.types import MessageParam
from tenacity import retry, stop_after_attempt, wait_exponential

from assistant.core.config.schemas import ModelConfig
from assistant.observability.correlation import get_trace_id, reset_trace_id, set_trace_id
from assistant.providers.interfaces import LLMRequest, LLMResponse, LLMUsage

logger = logging.getLogger(__name__)

_MAX_RETRY_ATTEMPTS = 3
_RETRY_WAIT_MIN_SECONDS = 1
_RETRY_WAIT_MAX_SECONDS = 8


class ModelNotAllowedError(ValueError):
    """Raised when the requested model ID is not in the configured allowlist."""


class AnthropicAdapter:
    """Anthropic LLM provider adapter implementing LLMProviderInterface.

    Validates model IDs against the configured allowlist, propagates trace IDs
    through every call, and retries transient API errors with exponential backoff.
    """

    def __init__(self, config: ModelConfig) -> None:
        self._config = config
        self._client = anthropic.AsyncAnthropic()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Execute a completion request against the Anthropic API.

        The trace_id from the request is set as the active correlation context
        for the duration of the call so all downstream log statements carry it.
        Raises ModelNotAllowedError if the requested model is not in the allowlist.
        Raises anthropic.APIError on unrecoverable provider failures after retries.
        """
        model_id = request.model_id or self._config.default_model_id
        self._validate_model(model_id)

        token = set_trace_id(request.trace_id)
        try:
            return await self._call_with_retry(request, model_id)
        finally:
            reset_trace_id(token)

    def _validate_model(self, model_id: str) -> None:
        if model_id not in self._config.model_allowlist:
            raise ModelNotAllowedError(
                f"Model '{model_id}' is not in the configured allowlist: "
                f"{self._config.model_allowlist}"
            )

    @retry(
        stop=stop_after_attempt(_MAX_RETRY_ATTEMPTS),
        wait=wait_exponential(
            min=_RETRY_WAIT_MIN_SECONDS,
            max=_RETRY_WAIT_MAX_SECONDS,
        ),
        reraise=True,
    )
    async def _call_with_retry(self, request: LLMRequest, model_id: str) -> LLMResponse:
        trace_id = get_trace_id()
        messages = cast(
            list[MessageParam],
            [{"role": m.role.value, "content": m.content} for m in request.messages],
        )
        max_tokens = request.max_tokens or self._config.max_tokens_default

        logger.info(
            "anthropic.complete.start",
            extra={
                "trace_id": trace_id,
                "model_id": model_id,
                "message_count": len(messages),
            },
        )

        try:
            if request.system:
                response = await self._client.messages.create(
                    model=model_id,
                    max_tokens=max_tokens,
                    messages=messages,
                    system=request.system,
                )
            else:
                response = await self._client.messages.create(
                    model=model_id,
                    max_tokens=max_tokens,
                    messages=messages,
                )
        except anthropic.APIStatusError as exc:
            logger.warning(
                "anthropic.complete.api_error",
                extra={"trace_id": trace_id, "status_code": exc.status_code, "error": str(exc)},
            )
            raise
        except anthropic.APIConnectionError as exc:
            logger.warning(
                "anthropic.complete.connection_error",
                extra={"trace_id": trace_id, "error": str(exc)},
            )
            raise

        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        usage: LLMUsage | None = None
        if response.usage:
            usage = LLMUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

        logger.info(
            "anthropic.complete.done",
            extra={
                "trace_id": trace_id,
                "model_id": model_id,
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
            },
        )

        return LLMResponse(
            text=text,
            model_id=model_id,
            trace_id=trace_id,
            usage=usage,
        )
