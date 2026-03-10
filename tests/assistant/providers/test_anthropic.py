"""Tests for the Anthropic provider adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from assistant.core.config.schemas import ModelConfig, QualityRouting
from assistant.observability.correlation import get_trace_id, reset_trace_id, set_trace_id
from assistant.providers.anthropic import AnthropicAdapter, ModelNotAllowedError
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
) -> LLMRequest:
    return LLMRequest(
        messages=[LLMMessage(role=MessageRole.USER, content=text)],
        trace_id=trace_id,
        model_id=model_id,
        system=system,
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


@pytest.mark.asyncio
async def test_complete_returns_response() -> None:
    adapter = AnthropicAdapter(_model_config())
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
    adapter = AnthropicAdapter(_model_config(default="claude-haiku-4-5"))
    mock_resp = _mock_response(model="claude-haiku-4-5")

    with patch.object(
        adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)
    ) as mock_create:
        await adapter.complete(_make_request(model_id=None))

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_complete_passes_system_prompt() -> None:
    adapter = AnthropicAdapter(_model_config())
    mock_resp = _mock_response()

    with patch.object(
        adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)
    ) as mock_create:
        await adapter.complete(_make_request(system="You are helpful."))

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["system"] == "You are helpful."


@pytest.mark.asyncio
async def test_complete_without_system_prompt_excludes_system_key() -> None:
    adapter = AnthropicAdapter(_model_config())
    mock_resp = _mock_response()

    with patch.object(
        adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)
    ) as mock_create:
        await adapter.complete(_make_request(system=None))

    call_kwargs = mock_create.call_args.kwargs
    assert "system" not in call_kwargs


@pytest.mark.asyncio
async def test_complete_raises_for_disallowed_model() -> None:
    adapter = AnthropicAdapter(_model_config())
    req = _make_request(model_id="gpt-4")
    with pytest.raises(ModelNotAllowedError, match="gpt-4"):
        await adapter.complete(req)


@pytest.mark.asyncio
async def test_complete_sets_trace_id_in_context() -> None:
    adapter = AnthropicAdapter(_model_config())
    captured: list[str] = []

    async def _fake_create(**kwargs: object) -> MagicMock:
        captured.append(get_trace_id())
        return _mock_response()

    with patch.object(adapter._client.messages, "create", new=_fake_create):
        await adapter.complete(_make_request(trace_id="ctx-trace-xyz"))

    assert captured == ["ctx-trace-xyz"]


@pytest.mark.asyncio
async def test_complete_restores_previous_trace_id_after_call() -> None:
    adapter = AnthropicAdapter(_model_config())
    outer_token = set_trace_id("outer-trace")
    mock_resp = _mock_response()

    with patch.object(adapter._client.messages, "create", new=AsyncMock(return_value=mock_resp)):
        await adapter.complete(_make_request(trace_id="inner-trace"))

    assert get_trace_id() == "outer-trace"
    reset_trace_id(outer_token)


@pytest.mark.asyncio
async def test_complete_raises_api_status_error_after_retries() -> None:
    adapter = AnthropicAdapter(_model_config())

    err = anthropic.APIStatusError(
        "overloaded",
        response=MagicMock(status_code=529),
        body={},
    )

    with (
        patch.object(adapter._client.messages, "create", new=AsyncMock(side_effect=err)),
        patch("assistant.providers.anthropic._MAX_RETRY_ATTEMPTS", 1),
        pytest.raises(anthropic.APIStatusError),
    ):
        await adapter.complete(_make_request())


@pytest.mark.asyncio
async def test_complete_handles_empty_content_blocks() -> None:
    adapter = AnthropicAdapter(_model_config())

    resp = MagicMock()
    resp.content = []
    resp.usage = None

    with patch.object(adapter._client.messages, "create", new=AsyncMock(return_value=resp)):
        result = await adapter.complete(_make_request())

    assert result.text == ""
    assert result.usage is None
