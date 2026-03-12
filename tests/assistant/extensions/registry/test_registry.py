"""Unit tests for capability/skill manifest registry."""

import tempfile
from pathlib import Path

import pytest

from assistant.extensions.registry import CapabilityRegistry
from assistant.extensions.registry.models import CapabilityManifest, SkillManifest
from assistant.extensions.registry.registry import ManifestRegistryError


def _write_manifest(path: Path, data: dict) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(data, f)


def test_capability_manifest_validation() -> None:
    CapabilityManifest(
        capability_id="cap.memory.read",
        version="1.0.0",
        entrypoint="assistant.extensions.memory:read",
        capabilities=["cap.memory.read"],
        permissions={"read_only": True, "timeout_seconds": 10},
    )
    with pytest.raises(ValueError, match="capability_id must follow"):
        CapabilityManifest(
            capability_id="invalid",
            version="1.0.0",
            entrypoint="mod:fn",
            capabilities=[],
            permissions={},
        )
    with pytest.raises(ValueError, match="entrypoint must be"):
        CapabilityManifest(
            capability_id="cap.memory.read",
            version="1.0.0",
            entrypoint="no_colon",
            capabilities=[],
            permissions={},
        )
    with pytest.raises(ValueError, match="semantic version"):
        CapabilityManifest(
            capability_id="cap.memory.read",
            version="invalid",
            entrypoint="mod:fn",
            capabilities=[],
            permissions={},
        )


def test_skill_manifest_validation() -> None:
    SkillManifest(
        skill_id="reminder_skill",
        version="1.0.0",
        entrypoint="assistant.extensions.skills.reminder:run",
        required_capabilities=["cap.macos.reminders.write"],
    )
    with pytest.raises(ValueError, match="capability ref"):
        SkillManifest(
            skill_id="x",
            version="1.0.0",
            entrypoint="mod:fn",
            required_capabilities=["bad_ref"],
        )
    with pytest.raises(ValueError, match="semantic version"):
        SkillManifest(
            skill_id="x",
            version="v1",
            entrypoint="mod:fn",
            required_capabilities=[],
        )


def test_capability_manifest_requires_capabilities_and_permissions() -> None:
    with pytest.raises(ValueError):
        CapabilityManifest(
            capability_id="cap.memory.read",
            version="1.0.0",
            entrypoint="mod:fn",
            permissions={},
        )
    with pytest.raises(ValueError):
        CapabilityManifest(
            capability_id="cap.memory.read",
            version="1.0.0",
            entrypoint="mod:fn",
            capabilities=[],
        )


def test_skill_manifest_requires_required_capabilities() -> None:
    with pytest.raises(ValueError):
        SkillManifest(
            skill_id="x",
            version="1.0.0",
            entrypoint="mod:fn",
        )


def test_discovery_empty_roots() -> None:
    reg = CapabilityRegistry(plugin_roots=[])
    reg.load()
    assert reg.list_capabilities() == []
    assert reg.list_skills() == []


def test_discovery_and_load_valid_manifests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "capabilities" / "memory").mkdir(parents=True)
        _write_manifest(
            root / "capabilities" / "memory" / "manifest.yaml",
            {
                "capability_id": "cap.memory.read",
                "version": "1.0.0",
                "entrypoint": "assistant.extensions.memory:read",
                "capabilities": ["cap.memory.read"],
                "permissions": {"read_only": True, "timeout_seconds": 30},
            },
        )
        (root / "skills" / "reminder").mkdir(parents=True)
        _write_manifest(
            root / "skills" / "reminder" / "manifest.yaml",
            {
                "skill_id": "reminder_skill",
                "version": "1.0.0",
                "entrypoint": "assistant.extensions.skills.reminder:run",
                "required_capabilities": ["cap.memory.read"],
            },
        )
        reg = CapabilityRegistry(plugin_roots=[root])
        reg.load()
        assert reg.list_capabilities() == ["cap.memory.read"]
        assert reg.list_skills() == ["reminder_skill"]
        cap = reg.get_capability("cap.memory.read")
        assert cap is not None
        assert cap.capability_id == "cap.memory.read"
        skill = reg.get_skill("reminder_skill")
        assert skill is not None
        assert skill.skill_id == "reminder_skill"


def test_duplicate_capability_id_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ("mem1", "mem2"):
            (root / "capabilities" / name).mkdir(parents=True)
            _write_manifest(
                root / "capabilities" / name / "manifest.yaml",
                {
                    "capability_id": "cap.memory.read",
                    "version": "1.0.0",
                    "entrypoint": "mod:fn",
                    "capabilities": [],
                    "permissions": {},
                },
            )
        reg = CapabilityRegistry(plugin_roots=[root])
        with pytest.raises(ManifestRegistryError, match="Duplicate capability_id"):
            reg.load()


def test_duplicate_skill_id_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "capabilities" / "mem").mkdir(parents=True)
        _write_manifest(
            root / "capabilities" / "mem" / "manifest.yaml",
            {
                "capability_id": "cap.memory.read",
                "version": "1.0.0",
                "entrypoint": "mod:fn",
                "capabilities": [],
                "permissions": {},
            },
        )
        for name in ("r1", "r2"):
            (root / "skills" / name).mkdir(parents=True)
            _write_manifest(
                root / "skills" / name / "manifest.yaml",
                {
                    "skill_id": "reminder_skill",
                    "version": "1.0.0",
                    "entrypoint": "mod:fn",
                    "required_capabilities": ["cap.memory.read"],
                },
            )
        reg = CapabilityRegistry(plugin_roots=[root])
        with pytest.raises(ManifestRegistryError, match="Duplicate skill_id"):
            reg.load()


def test_skill_with_unresolved_deps_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "skills" / "orphan").mkdir(parents=True)
        _write_manifest(
            root / "skills" / "orphan" / "manifest.yaml",
            {
                "skill_id": "orphan_skill",
                "version": "1.0.0",
                "entrypoint": "mod:fn",
                "required_capabilities": ["cap.missing.xyz"],
            },
        )
        reg = CapabilityRegistry(plugin_roots=[root])
        with pytest.raises(ManifestRegistryError, match="required_capabilities not registered"):
            reg.load()


def test_invalid_manifest_raises_at_startup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "capabilities" / "bad").mkdir(parents=True)
        _write_manifest(
            root / "capabilities" / "bad" / "manifest.yaml",
            {
                "capability_id": "invalid_format",
                "version": "1.0.0",
                "entrypoint": "mod:fn",
                "capabilities": [],
                "permissions": {},
            },
        )
        reg = CapabilityRegistry(plugin_roots=[root])
        with pytest.raises(ManifestRegistryError, match="Manifest load failed") as exc_info:
            reg.load()
        assert "capability_id" in str(exc_info.value) or "naming" in str(exc_info.value)


def test_multiple_plugin_roots_merged() -> None:
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        r1, r2 = Path(tmp1), Path(tmp2)
        (r1 / "capabilities" / "a").mkdir(parents=True)
        _write_manifest(
            r1 / "capabilities" / "a" / "manifest.yaml",
            {
                "capability_id": "cap.a.read",
                "version": "1.0.0",
                "entrypoint": "mod:fn",
                "capabilities": [],
                "permissions": {},
            },
        )
        (r2 / "capabilities" / "b").mkdir(parents=True)
        _write_manifest(
            r2 / "capabilities" / "b" / "manifest.yaml",
            {
                "capability_id": "cap.b.write",
                "version": "1.0.0",
                "entrypoint": "mod:fn",
                "capabilities": [],
                "permissions": {},
            },
        )
        reg = CapabilityRegistry(plugin_roots=[r1, r2])
        reg.load()
        assert set(reg.list_capabilities()) == {"cap.a.read", "cap.b.write"}


def test_manifest_grants_multiple_capabilities() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "capabilities" / "memory").mkdir(parents=True)
        _write_manifest(
            root / "capabilities" / "memory" / "manifest.yaml",
            {
                "capability_id": "cap.memory.ops",
                "version": "1.0.0",
                "entrypoint": "mod:fn",
                "capabilities": ["cap.memory.read", "cap.memory.write"],
                "permissions": {},
            },
        )
        reg = CapabilityRegistry(plugin_roots=[root])
        reg.load()
        assert set(reg.list_capabilities()) == {
            "cap.memory.ops",
            "cap.memory.read",
            "cap.memory.write",
        }
        m = reg.get_capability("cap.memory.read")
        assert m is not None and m.capability_id == "cap.memory.ops"
