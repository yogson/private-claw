"""
Component ID: CMP_CORE_CAPABILITIES

Single resolution of the active capability set: nested expansion + denied filtering.

Every consumer (system prompt, tools, tool params, mismatch check, sub-agent MCP servers,
``~/.claude/settings.json``) resolves the active set through
:func:`resolve_active_capabilities`, so they can never disagree about which capabilities
are active.
"""

from collections.abc import Iterable, Mapping

import structlog

from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilitySnapshot
from assistant.core.config.schemas import CapabilitiesPolicyConfig

logger = structlog.get_logger(__name__)


def expand_nested_capabilities(
    enabled: Iterable[str],
    definitions: Mapping[str, CapabilityDefinition],
    denied: Iterable[str] = (),
) -> list[str]:
    """Expand ``enabled`` with nested capabilities, depth-first and cycle-safe.

    Nested capabilities are added right after their parent, in declaration order, without
    duplicates. Denied ids are neither included nor descended into, so the children of a
    denied capability are only active when they are reachable through another, non-denied
    path (or enabled explicitly). Ids without a manifest pass through unchanged; use
    :func:`resolve_active_capabilities` to drop them.
    """
    denied_set = frozenset(denied)
    result: list[str] = []
    seen: set[str] = set()

    def _expand(cap_id: str) -> None:
        if cap_id in seen or cap_id in denied_set:
            return
        seen.add(cap_id)
        result.append(cap_id)
        definition = definitions.get(cap_id)
        if definition is not None:
            for nested_id in definition.nested_capabilities:
                _expand(nested_id)

    for cap_id in enabled:
        _expand(cap_id)
    return result


def resolve_active_capabilities(
    enabled: Iterable[str],
    denied: Iterable[str],
    definitions: Mapping[str, CapabilityDefinition],
) -> list[str]:
    """Return the active capability ids for a policy against a set of definitions.

    Expands ``nested_capabilities`` (see :func:`expand_nested_capabilities`), never
    descends into ``denied`` capabilities, and drops ids that have no manifest (with a
    warning). The order is deterministic: explicit ids in the given order, each followed by
    its nested children.
    """
    active: list[str] = []
    for cap_id in expand_nested_capabilities(enabled, definitions, denied):
        if cap_id not in definitions:
            logger.warning(
                "capabilities.resolve.missing_manifest",
                capability_id=cap_id,
                hint="Manifest not found in config/capabilities/*.yaml; ignored",
            )
            continue
        active.append(cap_id)
    return active


def find_missing_capabilities(
    enabled: Iterable[str],
    definitions: Mapping[str, CapabilityDefinition],
) -> list[str]:
    """Return ids reachable from ``enabled`` (nested expanded) that have no manifest."""
    return [c for c in expand_nested_capabilities(enabled, definitions) if c not in definitions]


def resolve_policy(policy: CapabilitiesPolicyConfig, snapshot: CapabilitySnapshot) -> list[str]:
    """:func:`resolve_active_capabilities` for a capability policy and a catalog snapshot."""
    return resolve_active_capabilities(
        policy.enabled_capabilities, policy.denied_capabilities, snapshot.definitions
    )
