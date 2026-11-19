"""
Component ID: CMP_CORE_CAPABILITIES

Policy decisions for session-scoped capability loading (``capability_load`` /
``capability_unload``).

Pure functions over a base policy, the snapshot definitions and the session's stored
loaded set: they validate a request and return the new stored set, but persist nothing.
The orchestrator owns persistence (``SessionCapabilityContextService``) and the turn
wiring; the agent tools only forward calls.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from assistant.core.capabilities.resolver import (
    EffectiveCapabilities,
    expand_nested_capabilities,
    resolve_effective_capabilities,
)
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.config.schemas import CapabilitiesPolicyConfig

type LoadStatus = Literal[
    "loaded", "already_active", "not_loadable", "denied", "unknown", "limit_reached"
]
type UnloadStatus = Literal["unloaded", "not_loaded", "base_capability"]


def with_loaded(
    policy: CapabilitiesPolicyConfig, loaded: Sequence[str]
) -> CapabilitiesPolicyConfig:
    """``policy`` with ``session_loaded_capabilities`` replaced by ``loaded``."""
    return policy.model_copy(update={"session_loaded_capabilities": list(loaded)})


@dataclass(frozen=True)
class LoadDecision:
    """Outcome of a ``capability_load`` request.

    ``stored`` is the session's loaded set to persist when ``status == "loaded"`` (the
    previous set otherwise). ``after`` is the effective set with ``stored`` applied.
    """

    status: LoadStatus
    capability_id: str
    reason: str
    stored: list[str]
    before: EffectiveCapabilities
    after: EffectiveCapabilities

    @property
    def newly_active(self) -> list[str]:
        """Capabilities activated by this load: the loaded id plus its new nested children."""
        previous = set(self.before.active)
        return [cap_id for cap_id in self.after.active if cap_id not in previous]


@dataclass(frozen=True)
class UnloadDecision:
    """Outcome of a ``capability_unload`` request (see :class:`LoadDecision`)."""

    status: UnloadStatus
    capability_id: str
    reason: str
    stored: list[str]
    before: EffectiveCapabilities
    after: EffectiveCapabilities

    @property
    def deactivated(self) -> list[str]:
        """Capabilities no longer active after the unload (the id and orphaned children)."""
        remaining = set(self.after.active)
        return [cap_id for cap_id in self.before.active if cap_id not in remaining]


def decide_load(
    policy: CapabilitiesPolicyConfig,
    definitions: Mapping[str, CapabilityDefinition],
    stored: Sequence[str],
    capability_id: str,
) -> LoadDecision:
    """Validate loading ``capability_id`` on top of the session's ``stored`` loaded set.

    Checks, in order: denied by policy, unknown id, already active (base, nested or
    loaded), not ``loadable``, per-session limit. ``policy`` is the turn's base policy; its
    ``session_loaded_capabilities`` is ignored in favour of ``stored``.
    """
    cap_id = capability_id.strip()
    current = list(stored)
    before = resolve_effective_capabilities(with_loaded(policy, current), definitions)

    def _reject(status: LoadStatus, reason: str) -> LoadDecision:
        return LoadDecision(status, cap_id, reason, current, before, before)

    definition = definitions.get(cap_id)
    if cap_id in policy.denied_capabilities:
        return _reject("denied", f"Capability '{cap_id}' is denied by policy.")
    if definition is None:
        return _reject("unknown", _unknown_reason(cap_id, before))
    if cap_id in before.active:
        return _reject(
            "already_active", f"Capability '{cap_id}' is already active in this session."
        )
    if not definition.loadable:
        return _reject("not_loadable", f"Capability '{cap_id}' exists but is not marked loadable.")
    if policy.max_loaded_capabilities <= 0:
        return _reject("limit_reached", "Capability loading is disabled by policy.")
    if len(before.loaded) >= policy.max_loaded_capabilities:
        return _reject(
            "limit_reached",
            f"At most {policy.max_loaded_capabilities} capabilities can be loaded per "
            f"session (loaded: {', '.join(before.loaded)}). Unload one with "
            "capability_unload first.",
        )
    new_stored = [c for c in current if c != cap_id] + [cap_id]
    after = resolve_effective_capabilities(with_loaded(policy, new_stored), definitions)
    return LoadDecision("loaded", cap_id, "", new_stored, before, after)


def decide_unload(
    policy: CapabilitiesPolicyConfig,
    definitions: Mapping[str, CapabilityDefinition],
    stored: Sequence[str],
    capability_id: str,
) -> UnloadDecision:
    """Validate removing ``capability_id`` from the session's ``stored`` loaded set.

    Only ids in the loaded set can be unloaded. Base capabilities (enabled policy or the
    per-session ``/capabilities`` override, including their nested children) are rejected
    with ``base_capability``; a child that is active only through a loaded parent is
    ``not_loaded`` (unload the parent instead).
    """
    cap_id = capability_id.strip()
    current = list(stored)
    before = resolve_effective_capabilities(with_loaded(policy, current), definitions)

    def _reject(status: UnloadStatus, reason: str) -> UnloadDecision:
        return UnloadDecision(status, cap_id, reason, current, before, before)

    if cap_id in current:
        new_stored = [c for c in current if c != cap_id]
        after = resolve_effective_capabilities(with_loaded(policy, new_stored), definitions)
        reason = (
            f"Capability '{cap_id}' stays active through the base capability set."
            if cap_id in after.active
            else ""
        )
        return UnloadDecision("unloaded", cap_id, reason, new_stored, before, after)
    if cap_id in before.base:
        return _reject(
            "base_capability",
            f"Capability '{cap_id}' belongs to the session's base capability set and cannot "
            "be unloaded with this tool.",
        )
    if cap_id in before.active:
        parents = [
            loaded_id
            for loaded_id in before.loaded
            if cap_id
            in expand_nested_capabilities([loaded_id], definitions, policy.denied_capabilities)
        ]
        return _reject(
            "not_loaded",
            f"Capability '{cap_id}' was not loaded itself; it is active as a nested "
            f"capability of {', '.join(parents)}. Unload that instead.",
        )
    return _reject("not_loaded", f"Capability '{cap_id}' is not loaded in this session.")


def _unknown_reason(cap_id: str, effective: EffectiveCapabilities) -> str:
    if effective.index:
        return f"No capability '{cap_id}'. Loadable: {', '.join(effective.index)}."
    return f"No capability '{cap_id}'."


def capability_prompt(
    definitions: Mapping[str, CapabilityDefinition], capability_ids: Sequence[str]
) -> str:
    """The non-empty prompts of ``capability_ids`` joined in the given order."""
    parts: list[str] = []
    for cap_id in capability_ids:
        definition = definitions.get(cap_id)
        if definition is not None and definition.prompt.strip():
            parts.append(definition.prompt.strip())
    return "\n\n".join(parts)
