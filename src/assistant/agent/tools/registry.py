"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Tool registration for Pydantic AI agent. Capability-first activation model:

- config/tools.yaml: canonical catalog of ALL available tools (enabled by default).
  Global enabled=false acts as a hard kill-switch (tool never exposed).
- config/capabilities/*.yaml: each capability declares which tools it activates.
  assistant = baseline; add-on capabilities (e.g. deploy, github-ops) extend the set.
- Runtime availability: tool is exposed only if BOTH (1) enabled in tools.yaml AND
  (2) listed and enabled in at least one active capability (enabled_capabilities or the
  session override, plus the session's loaded capabilities, excluding denied_capabilities).
- capability_load / capability_unload are not bound by any capability: they are added
  implicitly whenever the session has something to load (non-empty index) or to unload,
  so no capability can unload the tool that loads capabilities.
"""

from collections.abc import Mapping, Sequence
from contextlib import suppress
from importlib import import_module
from typing import Any, cast, get_type_hints

import structlog
from pydantic_ai import Tool
from pydantic_ai.tools import ToolFuncEither
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FunctionToolset

from assistant.agent.deps import TurnDeps
from assistant.core.capabilities.loader import collect_mcp_server_ids
from assistant.core.capabilities.resolver import resolve_effective_capabilities, resolve_policy
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilitySnapshot
from assistant.core.config.schemas import RuntimeConfig
from assistant.extensions.mcp.toolsets import mcp_toolsets

logger = structlog.get_logger(__name__)

type AgentTool = Tool[TurnDeps] | ToolFuncEither[TurnDeps, ...]

# Capability definitions of one snapshot plus the policy's active ids (nested expanded,
# denied removed, missing manifests dropped) — see :func:`active_capabilities_for`.
type ActiveCapabilities = tuple[Mapping[str, CapabilityDefinition], list[str]]

CAPABILITY_LOAD_TOOL_ID = "capability_load"
CAPABILITY_UNLOAD_TOOL_ID = "capability_unload"
CAPABILITY_LOADING_TOOL_IDS = frozenset({CAPABILITY_LOAD_TOOL_ID, CAPABILITY_UNLOAD_TOOL_ID})


def capability_loading_available(config: RuntimeConfig) -> bool:
    """True when sessions may load capabilities: a positive limit and capability_load on.

    tools.yaml stays the kill switch: with capability_load disabled (or missing) there is
    no index in the prompt and no capability_load tool.
    """
    if config.capabilities.max_loaded_capabilities <= 0:
        return False
    return any(t.tool_id == CAPABILITY_LOAD_TOOL_ID and t.enabled for t in config.tools.tools)


def capability_loading_tool_ids(
    config: RuntimeConfig, definitions: Mapping[str, CapabilityDefinition]
) -> set[str]:
    """capability_load/unload when there is something to load or unload, else nothing."""
    if not capability_loading_available(config):
        return set()
    effective = resolve_effective_capabilities(config.capabilities, definitions)
    if effective.index or effective.loaded:
        return set(CAPABILITY_LOADING_TOOL_IDS)
    return set()


def active_capabilities_for(
    config: RuntimeConfig, snapshot: CapabilitySnapshot
) -> ActiveCapabilities:
    """Resolve ``config``'s capability policy against one catalog snapshot.

    No disk access: every consumer of a turn passes the same snapshot, so the prompt,
    tools, tool params and the mismatch check agree on the manifest state.
    """
    return snapshot.definitions, resolve_policy(config.capabilities, snapshot)


def build_tool_runtime_params(
    config: RuntimeConfig, snapshot: CapabilitySnapshot
) -> dict[str, dict[str, Any]]:
    """Build merged per-tool params from tools.yaml defaults + capability overrides."""
    definitions, enabled = active_capabilities_for(config, snapshot)
    tool_ids = collect_enabled_tool_ids((definitions, enabled))
    tool_defs = {t.tool_id: t for t in config.tools.tools if t.enabled}

    result: dict[str, dict[str, Any]] = {}
    for tool_id in tool_ids:
        definition = tool_defs.get(tool_id)
        if definition is None:
            continue
        params = (
            definition.default_params.model_dump(exclude_none=True)
            if definition.default_params is not None
            else {}
        )
        for cap_id in enabled:
            cap_def = definitions.get(cap_id)
            if cap_def is None:
                continue
            override = cap_def.get_effective_tool_overrides(tool_id)
            for k, v in override.items():
                if k == "command_allowlist" and isinstance(v, list):
                    params[k] = [x.model_dump() if hasattr(x, "model_dump") else x for x in v]
                else:
                    params[k] = v
        result[tool_id] = params
    return result


def _resolve_entrypoint(entrypoint: str) -> object:
    """Resolve entrypoint string to Python callable. Raises on failure."""
    if ":" not in entrypoint:
        raise ValueError(f"Invalid entrypoint: {entrypoint!r}")
    module_path, attr = entrypoint.split(":", 1)
    module_path = module_path.strip()
    attr = attr.strip()
    if not module_path or not attr:
        raise ValueError(f"Invalid entrypoint: {entrypoint!r}")
    mod = import_module(module_path)
    obj = getattr(mod, attr)
    if not callable(obj):
        raise ValueError(f"Entrypoint {entrypoint!r} is not callable")
    return obj


def collect_enabled_mcp_server_ids(active: ActiveCapabilities) -> list[str]:
    """MCP server ids granted by the active capabilities."""
    definitions, enabled = active
    return collect_mcp_server_ids(definitions, enabled)


def get_agent_mcp_toolsets(
    config: RuntimeConfig, active: ActiveCapabilities
) -> list[AbstractToolset[Any]]:
    """Toolsets for the MCP servers the active capability set references.

    Servers that are unknown or disabled in mcp_servers.yaml are skipped. The underlying
    connections are shared process-wide and owned by the app lifespan.
    """
    server_ids = collect_enabled_mcp_server_ids(active)
    return mcp_toolsets.toolsets_for(config.mcp_servers, server_ids)


def collect_enabled_tool_ids(active: ActiveCapabilities) -> set[str]:
    """Collect first-party tool_ids bound (and enabled) by the active capabilities."""
    definitions, enabled = active

    tool_ids: set[str] = set()
    for cap_id in enabled:
        definition = definitions.get(cap_id)
        if definition is None:
            continue  # the resolver already dropped (and logged) missing manifests
        for binding in definition.tools:
            if binding.enabled:
                tool_ids.add(binding.tool_id)
    return tool_ids


def get_agent_tools(config: RuntimeConfig, active: ActiveCapabilities) -> Sequence[AgentTool]:
    """Return first-party tools from tools.yaml, gated by enabled capabilities.

    MCP servers are not tools here; see :func:`get_agent_mcp_toolsets`.
    """
    first_party_ids = collect_enabled_tool_ids(active) | capability_loading_tool_ids(
        config, active[0]
    )
    tool_defs = {t.tool_id: t for t in config.tools.tools if t.enabled}

    tools: list[AgentTool] = []
    for tool_id in sorted(first_party_ids):
        definition = tool_defs.get(tool_id)
        if definition is None:
            logger.debug(
                "provider.tools.skipped",
                tool_id=tool_id,
                reason="not in tools.yaml or disabled",
            )
            continue
        try:
            resolved = _resolve_entrypoint(definition.entrypoint)
        except Exception as exc:
            logger.warning(
                "provider.tools.resolve_failed",
                tool_id=tool_id,
                entrypoint=definition.entrypoint,
                error=str(exc),
            )
            continue
        # If the entrypoint is a factory (returns a Tool or None), call it.
        # Factories signal this by having a return annotation of `Tool | None` or `Any | None`.
        hints: dict[str, Any] = {}
        with suppress(Exception):
            hints = get_type_hints(resolved)
        return_hint = hints.get("return")
        is_factory = return_hint is not None and (
            hasattr(return_hint, "__args__")
            and any(
                getattr(a, "__name__", None) == "Tool" or a is type(None)
                for a in return_hint.__args__
            )
        )
        if is_factory:
            tool_instance = resolved()
            if tool_instance is None:
                logger.info(
                    "provider.tools.factory_skipped",
                    tool_id=tool_id,
                    reason="factory returned None (likely missing API key)",
                )
                continue
            # tools.yaml is canonical: apply max_retries to the pre-built Tool too,
            # otherwise factory tools would silently ignore it.
            if definition.max_retries is not None and isinstance(tool_instance, Tool):
                tool_instance.max_retries = definition.max_retries
            tools.append(cast(AgentTool, tool_instance))
        else:
            tools.append(
                Tool(
                    cast(ToolFuncEither[TurnDeps, ...], resolved),
                    max_retries=definition.max_retries,
                )
            )
    return tools


def agent_tool_name(tool: AgentTool) -> str:
    """The model-facing name of a first-party tool (``Tool.name`` or the function name)."""
    if isinstance(tool, Tool):
        return tool.name
    return str(getattr(tool, "__name__", ""))


def build_incremental_toolset(
    turn_config: RuntimeConfig,
    current_config: RuntimeConfig,
    snapshot: CapabilitySnapshot,
) -> tuple[AbstractToolset[Any] | None, list[str], list[str]]:
    """Tools of ``current_config`` that the adapter built from ``turn_config`` lacks.

    Used when capability_load runs mid-turn: the turn's adapter (prompt + tools) is fixed,
    so the newly activated capabilities' first-party tools and MCP servers are exposed on
    the next model request through a per-run-step dynamic toolset. Tools and servers the
    adapter already has are skipped (no duplicate tool names). Returns
    ``(toolset or None, new first-party tool names, new MCP server ids)``.
    """
    turn_active = active_capabilities_for(turn_config, snapshot)
    current_active = active_capabilities_for(current_config, snapshot)
    existing = {agent_tool_name(t) for t in get_agent_tools(turn_config, turn_active)}
    new_tools = [
        t
        for t in get_agent_tools(current_config, current_active)
        if agent_tool_name(t) not in existing
    ]
    existing_servers = set(collect_enabled_mcp_server_ids(turn_active))
    new_server_ids = [
        sid for sid in collect_enabled_mcp_server_ids(current_active) if sid not in existing_servers
    ]
    toolsets: list[AbstractToolset[Any]] = []
    if new_tools:
        toolsets.append(FunctionToolset[TurnDeps](list(new_tools), max_retries=0))
    toolsets.extend(mcp_toolsets.toolsets_for(current_config.mcp_servers, new_server_ids))
    if not toolsets:
        return None, [], []
    toolset: AbstractToolset[Any] = toolsets[0] if len(toolsets) == 1 else CombinedToolset(toolsets)
    return toolset, sorted(agent_tool_name(t) for t in new_tools), new_server_ids
