"""resolve_active_capabilities: the single nested-expansion + denied-filter function."""

import pytest
from structlog.testing import capture_logs

from assistant.core.capabilities.resolver import (
    expand_nested_capabilities,
    find_missing_capabilities,
    resolve_active_capabilities,
)
from assistant.core.capabilities.schemas import CapabilityDefinition


def _defs(**nested: list[str]) -> dict[str, CapabilityDefinition]:
    return {
        cap_id: CapabilityDefinition(capability_id=cap_id, nested_capabilities=children)
        for cap_id, children in nested.items()
    }


def test_nesting_is_depth_first_and_deterministic() -> None:
    defs = _defs(a=["b", "c"], b=["d"], c=[], d=[], e=[])
    assert resolve_active_capabilities(["a", "e"], [], defs) == ["a", "b", "d", "c", "e"]


def test_cycles_are_safe() -> None:
    defs = _defs(a=["b"], b=["a"])
    assert resolve_active_capabilities(["a"], [], defs) == ["a", "b"]


def test_denied_parent_prunes_its_subtree() -> None:
    defs = _defs(a=["b"], b=["c"], c=[])
    assert resolve_active_capabilities(["a"], ["a"], defs) == []


def test_denied_child_is_excluded_with_its_subtree() -> None:
    defs = _defs(a=["b", "d"], b=["c"], c=[], d=[])
    assert resolve_active_capabilities(["a"], ["b"], defs) == ["a", "d"]


@pytest.mark.parametrize(
    "enabled", [["denied_parent", "ok_parent"], ["ok_parent", "denied_parent"]]
)
def test_shared_child_reachable_via_non_denied_parent_stays(enabled: list[str]) -> None:
    defs = _defs(
        denied_parent=["shared", "only_denied"], ok_parent=["shared"], shared=[], only_denied=[]
    )
    active = resolve_active_capabilities(enabled, ["denied_parent"], defs)
    assert active == ["ok_parent", "shared"]


def test_explicitly_enabled_child_of_denied_parent_stays() -> None:
    defs = _defs(a=["b"], b=[])
    assert resolve_active_capabilities(["a", "b"], ["a"], defs) == ["b"]


def test_missing_manifest_is_dropped_with_warning() -> None:
    defs = _defs(a=["ghost"])
    with capture_logs() as logs:
        assert resolve_active_capabilities(["a", "nope"], [], defs) == ["a"]
    warned = {
        e["capability_id"] for e in logs if e["event"] == "capabilities.resolve.missing_manifest"
    }
    assert warned == {"ghost", "nope"}


def test_expand_keeps_missing_ids_and_find_missing_reports_them() -> None:
    defs = _defs(a=["ghost"])
    assert expand_nested_capabilities(["a"], defs) == ["a", "ghost"]
    assert find_missing_capabilities(["a", "nope"], defs) == ["ghost", "nope"]
