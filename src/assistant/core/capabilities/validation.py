"""
Component ID: CMP_CORE_CAPABILITIES

Cross-file validation of a candidate set of capability definitions.

Shared by the startup bootstrap (fail-fast) and the catalog reload path (reject the swap,
keep the last-known-good snapshot). Validators return issues instead of raising, so each
caller decides how to react.
"""

from collections.abc import Callable, Mapping

from assistant.core.capabilities.resolver import (
    find_missing_capabilities,
    resolve_active_capabilities,
)
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilityLoadIssue
from assistant.core.config.schemas import RuntimeConfig

type CapabilityValidator = Callable[[Mapping[str, CapabilityDefinition]], list[CapabilityLoadIssue]]
type EntrypointResolver = Callable[[str], object]

# Reserved for MCP tool names (see assistant.extensions.mcp.toolsets.MCP_TOOL_PREFIX); kept
# here as a literal so the capabilities domain does not depend on the MCP extension.
MCP_TOOL_PREFIX = "mcp_"


def validate_nested_references(
    definitions: Mapping[str, CapabilityDefinition],
) -> list[CapabilityLoadIssue]:
    """Every ``nested_capabilities`` entry must name a known capability."""
    issues: list[CapabilityLoadIssue] = []
    for cap_id in sorted(definitions):
        for nested_id in definitions[cap_id].nested_capabilities:
            if nested_id not in definitions:
                issues.append(
                    CapabilityLoadIssue(
                        kind="validation",
                        message=(f"Capability '{cap_id}' nests unknown capability '{nested_id}'"),
                    )
                )
    return issues


def validate_policy_bindings(
    runtime_config: RuntimeConfig,
    definitions: Mapping[str, CapabilityDefinition],
    resolve_entrypoint: EntrypointResolver | None = None,
) -> list[CapabilityLoadIssue]:
    """Validate the policy's active capabilities against tools.yaml.

    Bindings of the active set (enabled, nested expanded, denied removed) must reference
    tools that exist and are enabled in tools.yaml and must not use the reserved
    ``mcp_`` prefix; with ``resolve_entrypoint`` their entrypoints must resolve to
    callables. Enabled capabilities without a manifest are reported as non-blocking
    warnings (startup checks them separately and fails fast).
    """
    policy = runtime_config.capabilities
    issues = [
        CapabilityLoadIssue(
            kind="warning",
            message=f"Enabled capability '{cap_id}' has no manifest; it is ignored",
        )
        for cap_id in find_missing_capabilities(policy.enabled_capabilities, definitions)
    ]
    active = resolve_active_capabilities(
        policy.enabled_capabilities, policy.denied_capabilities, definitions
    )
    catalog = {t.tool_id: t for t in runtime_config.tools.tools}
    enabled_tools = {t.tool_id for t in runtime_config.tools.tools if t.enabled}

    def _error(message: str) -> None:
        issues.append(CapabilityLoadIssue(kind="validation", message=message))

    tool_ids_to_resolve: set[str] = set()
    for cap_id in active:
        for binding in definitions[cap_id].tools:
            if not binding.enabled:
                continue
            tool_id = binding.tool_id
            if tool_id.startswith(MCP_TOOL_PREFIX):
                _error(
                    f"Capability '{cap_id}' binds tool '{tool_id}': first-party tool ids must not "
                    f"start with '{MCP_TOOL_PREFIX}' (reserved for MCP tools). "
                    "Grant MCP servers through the capability's mcp_servers list."
                )
            elif tool_id not in catalog:
                _error(
                    f"Capability '{cap_id}' references tool '{tool_id}' which is not in tools.yaml"
                )
            elif tool_id not in enabled_tools:
                _error(
                    f"Capability '{cap_id}' references tool '{tool_id}' "
                    "which is disabled in tools.yaml"
                )
            else:
                tool_ids_to_resolve.add(tool_id)

    if resolve_entrypoint is not None:
        for tool_id in sorted(tool_ids_to_resolve):
            entrypoint = catalog[tool_id].entrypoint
            try:
                resolved = resolve_entrypoint(entrypoint)
            except Exception as exc:
                _error(f"Tool '{tool_id}' entrypoint '{entrypoint}' failed to resolve: {exc}")
                continue
            if not callable(resolved):
                _error(f"Tool '{tool_id}' entrypoint '{entrypoint}' is not callable")
    return issues


def make_policy_validator(
    runtime_config: RuntimeConfig,
    resolve_entrypoint: EntrypointResolver | None = None,
) -> CapabilityValidator:
    """Bind :func:`validate_policy_bindings` to a runtime config for the catalog."""

    def _validate(definitions: Mapping[str, CapabilityDefinition]) -> list[CapabilityLoadIssue]:
        return validate_policy_bindings(runtime_config, definitions, resolve_entrypoint)

    return _validate
