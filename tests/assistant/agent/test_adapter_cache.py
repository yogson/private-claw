"""
Unit tests for TurnAdapterCache (Option C — per-session adapter hot-swap).
"""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from assistant.agent.adapter_cache import TurnAdapterCache
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


def _make_config(enabled: list[str]) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id="claude-3-5-sonnet-20241022",
            model_allowlist=["claude-3-5-sonnet-20241022"],
            max_tokens_default=1024,
        ),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=enabled,
            denied_capabilities=[],
        ),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(lock_ttl_seconds=30, idempotency_retention_seconds=86400),
        memory=MemoryConfig(api_key="test"),
    )


@pytest.fixture()
def mock_adapter_cls() -> Generator[MagicMock, None, None]:
    """Patch PydanticAITurnAdapter so tests don't load YAML / call the LLM."""
    with patch("assistant.agent.adapter_cache.PydanticAITurnAdapter") as mock_cls:
        mock_cls.side_effect = lambda **kwargs: MagicMock(name="adapter")
        yield mock_cls


class TestTurnAdapterCache:
    def test_pre_populates_default_adapter_on_init(self, mock_adapter_cls: MagicMock) -> None:
        """Cache should eagerly build the default adapter during construction."""
        config = _make_config(["assistant"])
        cache = TurnAdapterCache(
            model_id="anthropic:claude-3-5-sonnet-20241022",
            max_tokens=1024,
            base_config=config,
        )
        assert cache.size == 1
        mock_adapter_cls.assert_called_once()

    def test_get_or_build_returns_same_instance_for_same_capabilities(
        self, mock_adapter_cls: MagicMock
    ) -> None:
        """Two get_or_build calls with identical capability sets must return the same object."""
        config = _make_config(["assistant"])
        cache = TurnAdapterCache(
            model_id="anthropic:claude-3-5-sonnet-20241022",
            max_tokens=1024,
            base_config=config,
        )
        first = cache.get_or_build(config)
        second = cache.get_or_build(config)
        assert first is second
        # Adapter should be built exactly once (at init time).
        assert mock_adapter_cls.call_count == 1

    def test_get_or_build_builds_new_adapter_for_different_capabilities(
        self, mock_adapter_cls: MagicMock
    ) -> None:
        """A capability set not yet in the cache must trigger a new adapter build."""
        default_config = _make_config(["assistant"])
        override_config = _make_config(["assistant", "github"])
        cache = TurnAdapterCache(
            model_id="anthropic:claude-3-5-sonnet-20241022",
            max_tokens=1024,
            base_config=default_config,
        )
        cache.get_or_build(override_config)
        assert cache.size == 2
        assert mock_adapter_cls.call_count == 2

    def test_cache_size_reflects_unique_capability_sets(self, mock_adapter_cls: MagicMock) -> None:
        default_config = _make_config(["assistant"])
        config_a = _make_config(["assistant", "github"])
        config_b = _make_config(["assistant", "deploy"])
        cache = TurnAdapterCache(
            model_id="anthropic:claude-3-5-sonnet-20241022",
            max_tokens=1024,
            base_config=default_config,
        )
        cache.get_or_build(config_a)
        cache.get_or_build(config_b)
        # Repeated call with config_a — must not increment size.
        cache.get_or_build(config_a)
        assert cache.size == 3

    def test_default_config_lookup_does_not_rebuild(self, mock_adapter_cls: MagicMock) -> None:
        """get_or_build with the default config must return the pre-warmed adapter."""
        config = _make_config(["assistant"])
        cache = TurnAdapterCache(
            model_id="anthropic:claude-3-5-sonnet-20241022",
            max_tokens=1024,
            base_config=config,
        )
        result = cache.get_or_build(config)
        # Only the init-time build should have occurred.
        assert mock_adapter_cls.call_count == 1
        assert result is not None

    def test_capability_order_does_not_affect_cache_key(self, mock_adapter_cls: MagicMock) -> None:
        """['assistant', 'github'] and ['github', 'assistant'] must share the same entry."""
        config_ab = _make_config(["assistant", "github"])
        config_ba = _make_config(["github", "assistant"])
        cache = TurnAdapterCache(
            model_id="anthropic:claude-3-5-sonnet-20241022",
            max_tokens=1024,
            base_config=config_ab,
        )
        adapter_ab = cache.get_or_build(config_ab)
        adapter_ba = cache.get_or_build(config_ba)
        assert adapter_ab is adapter_ba
        assert cache.size == 1

    def test_make_key_uses_frozenset_of_enabled_capabilities(self) -> None:
        config = _make_config(["b", "a", "c"])
        key = TurnAdapterCache._make_key(config)
        assert key == frozenset({"a", "b", "c"})
