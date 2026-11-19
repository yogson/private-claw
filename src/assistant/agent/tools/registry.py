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

from collections.abc import Mapping, Sequence
from contextlib import suppress
from importlib import import_module
from typing import Any, cast, get_type_hints

import structlog
from pydantic_ai import Tool
from pydantic_ai.tools import ToolFuncEither
from pydantic_ai.toolsets import AbstractToolset

from assistant.agent.deps import TurnDeps
from assistant.core.capabilities.loader import collect_mcp_server_ids
from assistant.core.capabilities.resolver import resolve_policy
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilitySnapshot
from assistant.core.config.schemas import RuntimeConfig
from assistant.extensions.mcp.toolsets import mcp_toolsets

logger = structlog.get_logger(__name__)

type AgentTool = Tool[TurnDeps] | ToolFuncEither[TurnDeps, ...]

# Capability definitions of one snapshot plus the policy's active ids (nested expanded,
# denied removed, missing manifests dropped) — see :func:`active_capabilities_for`.
type ActiveCapabilities = tuple[Mapping[str, CapabilityDefinition], list[str]]


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


def is_tool_factory(entrypoint: object) -> bool:
    """True if the entrypoint is a factory that returns a Tool (or None) to be called.

    Factories signal this by having a return annotation of `Tool | None` or `Any | None`.
    """
    hints: dict[str, Any] = {}
    with suppress(Exception):
        hints = get_type_hints(entrypoint)
    return_hint = hints.get("return")
    return return_hint is not None and (
        hasattr(return_hint, "__args__")
        and any(
            getattr(a, "__name__", None) == "Tool" or a is type(None) for a in return_hint.__args__
        )
    )


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
    first_party_ids = collect_enabled_tool_ids(active)
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
        if is_tool_factory(resolved):
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
