"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Tool registration for Pydantic AI agent. Capability-first activation model:

- config/tools.yaml: canonical catalog of ALL available tools (enabled by default).
  Global enabled=false acts as a hard kill-switch (tool never exposed).
- config/capabilities/*.yaml: each capability declares which tools it activates.
  assistant = baseline; add-on capabilities (e.g. deploy, github-ops) extend the set.
- Runtime availability: tool is exposed only if BOTH (1) enabled in tools.yaml AND
  (2) listed and enabled in at least one active capability (enabled_capabilities,
  excluding denied_capabilities).
"""

from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Any, cast, get_type_hints

import structlog
from pydantic_ai import Tool
from pydantic_ai.tools import ToolFuncEither

from assistant.agent.deps import TurnDeps
from assistant.core.capabilities.loader import (
    expand_nested_capabilities,
    load_capability_definitions,
)
from assistant.core.config.loader import resolve_config_dir
from assistant.core.config.schemas import RuntimeConfig
from assistant.extensions.mcp.bridge import McpBridge


def _config_dir(config: RuntimeConfig) -> Path:
    """Resolve config dir from RuntimeConfig or fallback to env/default."""
    return config.config_dir if config.config_dir is not None else resolve_config_dir()


logger = structlog.get_logger(__name__)

type AgentTool = Tool[TurnDeps] | ToolFuncEither[TurnDeps, ...]


def build_tool_runtime_params(config: RuntimeConfig) -> dict[str, dict[str, Any]]:
    """Build merged per-tool params from tools.yaml defaults + capability overrides."""
    tool_ids = collect_enabled_tool_ids(config)
    tool_defs = {t.tool_id: t for t in config.tools.tools if t.enabled}
    definitions = load_capability_definitions(config_dir=_config_dir(config))
    policy = config.capabilities
    denied = frozenset(policy.denied_capabilities)
    enabled = [
        c
        for c in expand_nested_capabilities(policy.enabled_capabilities, definitions)
        if c not in denied
    ]

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


def collect_enabled_tool_ids(config: RuntimeConfig) -> set[str]:
    """Collect tool_ids from enabled capabilities (excluding denied).

    MCP tools: cap.mcp.<server>.<tool> in enabled_capabilities are added directly
    (no capability manifest required); they are gated by tool mapping and server config.
    """
    policy = config.capabilities
    denied = frozenset(policy.denied_capabilities)
    definitions = load_capability_definitions(config_dir=_config_dir(config))
    enabled = [
        c
        for c in expand_nested_capabilities(policy.enabled_capabilities, definitions)
        if c not in denied
    ]

    tool_ids: set[str] = set()
    for cap_id in enabled:
        if cap_id.startswith("cap.mcp."):
            tool_ids.add(cap_id)
            continue
        definition = definitions.get(cap_id)
        if definition is None:
            logger.warning(
                "provider.tools.capability_missing",
                capability_id=cap_id,
                hint="Manifest not found in config/capabilities/*.yaml",
            )
            continue
        for binding in definition.tools:
            if binding.enabled:
                tool_ids.add(binding.tool_id)
    return tool_ids


def get_agent_tools(config: RuntimeConfig) -> Sequence[AgentTool]:
    """Return tools for the agent from tools.yaml and MCP bridge, gated by enabled capabilities."""
    tool_ids = collect_enabled_tool_ids(config)
    tool_defs = {t.tool_id: t for t in config.tools.tools if t.enabled}
    mcp_ids = {t for t in tool_ids if t.startswith("cap.mcp.")}
    first_party_ids = tool_ids - mcp_ids

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
        hints = {}
        try:
            hints = get_type_hints(resolved)
        except Exception:
            pass
        return_hint = hints.get("return")
        is_factory = return_hint is not None and (
            hasattr(return_hint, "__args__")
            and any(
                getattr(a, "__name__", None) == "Tool" or a is type(None)
                for a in return_hint.__args__
            )
        )
        if is_factory:
            if definition.max_retries != 0:
                logger.warning(
                    "provider.tools.factory_max_retries_ignored",
                    tool_id=tool_id,
                    hint="Factory tools return a pre-built Tool instance; max_retries in tools.yaml has no effect.",
                )
            tool_instance = resolved()
            if tool_instance is None:
                logger.info(
                    "provider.tools.factory_skipped",
                    tool_id=tool_id,
                    reason="factory returned None (likely missing API key)",
                )
                continue
            tools.append(cast(AgentTool, tool_instance))
        else:
            tools.append(
                Tool(
                    cast(ToolFuncEither[TurnDeps, ...], resolved),
                    max_retries=definition.max_retries,
                )
            )

    if mcp_ids:
        bridge = McpBridge(config)
        mcp_tools = bridge.get_tools_for_capability_ids(mcp_ids)
        tools.extend(cast(list[AgentTool], mcp_tools))
    return tools
