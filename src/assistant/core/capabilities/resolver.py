"""
Component ID: CMP_CORE_CAPABILITIES

Single resolution of the active capability set: nested expansion + denied filtering.

Every consumer (system prompt, tools, tool params, mismatch check, sub-agent MCP servers,
``~/.claude/settings.json``) resolves the active set through
:func:`resolve_active_capabilities`, so they can never disagree about which capabilities
are active.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

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
    descends into ``denied`` capabilities, and drops ids that have no manifest (logged at
    debug level: this runs several times per turn, and the catalog already warns once).
    The order is deterministic: explicit ids in the given order, each followed by its
    nested children.
    """
    active: list[str] = []
    for cap_id in expand_nested_capabilities(enabled, definitions, denied):
        if cap_id not in definitions:
            logger.debug(
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


# Why a session-loaded id does not take effect (it stays stored, see EffectiveCapabilities).
type IgnoredLoadedReason = Literal["denied", "unknown", "not_loadable", "limit_reached"]


@dataclass(frozen=True)
class EffectiveCapabilities:
    """The capability set of one turn: base policy plus the session's loaded capabilities.

    - ``base``: the base ids (``enabled_capabilities`` or the per-session override) resolved
      on their own (nested expanded, denied pruned, missing dropped).
    - ``loaded``: the session-loaded ids that take effect, in load order. Ids already active
      through ``base`` are left out (they add nothing), so equal effective sets produce
      equal values.
    - ``ignored_loaded``: stored loaded ids that do not take effect, with the reason (denied,
      manifest missing, no longer ``loadable`` after a reload, over the per-session limit).
      They stay in the session's stored set and come back if the reason goes away.
    - ``active``: ``base ++ loaded`` resolved; the order is the resolver order, i.e. base
      first, then each loaded id followed by its nested children.
    - ``index``: ``loadable`` capabilities that are neither active nor denied, sorted by id.
    """

    base: list[str]
    loaded: list[str]
    active: list[str]
    index: list[str]
    ignored_loaded: dict[str, IgnoredLoadedReason] = field(default_factory=dict)

    @property
    def added_by_loading(self) -> list[str]:
        """Active ids that are active only because of ``loaded`` (loaded ids + children)."""
        base = set(self.base)
        return [cap_id for cap_id in self.active if cap_id not in base]


def resolve_effective_capabilities(
    policy: CapabilitiesPolicyConfig,
    definitions: Mapping[str, CapabilityDefinition],
) -> EffectiveCapabilities:
    """Resolve ``policy`` (base + ``session_loaded_capabilities``) against ``definitions``.

    ``denied_capabilities`` always wins: a denied id is never loaded, never indexed, and
    its nested children are only active through another, non-denied path. A loaded id
    must name an existing ``loadable`` manifest; at most ``max_loaded_capabilities`` loaded
    ids take effect (first loaded first). Pure and cheap; logs nothing (the orchestrator
    reports ignored loaded ids once per turn).
    """
    denied = frozenset(policy.denied_capabilities)
    base = resolve_active_capabilities(policy.enabled_capabilities, denied, definitions)
    base_set = set(base)
    loaded: list[str] = []
    ignored: dict[str, IgnoredLoadedReason] = {}
    for cap_id in dict.fromkeys(policy.session_loaded_capabilities):
        definition = definitions.get(cap_id)
        if cap_id in denied:
            ignored[cap_id] = "denied"
        elif definition is None:
            ignored[cap_id] = "unknown"
        elif not definition.loadable:
            ignored[cap_id] = "not_loadable"
        elif cap_id in base_set:
            continue
        elif len(loaded) >= policy.max_loaded_capabilities:
            ignored[cap_id] = "limit_reached"
        else:
            loaded.append(cap_id)
    active = resolve_active_capabilities(
        [*policy.enabled_capabilities, *loaded], denied, definitions
    )
    active_set = set(active)
    index: list[str] = []
    if policy.max_loaded_capabilities > 0:
        index = sorted(
            cap_id
            for cap_id, definition in definitions.items()
            if definition.loadable and cap_id not in active_set and cap_id not in denied
        )
    return EffectiveCapabilities(
        base=base, loaded=loaded, active=active, index=index, ignored_loaded=ignored
    )


def resolve_policy(policy: CapabilitiesPolicyConfig, snapshot: CapabilitySnapshot) -> list[str]:
    """The active capability ids of ``policy`` (base + session-loaded) at ``snapshot``."""
    return resolve_effective_capabilities(policy, snapshot.definitions).active
