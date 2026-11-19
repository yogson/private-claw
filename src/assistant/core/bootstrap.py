"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Application bootstrap: loads and validates all configuration domains at startup.
Fail-fast policy: any invalid configuration prevents the service from starting.
"""

from collections.abc import Mapping
from pathlib import Path

import structlog

from assistant.agent.tools.registry import _resolve_entrypoint
from assistant.core.capabilities.catalog import CapabilityCatalog
from assistant.core.capabilities.loader import CapabilityLoadError
from assistant.core.capabilities.resolver import (
    find_missing_capabilities,
    resolve_active_capabilities,
)
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.validation import make_policy_validator
from assistant.core.config.loader import ConfigLoader, ConfigLoadError, resolve_config_dir
from assistant.core.config.schemas import RuntimeConfig, ToolDefinition

logger = structlog.get_logger(__name__)


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


def _validate_mcp_capabilities(
    runtime_config: RuntimeConfig,
    definitions: Mapping[str, CapabilityDefinition],
    all_enabled: list[str],
) -> None:
    """Cross-validate capability ``mcp_servers`` against mcp_servers.yaml.

    Logs warnings (never fails startup) for referenced servers that are missing from
    mcp_servers.yaml or disabled there.
    """
    server_index = {srv.id: srv for srv in runtime_config.mcp_servers.servers}
    for cap_id in all_enabled:
        definition = definitions.get(cap_id)
        if definition is None:
            continue
        for server_id in definition.mcp_servers:
            srv = server_index.get(server_id)
            if srv is None:
                logger.warning(
                    "bootstrap.mcp.server_not_configured",
                    capability_id=cap_id,
                    server_id=server_id,
                    hint=(
                        f"Server '{server_id}' is referenced in capabilities "
                        "but not in mcp_servers.yaml"
                    ),
                )
            elif not srv.enabled:
                logger.warning(
                    "bootstrap.mcp.server_disabled",
                    capability_id=cap_id,
                    server_id=server_id,
                    hint=f"Server '{server_id}' is in mcp_servers.yaml but enabled=false",
                )


def bootstrap_with_catalog(
    config_dir: str | Path | None = None,
) -> tuple[RuntimeConfig, CapabilityCatalog]:
    """Load and validate all configuration domains and build the capability catalog.

    Returns the fully validated RuntimeConfig and the CapabilityCatalog (revision 1) on
    success. Raises SystemExit with an actionable report on any validation failure: the
    initial capability load is fail-fast, since there is no last-known-good state yet.
    """
    config_path = resolve_config_dir(config_dir)
    loader = ConfigLoader(config_dir=config_path)
    try:
        runtime_config = loader.load()
    except ConfigLoadError as exc:
        raise SystemExit(str(exc)) from exc

    policy = runtime_config.capabilities
    if policy.session_loaded_capabilities:
        raise SystemExit(
            "capabilities.yaml: session_loaded_capabilities is runtime-only (filled per session "
            "by capability_load); use enabled_capabilities instead"
        )
    try:
        # The same cross-file validator rejects broken reloads at runtime.
        catalog = CapabilityCatalog(
            config_path,
            validator=make_policy_validator(runtime_config, _resolve_entrypoint),
        )
    except CapabilityLoadError as exc:
        raise SystemExit(str(exc)) from exc
    definitions = catalog.current().definitions

    for cap_id in find_missing_capabilities(policy.enabled_capabilities, definitions):
        raise SystemExit(
            f"Enabled capability '{cap_id}' has no manifest in config/capabilities/*.yaml"
        ) from None

    # Loadable capabilities can be activated by any session, so their MCP servers are
    # checked like the enabled ones.
    loadable = sorted(cap_id for cap_id, d in definitions.items() if d.loadable)
    active = resolve_active_capabilities(
        [*policy.enabled_capabilities, *loadable], policy.denied_capabilities, definitions
    )
    _validate_mcp_capabilities(runtime_config, definitions, active)
    _validate_delegation_defaults(runtime_config, runtime_config.tools.tools)
    # ~/.claude/settings.json is applied by ClaudeCodeSettingsSync (api/lifespan.py), which
    # records whether the write succeeded so a skipped write is retried.

    return runtime_config, catalog


def bootstrap(config_dir: str | Path | None = None) -> RuntimeConfig:
    """Load and validate all configuration domains.

    Returns the fully validated RuntimeConfig on success.
    Raises SystemExit with an actionable report on any validation failure.
    """
    runtime_config, _ = bootstrap_with_catalog(config_dir)
    return runtime_config
