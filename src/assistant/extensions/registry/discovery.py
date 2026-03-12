"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Manifest file discovery from plugin directory roots.
"""

from pathlib import Path


def discover_capability_manifests(plugin_roots: list[Path]) -> list[Path]:
    """Discover capability manifest paths under plugins/capabilities/*/manifest.yaml."""
    paths: list[Path] = []
    for root in plugin_roots:
        capabilities_dir = root / "capabilities"
        if not capabilities_dir.is_dir():
            continue
        for subdir in capabilities_dir.iterdir():
            if subdir.is_dir():
                manifest_path = subdir / "manifest.yaml"
                if manifest_path.is_file():
                    paths.append(manifest_path)
    return sorted(paths)


def discover_skill_manifests(plugin_roots: list[Path]) -> list[Path]:
    """Discover skill manifest paths under plugins/skills/*/manifest.yaml."""
    paths: list[Path] = []
    for root in plugin_roots:
        skills_dir = root / "skills"
        if not skills_dir.is_dir():
            continue
        for subdir in skills_dir.iterdir():
            if subdir.is_dir():
                manifest_path = subdir / "manifest.yaml"
                if manifest_path.is_file():
                    paths.append(manifest_path)
    return sorted(paths)
