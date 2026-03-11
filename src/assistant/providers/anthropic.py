"""
Component ID: CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER

Anthropic LLM provider adapter.

Wraps the Anthropic async client, enforces model allowlist validation,
and propagates the trace ID from every LLMRequest through structured logs.
Error handling follows CMP_ERROR_PROVIDER_UNAVAILABLE: only transient failures
(connection errors, timeouts, HTTP 429 and 5xx) are retried with exponential
backoff. Deterministic client-side errors (4xx except 429) are raised immediately.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import cast

import anthropic
from anthropic.types import MessageParam
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from assistant.core.config.schemas import ModelConfig
from assistant.observability.correlation import get_trace_id, reset_trace_id, set_trace_id
from assistant.providers.interfaces import LLMRequest, LLMResponse, LLMUsage

logger = logging.getLogger(__name__)

_DEFAULT_RETRY_ATTEMPTS = 3
_RETRY_WAIT_MIN_SECONDS = 1.0
_RETRY_WAIT_MAX_SECONDS = 8.0

_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 529})


def _is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, anthropic.APIConnectionError | anthropic.APITimeoutError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in _TRANSIENT_STATUS_CODES
    return False


class ModelNotAllowedError(ValueError):
    """Raised when the requested model ID is not in the configured allowlist."""


class AnthropicAdapter:
    """Anthropic LLM provider adapter implementing LLMProviderInterface.

    Validates model IDs against the configured allowlist, propagates trace IDs
    through every call, and retries only transient API failures with exponential
    backoff. Non-transient errors (4xx except 429) are raised immediately.

    Args:
        config: Model configuration including allowlist and token defaults.
        max_retry_attempts: Total attempts (1 = no retries). Defaults to 3.
        _retry_sleep: Async sleep callable used between retries. Pass an AsyncMock
            in tests to avoid real delays.
    """

    def __init__(
        self,
        config: ModelConfig,
        *,
        max_retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
        _retry_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._client = anthropic.AsyncAnthropic()
        self._max_retry_attempts = max_retry_attempts
        self._retry_sleep: Callable[[float], Awaitable[None]] = _retry_sleep or asyncio.sleep

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Execute a completion request against the Anthropic API.

        Sets the request trace_id as the active correlation context for the call.
        Only transient failures trigger retries; deterministic 4xx errors raise immediately.
        Raises ModelNotAllowedError if the requested model is not in the allowlist.
        """
        model_id = request.model_id or self._config.default_model_id
        self._validate_model(model_id)

        token = set_trace_id(request.trace_id)
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_retry_attempts),
                wait=wait_exponential(min=_RETRY_WAIT_MIN_SECONDS, max=_RETRY_WAIT_MAX_SECONDS),
                retry=retry_if_exception(_is_transient_error),
                sleep=self._retry_sleep,
                reraise=True,
            ):
                with attempt:
                    return await self._do_call(request, model_id)
        finally:
            reset_trace_id(token)
        raise RuntimeError("unreachable")  # pragma: no cover

    def _validate_model(self, model_id: str) -> None:
        if model_id not in self._config.model_allowlist:
            raise ModelNotAllowedError(
                f"Model '{model_id}' is not in the configured allowlist: "
                f"{self._config.model_allowlist}"
            )

    async def _do_call(self, request: LLMRequest, model_id: str) -> LLMResponse:
        trace_id = get_trace_id()
        messages = cast(
            list[MessageParam],
            [{"role": m.role.value, "content": m.content} for m in request.messages],
        )
        max_tokens = (
            request.max_tokens
            if request.max_tokens is not None
            else self._config.max_tokens_default
        )

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
