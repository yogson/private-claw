"""PydanticAITurnAdapter: prompt from one capability snapshot, current date per turn."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from assistant.agent.adapter_cache import TurnAdapterCache
from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
from assistant.agent.tools import TurnDeps
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesPolicyConfig,
    McpServersConfig,
    MemoryConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
    ToolsConfig,
)
from tests.assistant.capability_helpers import snapshot_of

_MODEL = "anthropic:claude-3-5-sonnet-20241022"


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=["a"]),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


@pytest.fixture(autouse=True)
def _anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Anthropic provider is built eagerly; no request is ever sent.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def test_system_prompt_comes_from_snapshot_and_has_no_date() -> None:
    snapshot = snapshot_of({"a": CapabilityDefinition(capability_id="a", prompt="PROMPT-A")})
    adapter = PydanticAITurnAdapter(_MODEL, 256, config=_config(), snapshot=snapshot)
    assert "PROMPT-A" in adapter.system_prompt
    assert "Today's date" not in adapter.system_prompt
    assert adapter.capability_revision == 1


def test_adapter_requires_snapshot() -> None:
    with pytest.raises(ValueError, match="snapshot"):
        PydanticAITurnAdapter(_MODEL, 256, config=_config())


async def test_date_is_computed_per_turn_on_a_cached_adapter() -> None:
    """Bug 1: the date used to be baked into the cached prompt and went stale after midnight."""
    now = [datetime(2026, 3, 1, 23, 59, tzinfo=UTC)]
    seen: list[str | None] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions)
        return ModelResponse(parts=[TextPart(content="ok")])

    config = _config()
    snapshot = snapshot_of({"a": CapabilityDefinition(capability_id="a", prompt="PROMPT-A")})
    cache = TurnAdapterCache(_MODEL, 256, base_config=config, snapshot=snapshot)
    adapter = cache.get_or_build(config, snapshot)
    adapter._clock = lambda: now[0]  # frozen clock
    prompt_before = adapter.system_prompt

    deps = TurnDeps()
    with adapter._agent.override(model=FunctionModel(respond)):
        await adapter.run_turn(
            messages=[{"role": "user", "content": "hi"}], deps=deps, trace_id="t"
        )
        now[0] += timedelta(days=1)
        assert cache.get_or_build(config, snapshot) is adapter  # same cached adapter
        await adapter.run_turn(
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "again"},
            ],
            deps=deps,
            trace_id="t",
        )

    assert seen == ["Today's date is March 01, 2026.", "Today's date is March 02, 2026."]
    assert adapter.system_prompt == prompt_before  # static prompt stays byte-stable
