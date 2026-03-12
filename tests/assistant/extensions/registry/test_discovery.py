"""Unit tests for manifest discovery."""

import tempfile
from pathlib import Path

from assistant.extensions.registry.discovery import (
    discover_capability_manifests,
    discover_skill_manifests,
)


def test_discover_capability_manifests_finds_manifests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "capabilities" / "mem").mkdir(parents=True)
        (root / "capabilities" / "mem" / "manifest.yaml").write_text("x: 1")
        (root / "capabilities" / "other").mkdir(parents=True)
        (root / "capabilities" / "other" / "manifest.yaml").write_text("y: 2")
        paths = discover_capability_manifests([root])
        assert len(paths) == 2
        assert all(p.name == "manifest.yaml" for p in paths)


def test_discover_skill_manifests_finds_manifests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "skills" / "reminder").mkdir(parents=True)
        (root / "skills" / "reminder" / "manifest.yaml").write_text("x: 1")
        paths = discover_skill_manifests([root])
        assert len(paths) == 1
        assert paths[0].name == "manifest.yaml"
