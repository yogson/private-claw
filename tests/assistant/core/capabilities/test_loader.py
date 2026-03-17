"""Unit tests for load_capability_definitions and startup validation."""

from pathlib import Path

import pytest
import yaml

from assistant.core.capabilities.loader import (
    CapabilityLoadError,
    discover_capability_manifests,
    load_capability_definitions,
)


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


def test_load_valid_manifest_with_delegation_workflow(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities"
    cap_dir.mkdir()
    (cap_dir / "delegation.yaml").write_text(
        yaml.dump(
            {
                "capability_id": "delegation_coding",
                "prompt": "",
                "tools": [{"tool_id": "delegate_subagent_task", "enabled": True}],
                "delegation": {
                    "workflow_id": "coding_flow",
                    "backend": "claude_code",
                    "stages": [
                        {
                            "stage_id": "implement",
                            "purpose": "implement requested changes",
                            "model_id": "claude-sonnet-4-5",
                            "timeout_seconds": 600,
                            "max_turns": 8,
                        }
                    ],
                },
            }
        )
    )
    result = load_capability_definitions(config_dir=tmp_path)
    assert "delegation_coding" in result
    workflow = result["delegation_coding"].delegation
    assert workflow is not None
    assert workflow.backend == "claude_code"
    assert workflow.stages[0].stage_id == "implement"
