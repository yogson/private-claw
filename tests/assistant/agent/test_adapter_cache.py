"""
Unit tests for TurnAdapterCache (Option C — per-session adapter hot-swap).
"""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from assistant.agent.adapter_cache import TurnAdapterCache
from assistant.core.capabilities.snapshot import CapabilitySnapshot
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


def _make_config(enabled: list[str], denied: list[str] | None = None) -> RuntimeConfig:
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
            denied_capabilities=denied or [],
        ),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(lock_ttl_seconds=30, idempotency_retention_seconds=86400),
        memory=MemoryConfig(api_key="test"),
    )


_SNAP = snapshot_of({})


def _snap(revision: int) -> CapabilitySnapshot:
    return snapshot_of({}, revision=revision)


def _cache(config: RuntimeConfig, max_size: int = 16) -> TurnAdapterCache:
    return TurnAdapterCache(
        model_id="anthropic:claude-3-5-sonnet-20241022",
        max_tokens=1024,
        base_config=config,
        snapshot=_SNAP,
        max_size=max_size,
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
        cache = _cache(_make_config(["assistant"]))
        assert cache.size == 1
        mock_adapter_cls.assert_called_once()
        assert mock_adapter_cls.call_args.kwargs["snapshot"] is _SNAP

    def test_get_or_build_returns_same_instance_for_same_capabilities(
        self, mock_adapter_cls: MagicMock
    ) -> None:
        """Two get_or_build calls with identical capability sets must return the same object."""
        config = _make_config(["assistant"])
        cache = _cache(config)
        first = cache.get_or_build(config, _SNAP)
        second = cache.get_or_build(config, _SNAP)
        assert first is second
        # Adapter should be built exactly once (at init time).
        assert mock_adapter_cls.call_count == 1

    def test_get_or_build_builds_new_adapter_for_different_capabilities(
        self, mock_adapter_cls: MagicMock
    ) -> None:
        """A capability set not yet in the cache must trigger a new adapter build."""
        cache = _cache(_make_config(["assistant"]))
        cache.get_or_build(_make_config(["assistant", "github"]), _SNAP)
        assert cache.size == 2
        assert mock_adapter_cls.call_count == 2

    def test_cache_size_reflects_unique_capability_sets(self, mock_adapter_cls: MagicMock) -> None:
        config_a = _make_config(["assistant", "github"])
        config_b = _make_config(["assistant", "deploy"])
        cache = _cache(_make_config(["assistant"]))
        cache.get_or_build(config_a, _SNAP)
        cache.get_or_build(config_b, _SNAP)
        # Repeated call with config_a — must not increment size.
        cache.get_or_build(config_a, _SNAP)
        assert cache.size == 3

    def test_capability_order_does_not_affect_cache_key(self, mock_adapter_cls: MagicMock) -> None:
        """['assistant', 'github'] and ['github', 'assistant'] must share the same entry."""
        config_ab = _make_config(["assistant", "github"])
        cache = _cache(config_ab)
        adapter_ab = cache.get_or_build(config_ab, _SNAP)
        adapter_ba = cache.get_or_build(_make_config(["github", "assistant"]), _SNAP)
        assert adapter_ab is adapter_ba
        assert cache.size == 1

    def test_make_key_includes_enabled_loaded_denied_and_revision(self) -> None:
        config = _make_config(["b", "a", "c"], denied=["c"])
        key = TurnAdapterCache._make_key(config, _snap(7))
        assert key == (frozenset({"a", "b", "c"}), (), frozenset({"c"}), 7)

    def test_different_denied_lists_get_different_adapters(
        self, mock_adapter_cls: MagicMock
    ) -> None:
        """Bug 3: the key used to ignore denied_capabilities, so these shared one adapter."""
        allowed = _make_config(["assistant", "deploy"])
        denied = _make_config(["assistant", "deploy"], denied=["deploy"])
        cache = _cache(allowed)
        assert cache.get_or_build(allowed, _SNAP) is not cache.get_or_build(denied, _SNAP)
        assert mock_adapter_cls.call_count == 2

    def test_new_revision_builds_new_adapter_and_evicts_old_revisions(
        self, mock_adapter_cls: MagicMock
    ) -> None:
        config = _make_config(["assistant"])
        other = _make_config(["assistant", "github"])
        cache = _cache(config)
        rev1 = cache.get_or_build(config, _SNAP)
        cache.get_or_build(other, _SNAP)
        assert cache.size == 2

        rev2 = cache.get_or_build(config, _snap(2))
        assert rev2 is not rev1
        assert mock_adapter_cls.call_args.kwargs["snapshot"].revision == 2
        assert cache.size == 1  # both revision-1 adapters were evicted
        assert cache.get_or_build(config, _snap(2)) is rev2

    def test_stale_revision_request_is_built_but_not_cached(
        self, mock_adapter_cls: MagicMock
    ) -> None:
        config = _make_config(["assistant"])
        cache = _cache(config)
        cache.get_or_build(config, _snap(2))
        stale = cache.get_or_build(config, _SNAP)
        assert stale is not cache.get_or_build(config, _snap(2))
        assert cache.size == 1

    def test_size_is_bounded_lru(self, mock_adapter_cls: MagicMock) -> None:
        base = _make_config(["assistant"])
        cache = _cache(base, max_size=2)
        first = cache.get_or_build(base, _SNAP)
        cache.get_or_build(_make_config(["a"]), _SNAP)
        cache.get_or_build(base, _SNAP)  # touch: base becomes most recently used
        cache.get_or_build(_make_config(["b"]), _SNAP)  # evicts ["a"]
        assert cache.size == 2
        assert cache.get_or_build(base, _SNAP) is first
        builds = mock_adapter_cls.call_count
        cache.get_or_build(_make_config(["a"]), _SNAP)
        assert mock_adapter_cls.call_count == builds + 1

    def test_adapter_is_reused_regardless_of_mcp_state(self, mock_adapter_cls: MagicMock) -> None:
        """MCP tools are listed per run by the shared toolsets, so a cached adapter is
        never rebuilt for MCP reasons."""
        config = _make_config(["assistant"])
        cache = _cache(config)
        first = cache.get_or_build(config, _SNAP)
        assert cache.get_or_build(config, _SNAP) is first
        assert mock_adapter_cls.call_count == 1
