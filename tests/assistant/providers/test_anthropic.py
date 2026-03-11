"""Tests for the Anthropic provider adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from assistant.core.config.schemas import ModelConfig, QualityRouting
from assistant.observability.correlation import get_trace_id, reset_trace_id, set_trace_id
from assistant.providers.anthropic import (
    AnthropicAdapter,
    ModelNotAllowedError,
    _is_transient_error,
)
from assistant.providers.interfaces import LLMMessage, LLMRequest, MessageRole


def _model_config(
    default: str = "claude-sonnet-4-5",
    allowlist: list[str] | None = None,
) -> ModelConfig:
    return ModelConfig(
        default_model_id=default,
        model_allowlist=allowlist or ["claude-sonnet-4-5", "claude-haiku-4-5"],
        quality_routing=QualityRouting.QUALITY_FIRST,
        max_tokens_default=1024,
    )


def _make_request(
    text: str = "Hello",
    model_id: str | None = None,
    trace_id: str = "test-trace-001",
    system: str | None = None,
    max_tokens: int | None = None,
) -> LLMRequest:
    return LLMRequest(
        messages=[LLMMessage(role=MessageRole.USER, content=text)],
        trace_id=trace_id,
        model_id=model_id,
        system=system,
        max_tokens=max_tokens,
    )


def _make_adapter(**kwargs: object) -> AnthropicAdapter:
    """Build an adapter with a no-op sleep so retry tests don't block."""
    return AnthropicAdapter(
        _model_config(),
        _retry_sleep=AsyncMock(),
        **kwargs,  # type: ignore[arg-type]
    )


def _mock_response(text: str = "Hi there", model: str = "claude-sonnet-4-5") -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5

    resp = MagicMock()
    resp.content = [block]
    resp.usage = usage
    resp.model = model
    return resp


# ---------------------------------------------------------------------------
# Transient-error predicate
# ---------------------------------------------------------------------------


def test_is_transient_error_connection_error() -> None:
    exc = anthropic.APIConnectionError(request=MagicMock())
    assert _is_transient_error(exc) is True


def test_is_transient_error_timeout() -> None:
    exc = anthropic.APITimeoutError(request=MagicMock())
    assert _is_transient_error(exc) is True


@pytest.mark.parametrize("code", [429, 500, 502, 503, 529])
def test_is_transient_error_5xx_and_429(code: int) -> None:
    exc = anthropic.APIStatusError("err", response=MagicMock(status_code=code), body={})
    assert _is_transient_error(exc) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_is_transient_error_4xx_not_429(code: int) -> None:
    exc = anthropic.APIStatusError("err", response=MagicMock(status_code=code), body={})
    assert _is_transient_error(exc) is False


def test_is_transient_error_generic_exception() -> None:
    assert _is_transient_error(ValueError("nope")) is False


# ---------------------------------------------------------------------------
# Happy-path completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_returns_response() -> None:
    adapter = _make_adapter()
    mock_resp = _mock_response("Hello back")

    with patch.object(adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)):
        result = await adapter.complete(_make_request("Hello"))

    assert result.text == "Hello back"
    assert result.model_id == "claude-sonnet-4-5"
    assert result.trace_id == "test-trace-001"
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5


@pytest.mark.asyncio
async def test_complete_uses_default_model_when_none_specified() -> None:
    adapter = AnthropicAdapter(
        _model_config(default="claude-haiku-4-5"),
        _retry_sleep=AsyncMock(),
    )
    mock_resp = _mock_response(model="claude-haiku-4-5")

    with patch.object(
        adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)
    ) as mock_create:
        await adapter.complete(_make_request(model_id=None))

    assert mock_create.call_args.kwargs["model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_complete_passes_system_prompt() -> None:
    adapter = _make_adapter()
    mock_resp = _mock_response()

    with patch.object(
        adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)
    ) as mock_create:
        await adapter.complete(_make_request(system="You are helpful."))

    assert mock_create.call_args.kwargs["system"] == "You are helpful."


@pytest.mark.asyncio
async def test_complete_without_system_prompt_excludes_system_key() -> None:
    adapter = _make_adapter()
    mock_resp = _mock_response()

    with patch.object(
        adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)
    ) as mock_create:
        await adapter.complete(_make_request(system=None))

    assert "system" not in mock_create.call_args.kwargs


@pytest.mark.asyncio
async def test_complete_handles_empty_content_blocks() -> None:
    adapter = _make_adapter()
    resp = MagicMock()
    resp.content = []
    resp.usage = None

    with patch.object(adapter._client.messages, "create", new=AsyncMock(return_value=resp)):
        result = await adapter.complete(_make_request())

    assert result.text == ""
    assert result.usage is None


# ---------------------------------------------------------------------------
# max_tokens handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_uses_explicit_max_tokens() -> None:
    adapter = _make_adapter()
    mock_resp = _mock_response()

    with patch.object(
        adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)
    ) as mock_create:
        await adapter.complete(_make_request(max_tokens=512))

    assert mock_create.call_args.kwargs["max_tokens"] == 512


@pytest.mark.asyncio
async def test_complete_falls_back_to_config_max_tokens_when_none() -> None:
    adapter = _make_adapter()
    mock_resp = _mock_response()

    with patch.object(
        adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)
    ) as mock_create:
        await adapter.complete(_make_request(max_tokens=None))

    assert mock_create.call_args.kwargs["max_tokens"] == 1024  # from _model_config default


def test_max_tokens_zero_is_invalid() -> None:
    with pytest.raises(Exception, match="greater than or equal to 1"):
        _make_request(max_tokens=0)


# ---------------------------------------------------------------------------
# Allowlist validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_raises_for_disallowed_model() -> None:
    adapter = _make_adapter()
    with pytest.raises(ModelNotAllowedError, match="gpt-4"):
        await adapter.complete(_make_request(model_id="gpt-4"))


# ---------------------------------------------------------------------------
# Correlation propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_sets_trace_id_in_context() -> None:
    adapter = _make_adapter()
    captured: list[str] = []

    async def _fake_create(**kwargs: object) -> MagicMock:
        captured.append(get_trace_id())
        return _mock_response()

    with patch.object(adapter._client.messages, "create", new=_fake_create):
        await adapter.complete(_make_request(trace_id="ctx-trace-xyz"))

    assert captured == ["ctx-trace-xyz"]


@pytest.mark.asyncio
async def test_complete_restores_previous_trace_id_after_call() -> None:
    adapter = _make_adapter()
    outer_token = set_trace_id("outer-trace")
    mock_resp = _mock_response()

    with patch.object(adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)):
        await adapter.complete(_make_request(trace_id="inner-trace"))

    assert get_trace_id() == "outer-trace"
    reset_trace_id(outer_token)


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_error_is_retried_up_to_max_attempts() -> None:
    """Transient 529 error must trigger retries until max_retry_attempts is exhausted."""
    err = anthropic.APIStatusError("overloaded", response=MagicMock(status_code=529), body={})
    adapter = _make_adapter(max_retry_attempts=3)
    mock_create = AsyncMock(side_effect=err)

    with (
        patch.object(adapter._client.messages, "create", new=mock_create),
        pytest.raises(anthropic.APIStatusError),
    ):
        await adapter.complete(_make_request())

    assert mock_create.await_count == 3


@pytest.mark.asyncio
async def test_non_transient_error_is_not_retried() -> None:
    """Deterministic 400 error must be raised immediately without any retry."""
    err = anthropic.APIStatusError("bad request", response=MagicMock(status_code=400), body={})
    adapter = _make_adapter(max_retry_attempts=3)
    mock_create = AsyncMock(side_effect=err)

    with (
        patch.object(adapter._client.messages, "create", new=mock_create),
        pytest.raises(anthropic.APIStatusError),
    ):
        await adapter.complete(_make_request())

    assert mock_create.await_count == 1


@pytest.mark.asyncio
async def test_connection_error_is_retried() -> None:
    """APIConnectionError must be treated as transient and retried."""
    err = anthropic.APIConnectionError(request=MagicMock())
    adapter = _make_adapter(max_retry_attempts=2)
    mock_create = AsyncMock(side_effect=err)

    with (
        patch.object(adapter._client.messages, "create", new=mock_create),
        pytest.raises(anthropic.APIConnectionError),
    ):
        await adapter.complete(_make_request())

    assert mock_create.await_count == 2


@pytest.mark.asyncio
async def test_succeeds_on_second_attempt_after_transient_failure() -> None:
    """Adapter must return successfully when a transient failure is followed by success."""
    err = anthropic.APIStatusError("overloaded", response=MagicMock(status_code=529), body={})
    success_resp = _mock_response("ok")
    adapter = _make_adapter(max_retry_attempts=3)
    mock_create = AsyncMock(side_effect=[err, success_resp])

    with patch.object(adapter._client.messages, "create", new=mock_create):
        result = await adapter.complete(_make_request())

    assert result.text == "ok"
    assert mock_create.await_count == 2
