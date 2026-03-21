"""Unit tests for load_capability_definitions and startup validation."""

from pathlib import Path

import pytest
import yaml

from assistant.core.capabilities.loader import (
    CapabilityLoadError,
    discover_capability_manifests,
    expand_nested_capabilities,
    load_capability_definitions,
)
from assistant.core.capabilities.schemas import CapabilityDefinition


def test_discover_empty_dir(tmp_path: Path) -> None:
    (tmp_path / "capabilities").mkdir()
    paths = discover_capability_manifests(tmp_path / "capabilities")
    assert paths == []


def test_discover_skips_index_yaml(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities"
    cap_dir.mkdir()
    (cap_dir / "index.yaml").write_text("x: 1")
    (cap_dir / "assistant.yaml").write_text("capability_id: assistant\nprompt: ''\ntools: []")
    paths = discover_capability_manifests(cap_dir)
    assert len(paths) == 1
    assert paths[0].name == "assistant.yaml"


def test_load_returns_empty_when_no_capabilities_dir(tmp_path: Path) -> None:
    result = load_capability_definitions(config_dir=tmp_path)
    assert result == {}


def test_load_valid_manifest(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities"
    cap_dir.mkdir()
    (cap_dir / "assistant.yaml").write_text(
        yaml.dump(
            {
                "capability_id": "assistant",
                "prompt": "You are helpful.",
                "tools": [{"tool_id": "memory_search", "enabled": True}],
            }
        )
    )
    result = load_capability_definitions(config_dir=tmp_path)
    assert "assistant" in result
    assert result["assistant"].prompt == "You are helpful."
    assert len(result["assistant"].tools) == 1
    assert result["assistant"].tools[0].tool_id == "memory_search"


def test_load_duplicate_capability_id_raises(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities"
    cap_dir.mkdir()
    manifest = {"capability_id": "dup", "prompt": "", "tools": []}
    (cap_dir / "a.yaml").write_text(yaml.dump(manifest))
    (cap_dir / "b.yaml").write_text(yaml.dump(manifest))
    with pytest.raises(CapabilityLoadError, match="duplicate capability_id"):
        load_capability_definitions(config_dir=tmp_path)


def test_load_malformed_yaml_raises(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities"
    cap_dir.mkdir()
    (cap_dir / "bad.yaml").write_text("not: valid: yaml: [")
    with pytest.raises(CapabilityLoadError, match="YAML parse error"):
        load_capability_definitions(config_dir=tmp_path)


def test_load_invalid_schema_raises(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities"
    cap_dir.mkdir()
    (cap_dir / "bad.yaml").write_text(yaml.dump({"prompt": "x"}))  # missing capability_id
    with pytest.raises(CapabilityLoadError, match="capability_id"):
        load_capability_definitions(config_dir=tmp_path)


def test_load_non_dict_content_raises(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities"
    cap_dir.mkdir()
    (cap_dir / "bad.yaml").write_text(yaml.dump(["list", "not", "dict"]))
    with pytest.raises(CapabilityLoadError, match="expected dict"):
        load_capability_definitions(config_dir=tmp_path)


def test_load_tool_overrides_merge(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities"
    cap_dir.mkdir()
    (cap_dir / "deploy.yaml").write_text(
        yaml.dump(
            {
                "capability_id": "deploy",
                "prompt": "",
                "tools": [{"tool_id": "shell_execute_readonly", "enabled": True}],
                "tool_overrides": {
                    "shell_execute_readonly": {
                        "shell_readonly_commands": ["gh", "git", "ssh"],
                    },
                },
            }
        )
    )
    result = load_capability_definitions(config_dir=tmp_path)
    assert "deploy" in result
    overrides = result["deploy"].get_effective_tool_overrides("shell_execute_readonly")
    assert overrides.get("shell_readonly_commands") == ["gh", "git", "ssh"]


def test_load_delegate_tool_overrides(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities"
    cap_dir.mkdir()
    (cap_dir / "delegation.yaml").write_text(
        yaml.dump(
            {
                "capability_id": "delegation_coding",
                "prompt": "",
                "tools": [{"tool_id": "delegate_subagent_task", "enabled": True}],
                "tool_overrides": {
                    "delegate_subagent_task": {
                        "delegation_model_allowlist": ["claude-sonnet-4-5"],
                    }
                },
            }
        )
    )
    result = load_capability_definitions(config_dir=tmp_path)
    assert "delegation_coding" in result
    overrides = result["delegation_coding"].get_effective_tool_overrides("delegate_subagent_task")
    assert overrides["delegation_model_allowlist"] == ["claude-sonnet-4-5"]


# ── nested_capabilities field ───────────────────────────────────────────────


def test_nested_capabilities_field_defaults_to_empty() -> None:
    cap = CapabilityDefinition(capability_id="test")
    assert cap.nested_capabilities == []


def test_nested_capabilities_field_loaded_from_yaml(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities"
    cap_dir.mkdir()
    (cap_dir / "parent.yaml").write_text(
        yaml.dump(
            {
                "capability_id": "parent",
                "prompt": "",
                "tools": [],
                "nested_capabilities": ["child"],
            }
        )
    )
    (cap_dir / "child.yaml").write_text(
        yaml.dump({"capability_id": "child", "prompt": "child prompt", "tools": []})
    )
    result = load_capability_definitions(config_dir=tmp_path)
    assert result["parent"].nested_capabilities == ["child"]


# ── expand_nested_capabilities ───────────────────────────────────────────────


def _make_def(cap_id: str, nested: list[str] | None = None) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=cap_id,
        nested_capabilities=nested or [],
    )


def test_expand_no_nesting() -> None:
    defs = {"a": _make_def("a"), "b": _make_def("b")}
    assert expand_nested_capabilities(["a", "b"], defs) == ["a", "b"]


def test_expand_single_level_nesting() -> None:
    defs = {
        "parent": _make_def("parent", ["child"]),
        "child": _make_def("child"),
    }
    result = expand_nested_capabilities(["parent"], defs)
    assert result == ["parent", "child"]


def test_expand_multi_level_nesting() -> None:
    defs = {
        "a": _make_def("a", ["b"]),
        "b": _make_def("b", ["c"]),
        "c": _make_def("c"),
    }
    result = expand_nested_capabilities(["a"], defs)
    assert result == ["a", "b", "c"]


def test_expand_multiple_nested_caps() -> None:
    defs = {
        "a": _make_def("a", ["b", "c"]),
        "b": _make_def("b"),
        "c": _make_def("c"),
    }
    result = expand_nested_capabilities(["a"], defs)
    assert result == ["a", "b", "c"]


def test_expand_deduplicates_shared_nested() -> None:
    """Two parent caps that both nest the same child should include child once."""
    defs = {
        "p1": _make_def("p1", ["shared"]),
        "p2": _make_def("p2", ["shared"]),
        "shared": _make_def("shared"),
    }
    result = expand_nested_capabilities(["p1", "p2"], defs)
    assert result.count("shared") == 1
    assert set(result) == {"p1", "p2", "shared"}


def test_expand_cycle_safe() -> None:
    """Circular nested_capabilities references must not cause infinite recursion."""
    defs = {
        "a": _make_def("a", ["b"]),
        "b": _make_def("b", ["a"]),
    }
    result = expand_nested_capabilities(["a"], defs)
    assert set(result) == {"a", "b"}


def test_expand_missing_nested_cap_not_added() -> None:
    """If a nested capability has no manifest, it is still included in the expansion."""
    defs = {
        "parent": _make_def("parent", ["ghost"]),
    }
    result = expand_nested_capabilities(["parent"], defs)
    # ghost is referenced but has no manifest — still expanded (validation is bootstrap's job)
    assert "ghost" in result


def test_expand_preserves_order() -> None:
    defs = {
        "a": _make_def("a", ["b", "c"]),
        "b": _make_def("b"),
        "c": _make_def("c"),
        "d": _make_def("d"),
    }
    result = expand_nested_capabilities(["a", "d"], defs)
    assert result == ["a", "b", "c", "d"]
