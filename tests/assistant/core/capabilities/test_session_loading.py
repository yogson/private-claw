"""Session capability loading: effective set, index, load/unload decisions, manifest fields."""

import pytest
from pydantic import ValidationError

from assistant.core.capabilities.resolver import resolve_effective_capabilities
from assistant.core.capabilities.schemas import CapabilityDefinition, CapabilityToolBinding
from assistant.core.capabilities.session_loading import (
    capability_prompt,
    decide_load,
    decide_unload,
)
from assistant.core.config.schemas import CapabilitiesPolicyConfig


def _cap(
    cap_id: str,
    *,
    loadable: bool = False,
    nested: list[str] | None = None,
    description: str = "",
) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=cap_id,
        description=description,
        loadable=loadable,
        prompt=f"PROMPT-{cap_id}",
        tools=[CapabilityToolBinding(tool_id=f"tool_{cap_id}")],
        nested_capabilities=nested or [],
    )


DEFS = {
    "assistant": _cap("assistant"),
    "notes": _cap("notes", loadable=True, description="Apple Notes"),
    "deploy": _cap("deploy", loadable=True, nested=["gh"], description="Deploy services"),
    "gh": _cap("gh"),
    "secret": _cap("secret", loadable=True, nested=["leak"]),
    "leak": _cap("leak", loadable=True),
    "plain": _cap("plain"),
}


def _policy(
    enabled: list[str] | None = None,
    denied: list[str] | None = None,
    loaded: list[str] | None = None,
    limit: int = 5,
) -> CapabilitiesPolicyConfig:
    return CapabilitiesPolicyConfig(
        enabled_capabilities=enabled if enabled is not None else ["assistant"],
        denied_capabilities=denied or [],
        max_loaded_capabilities=limit,
        session_loaded_capabilities=loaded or [],
    )


# --- effective set / index ---------------------------------------------------------------


def test_index_lists_loadable_non_active_non_denied_sorted() -> None:
    effective = resolve_effective_capabilities(_policy(denied=["secret"], loaded=["notes"]), DEFS)
    # notes is active (loaded), secret denied, plain/gh not loadable.
    assert effective.index == ["deploy", "leak"]
    assert effective.active == ["assistant", "notes"]
    assert effective.base == ["assistant"]
    assert effective.loaded == ["notes"]


def test_index_is_deterministic_regardless_of_manifest_order() -> None:
    reversed_defs = dict(reversed(list(DEFS.items())))
    first = resolve_effective_capabilities(_policy(), DEFS).index
    assert resolve_effective_capabilities(_policy(), reversed_defs).index == first


def test_loaded_capability_activates_nested_children_in_load_order() -> None:
    effective = resolve_effective_capabilities(_policy(loaded=["deploy", "notes"]), DEFS)
    assert effective.active == ["assistant", "deploy", "gh", "notes"]
    assert effective.added_by_loading == ["deploy", "gh", "notes"]
    assert "gh" not in effective.index


def test_denied_wins_over_loaded_and_prunes_children() -> None:
    effective = resolve_effective_capabilities(_policy(denied=["secret"], loaded=["secret"]), DEFS)
    assert "secret" not in effective.active
    assert "leak" not in effective.active  # child of a denied parent stays inactive
    assert effective.ignored_loaded == {"secret": "denied"}
    assert "secret" not in effective.index


def test_children_of_denied_capability_are_never_activated_by_loading() -> None:
    effective = resolve_effective_capabilities(_policy(denied=["gh"], loaded=["deploy"]), DEFS)
    assert effective.active == ["assistant", "deploy"]


def test_stored_ids_that_are_missing_or_not_loadable_are_ignored() -> None:
    effective = resolve_effective_capabilities(_policy(loaded=["ghost", "plain", "notes"]), DEFS)
    assert effective.loaded == ["notes"]
    assert effective.ignored_loaded == {"ghost": "unknown", "plain": "not_loadable"}


def test_loaded_ids_beyond_the_limit_are_ignored_first_loaded_first() -> None:
    effective = resolve_effective_capabilities(
        _policy(loaded=["notes", "deploy", "leak"], limit=2), DEFS
    )
    assert effective.loaded == ["notes", "deploy"]
    assert effective.ignored_loaded == {"leak": "limit_reached"}


def test_loaded_id_already_in_base_is_left_out_of_the_loaded_list() -> None:
    effective = resolve_effective_capabilities(
        _policy(enabled=["assistant", "notes"], loaded=["notes"]), DEFS
    )
    assert effective.loaded == []
    assert effective.ignored_loaded == {}
    assert effective.active == ["assistant", "notes"]


def test_zero_limit_disables_loading_and_index() -> None:
    effective = resolve_effective_capabilities(_policy(loaded=["notes"], limit=0), DEFS)
    assert effective.index == []
    assert effective.loaded == []


# --- decide_load -------------------------------------------------------------------------


def test_load_success_returns_new_stored_set_and_newly_active() -> None:
    decision = decide_load(_policy(), DEFS, ["notes"], " deploy ")
    assert decision.status == "loaded"
    assert decision.capability_id == "deploy"
    assert decision.stored == ["notes", "deploy"]
    assert decision.newly_active == ["deploy", "gh"]
    assert capability_prompt(DEFS, decision.newly_active) == "PROMPT-deploy\n\nPROMPT-gh"


@pytest.mark.parametrize(
    ("cap_id", "status"),
    [
        ("ghost", "unknown"),
        ("plain", "not_loadable"),
        ("secret", "denied"),
        ("assistant", "already_active"),
        ("notes", "already_active"),
    ],
)
def test_load_rejections(cap_id: str, status: str) -> None:
    decision = decide_load(_policy(denied=["secret"]), DEFS, ["notes"], cap_id)
    assert decision.status == status
    assert decision.reason
    assert decision.stored == ["notes"]


def test_load_rejects_nested_child_already_active_through_loaded_parent() -> None:
    defs = {**DEFS, "gh": _cap("gh", loadable=True)}
    assert decide_load(_policy(), defs, ["deploy"], "gh").status == "already_active"


def test_load_unknown_lists_loadable_ids() -> None:
    decision = decide_load(_policy(), DEFS, [], "ghost")
    assert "deploy" in decision.reason and "notes" in decision.reason


def test_load_enforces_the_per_session_limit() -> None:
    decision = decide_load(_policy(limit=1), DEFS, ["notes"], "deploy")
    assert decision.status == "limit_reached"
    assert "notes" in decision.reason


def test_load_with_zero_limit_is_rejected() -> None:
    assert decide_load(_policy(limit=0), DEFS, [], "notes").status == "limit_reached"


# --- decide_unload -----------------------------------------------------------------------


def test_unload_success_removes_parent_and_orphaned_children() -> None:
    decision = decide_unload(_policy(), DEFS, ["deploy", "notes"], "deploy")
    assert decision.status == "unloaded"
    assert decision.stored == ["notes"]
    assert decision.deactivated == ["deploy", "gh"]


def test_unload_of_base_capability_is_rejected() -> None:
    decision = decide_unload(_policy(), DEFS, ["notes"], "assistant")
    assert decision.status == "base_capability"
    assert decision.stored == ["notes"]


def test_unload_of_never_loaded_id_is_rejected() -> None:
    assert decide_unload(_policy(), DEFS, [], "notes").status == "not_loaded"
    assert decide_unload(_policy(), DEFS, [], "ghost").status == "not_loaded"


def test_unload_of_nested_child_points_at_the_parent() -> None:
    decision = decide_unload(_policy(), DEFS, ["deploy"], "gh")
    assert decision.status == "not_loaded"
    assert "deploy" in decision.reason


def test_unload_of_ignored_stored_id_cleans_it_up() -> None:
    decision = decide_unload(_policy(denied=["secret"]), DEFS, ["secret"], "secret")
    assert decision.status == "unloaded"
    assert decision.stored == []
    assert decision.deactivated == []


# --- manifest fields ---------------------------------------------------------------------


def test_manifest_defaults_are_not_loadable_without_description() -> None:
    definition = CapabilityDefinition(capability_id="x")
    assert definition.loadable is False
    assert definition.description == ""


def test_description_is_stripped() -> None:
    assert CapabilityDefinition(capability_id="x", description="  hi  ").description == "hi"


@pytest.mark.parametrize("description", ["two\nlines", "x" * 161])
def test_description_must_be_one_short_line(description: str) -> None:
    with pytest.raises(ValidationError):
        CapabilityDefinition(capability_id="x", description=description)
