"""Loadable-capability index in the system prompt, implicit load tools, adapter cache keys."""

from typing import Any

import pytest
from pydantic_ai import RunContext

from assistant.agent.adapter_cache import TurnAdapterCache
from assistant.agent.deps import TurnDeps
from assistant.agent.system_prompt_builder import INDEX_HEADING, _compose_system_prompt
from assistant.agent.tools.registry import (
    active_capabilities_for,
    agent_tool_name,
    build_incremental_toolset,
    get_agent_tools,
)
from assistant.core.capabilities.resolver import resolve_effective_capabilities
from assistant.core.capabilities.schemas import CapabilityDefinition, CapabilityToolBinding
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
    ToolDefinition,
    ToolsConfig,
)
from tests.assistant.capability_helpers import snapshot_of

_MODEL = "claude-3-5-sonnet-20241022"
_HERE = "tests.assistant.agent.test_capability_index_prompt"


async def notes_tool(ctx: RunContext[TurnDeps]) -> str:
    """Read notes."""
    return "notes"


async def gh_tool(ctx: RunContext[TurnDeps]) -> str:
    """Run gh."""
    return "gh"


async def base_tool(ctx: RunContext[TurnDeps]) -> str:
    """Base tool."""
    return "base"


def _cap(cap_id: str, tool: str | None = None, **kwargs: Any) -> CapabilityDefinition:
    tools = [CapabilityToolBinding(tool_id=tool)] if tool else []
    return CapabilityDefinition(
        capability_id=cap_id, prompt=f"PROMPT-{cap_id}", tools=tools, **kwargs
    )


DEFS = {
    "assistant": _cap("assistant", "base_tool"),
    "zeta": _cap("zeta"),
    "notes": _cap("notes", "notes_tool", loadable=True, description="Apple Notes"),
    "deploy": _cap(
        "deploy", loadable=True, nested_capabilities=["gh"], description="Deploy services"
    ),
    "gh": _cap("gh", "gh_tool"),
    "hidden": _cap("hidden", "notes_tool"),
}


def _tools(*, loading: bool = True) -> ToolsConfig:
    tools = [
        ToolDefinition(tool_id="base_tool", entrypoint=f"{_HERE}:base_tool"),
        ToolDefinition(tool_id="notes_tool", entrypoint=f"{_HERE}:notes_tool"),
        ToolDefinition(tool_id="gh_tool", entrypoint=f"{_HERE}:gh_tool"),
    ]
    if loading:
        tools += [
            ToolDefinition(
                tool_id="capability_load",
                entrypoint="assistant.agent.tools.capability_loading:capability_load",
            ),
            ToolDefinition(
                tool_id="capability_unload",
                entrypoint="assistant.agent.tools.capability_loading:capability_unload",
            ),
        ]
    return ToolsConfig(tools=tools)


def _config(
    *,
    enabled: list[str] | None = None,
    denied: list[str] | None = None,
    loaded: list[str] | None = None,
    limit: int = 5,
    loading_tools: bool = True,
) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(default_model_id=_MODEL, model_allowlist=[_MODEL]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=enabled if enabled is not None else ["assistant", "zeta"],
            denied_capabilities=denied or [],
            max_loaded_capabilities=limit,
            session_loaded_capabilities=loaded or [],
        ),
        tools=_tools(loading=loading_tools),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


def _prompt(config: RuntimeConfig, *, index_enabled: bool = True) -> str:
    effective = resolve_effective_capabilities(config.capabilities, DEFS)
    return _compose_system_prompt(DEFS, effective, index_enabled=index_enabled)


def _tool_names(config: RuntimeConfig, snapshot: CapabilitySnapshot | None = None) -> set[str]:
    snap = snapshot or snapshot_of(DEFS)
    return {
        agent_tool_name(t) for t in get_agent_tools(config, active_capabilities_for(config, snap))
    }


# --- prompt ------------------------------------------------------------------------------


def test_index_lists_loadable_capabilities_with_descriptions() -> None:
    prompt = _prompt(_config())
    index = prompt[prompt.index(INDEX_HEADING) :]
    assert index.splitlines() == [
        INDEX_HEADING,
        "Call capability_load(capability_id, reason) when a request needs one of these:",
        "- deploy — Deploy services",
        "- notes — Apple Notes",
    ]
    assert "PROMPT-notes" not in prompt  # only the index line, not the full prompt
    assert "notes_tool" not in prompt  # no tool definitions in the index


def test_index_is_absent_when_nothing_is_loadable() -> None:
    prompt = _prompt(_config(denied=["notes", "deploy"]))
    assert INDEX_HEADING not in prompt


def test_index_is_absent_when_loading_is_disabled() -> None:
    assert INDEX_HEADING not in _prompt(_config(), index_enabled=False)
    assert INDEX_HEADING not in _prompt(_config(limit=0))


def test_loading_keeps_the_base_prefix_byte_identical() -> None:
    before = _prompt(_config())
    after = _prompt(_config(loaded=["deploy"]))
    base_prefix = before[: before.index(INDEX_HEADING)]
    assert after.startswith(base_prefix)
    # Loaded prompts (with nested children) come after the sorted base prompts.
    tail = after[len(base_prefix) :]
    assert tail.startswith("PROMPT-deploy\n\nPROMPT-gh\n\n" + INDEX_HEADING)
    assert "- deploy" not in tail  # no longer in the index
    assert "- notes — Apple Notes" in tail
    assert "Loaded for this session: deploy." in tail


def test_loaded_prompts_follow_load_order_not_id_order() -> None:
    prompt = _prompt(_config(loaded=["notes", "deploy"]))
    assert prompt.index("PROMPT-notes") < prompt.index("PROMPT-deploy")
    assert prompt.index("PROMPT-zeta") < prompt.index("PROMPT-notes")
    assert INDEX_HEADING not in prompt  # everything loadable is loaded


# --- tools -------------------------------------------------------------------------------


def test_load_tools_are_implicit_while_something_is_loadable() -> None:
    names = _tool_names(_config())
    assert {"capability_load", "capability_unload", "base_tool"} <= names
    assert "notes_tool" not in names


def test_load_tools_are_absent_without_index_or_loaded_set() -> None:
    assert "capability_load" not in _tool_names(_config(denied=["notes", "deploy"]))
    assert "capability_load" not in _tool_names(_config(loading_tools=False))


def test_load_tools_stay_while_capabilities_are_loaded_even_with_empty_index() -> None:
    names = _tool_names(_config(loaded=["notes", "deploy"]))
    assert {"capability_load", "capability_unload", "notes_tool", "gh_tool"} <= names


def test_incremental_toolset_holds_only_new_tools() -> None:
    snapshot = snapshot_of(DEFS)
    turn = _config()
    toolset, names, servers = build_incremental_toolset(turn, _config(loaded=["deploy"]), snapshot)
    assert toolset is not None
    assert names == ["gh_tool"]
    assert servers == []
    # Nothing new -> no toolset (unload, or a capability that only adds a prompt).
    assert build_incremental_toolset(turn, turn, snapshot) == (None, [], [])


def test_incremental_toolset_skips_tools_the_adapter_already_has() -> None:
    snapshot = snapshot_of(DEFS)
    turn = _config(enabled=["assistant", "hidden"])  # hidden already binds notes_tool
    _, names, _ = build_incremental_toolset(
        turn, _config(enabled=["assistant", "hidden"], loaded=["notes"]), snapshot
    )
    assert names == []


# --- adapter cache -----------------------------------------------------------------------


@pytest.fixture()
def cache(monkeypatch: pytest.MonkeyPatch) -> TurnAdapterCache:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return TurnAdapterCache(
        model_id=f"anthropic:{_MODEL}",
        max_tokens=256,
        base_config=_config(),
        snapshot=snapshot_of(DEFS),
    )


def test_cache_key_contains_the_effective_loaded_set() -> None:
    snapshot = snapshot_of(DEFS, revision=3)
    key = TurnAdapterCache._make_key(_config(loaded=["notes", "ghost", "deploy"]), snapshot)
    assert key == (frozenset({"assistant", "zeta"}), ("notes", "deploy"), frozenset(), 3)
    # Ignored / redundant stored ids do not split the cache.
    assert TurnAdapterCache._make_key(_config(loaded=["ghost"]), snapshot) == (
        TurnAdapterCache._make_key(_config(), snapshot)
    )
    # Load order is part of the key (it fixes the prompt layout).
    assert TurnAdapterCache._make_key(_config(loaded=["deploy", "notes"]), snapshot) != key


def test_load_builds_once_then_reuses_and_unload_returns_to_previous(
    cache: TurnAdapterCache,
) -> None:
    snapshot = snapshot_of(DEFS)
    default = cache.get_or_build(_config(), snapshot)
    loaded = cache.get_or_build(_config(loaded=["notes"]), snapshot)
    assert loaded is not default
    assert "PROMPT-notes" in loaded.system_prompt
    assert cache.size == 2
    assert cache.get_or_build(_config(loaded=["notes"]), snapshot) is loaded  # later turns
    assert cache.get_or_build(_config(), snapshot) is default  # unload -> cache hit
    assert cache.size == 2


def test_sessions_with_different_loaded_sets_never_share_an_adapter(
    cache: TurnAdapterCache,
) -> None:
    snapshot = snapshot_of(DEFS)
    a = cache.get_or_build(_config(loaded=["notes"]), snapshot)
    b = cache.get_or_build(_config(loaded=["deploy"]), snapshot)
    assert a is not b
    assert "PROMPT-deploy" not in a.system_prompt
    assert "PROMPT-notes" not in b.system_prompt
