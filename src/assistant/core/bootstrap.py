"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Application bootstrap: loads and validates all configuration domains at startup.
Fail-fast policy: any invalid configuration prevents the service from starting.
"""

from pathlib import Path
from typing import Any

from assistant.agent.tools.registry import _resolve_entrypoint
from assistant.core.capabilities.loader import (
    CapabilityLoadError,
    apply_claude_code_settings,
    expand_nested_capabilities,
    load_capability_definitions,
)
from assistant.core.config.loader import ConfigLoader, ConfigLoadError, resolve_config_dir
from assistant.core.config.schemas import RuntimeConfig, ToolDefinition


def _validate_tools_and_capabilities(
    runtime_config: RuntimeConfig,
    definitions: dict[str, Any],
) -> None:
    """Validate capability bindings and tool entrypoints. Raises SystemExit on failure."""
    policy = runtime_config.capabilities
    denied = frozenset(policy.denied_capabilities)
    enabled_caps = [
        c
        for c in expand_nested_capabilities(policy.enabled_capabilities, definitions)
        if c not in denied
    ]
    catalog = {t.tool_id: t for t in runtime_config.tools.tools}
    enabled_tools = {t.tool_id for t in runtime_config.tools.tools if t.enabled}

    tool_ids_to_resolve: set[str] = set()
    for cap_id in enabled_caps:
        definition = definitions.get(cap_id)
        if definition is None:
            continue
        for binding in definition.tools:
            if not binding.enabled:
                continue
            tool_id = binding.tool_id
            if tool_id not in catalog:
                raise SystemExit(
                    f"Capability '{cap_id}' references tool '{tool_id}' which is not in tools.yaml"
                ) from None
            if tool_id not in enabled_tools:
                raise SystemExit(
                    f"Capability '{cap_id}' references tool '{tool_id}' "
                    "which is disabled in tools.yaml"
                ) from None
            tool_ids_to_resolve.add(tool_id)

    for tool_id in tool_ids_to_resolve:
        definition = catalog[tool_id]
        try:
            resolved = _resolve_entrypoint(definition.entrypoint)
        except Exception as exc:
            raise SystemExit(
                f"Tool '{tool_id}' entrypoint '{definition.entrypoint}' failed to resolve: {exc}"
            ) from exc
        if not callable(resolved):
            raise SystemExit(
                f"Tool '{tool_id}' entrypoint '{definition.entrypoint}' is not callable"
            ) from None


def _validate_delegation_defaults(
    runtime_config: RuntimeConfig,
    tools: list[ToolDefinition],
) -> None:
    """Validate delegation tool defaults against model allowlist."""
    model_allowlist = frozenset(runtime_config.model.model_allowlist)
    for tool in tools:
        if tool.default_params is None:
            continue
        default_model = tool.default_params.delegation_default_model_id
        if default_model and default_model not in model_allowlist:
            raise SystemExit(
                f"Tool '{tool.tool_id}' has delegation_default_model_id '{default_model}' "
                f"not in model_allowlist"
            ) from None


def bootstrap(config_dir: str | Path | None = None) -> RuntimeConfig:
    """Load and validate all configuration domains.

    Returns the fully validated RuntimeConfig on success.
    Raises SystemExit with an actionable report on any validation failure.
    """
    config_path = resolve_config_dir(config_dir)
    loader = ConfigLoader(config_dir=config_path)
    try:
        runtime_config = loader.load()
    except ConfigLoadError as exc:
        raise SystemExit(str(exc)) from exc

    try:
        definitions = load_capability_definitions(config_dir=config_path)
    except CapabilityLoadError as exc:
        raise SystemExit(str(exc)) from exc

    all_enabled = expand_nested_capabilities(
        runtime_config.capabilities.enabled_capabilities, definitions
    )
    for cap_id in all_enabled:
        if cap_id not in definitions:
            raise SystemExit(
                f"Enabled capability '{cap_id}' has no manifest in config/capabilities/*.yaml"
            ) from None

    _validate_tools_and_capabilities(runtime_config, definitions)
    _validate_delegation_defaults(runtime_config, runtime_config.tools.tools)
    denied = frozenset(runtime_config.capabilities.denied_capabilities)
    apply_claude_code_settings(
        definitions,
        [c for c in all_enabled if c not in denied],
    )

    return runtime_config
