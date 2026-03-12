"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Capability and skill registry with manifest discovery, validation, and lifecycle.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from assistant.extensions.registry.discovery import (
    discover_capability_manifests,
    discover_skill_manifests,
)
from assistant.extensions.registry.models import CapabilityManifest, SkillManifest


class ManifestRegistryError(Exception):
    """Raised when registry load fails (duplicate IDs, fatal validation)."""


class ManifestLoadDiagnostic:
    """Diagnostic for a skipped or failed manifest load."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason


class CapabilityRegistry:
    """Registry for capability and skill manifests with discovery and validation."""

    def __init__(self, plugin_roots: list[Path]) -> None:
        self._plugin_roots = [Path(p) for p in plugin_roots]
        self._capabilities: dict[str, CapabilityManifest] = {}
        self._skills: dict[str, SkillManifest] = {}
        self._diagnostics: list[ManifestLoadDiagnostic] = []

    def load(self) -> None:
        """Discover, validate, and load all manifests. Raises ManifestRegistryError on failure."""
        self._diagnostics = []
        self._capabilities = {}
        self._skills = {}

        cap_paths = discover_capability_manifests(self._plugin_roots)
        for path in cap_paths:
            cap_manifest = self._load_capability_manifest(path)
            if cap_manifest is None:
                continue
            all_cap_ids = list(
                dict.fromkeys([cap_manifest.capability_id] + (cap_manifest.capabilities or []))
            )
            for cap_id in all_cap_ids:
                if cap_id in self._capabilities:
                    raise ManifestRegistryError(
                        f"Duplicate capability_id '{cap_id}' in {path} (already registered)"
                    )
                self._capabilities[cap_id] = cap_manifest

        skill_paths = discover_skill_manifests(self._plugin_roots)
        for path in skill_paths:
            skill_manifest = self._load_skill_manifest(path)
            if skill_manifest is None:
                continue
            if skill_manifest.skill_id in self._skills:
                raise ManifestRegistryError(
                    f"Duplicate skill_id '{skill_manifest.skill_id}' in {path}"
                )
            missing = [
                c for c in skill_manifest.required_capabilities if c not in self._capabilities
            ]
            if missing:
                self._diagnostics.append(
                    ManifestLoadDiagnostic(
                        str(path),
                        f"required_capabilities not registered: {missing}",
                    )
                )
                continue
            self._skills[skill_manifest.skill_id] = skill_manifest

        if self._diagnostics:
            lines = ["Manifest load failed:"]
            for d in self._diagnostics:
                lines.append(f"  {d.path}: {d.reason}")
            raise ManifestRegistryError("\n".join(lines))

    def _load_capability_manifest(self, path: Path) -> CapabilityManifest | None:
        data = self._read_yaml(path)
        if data is None:
            return None
        try:
            return CapabilityManifest(**data)
        except ValidationError as e:
            self._diagnostics.append(ManifestLoadDiagnostic(str(path), str(e)))
            return None

    def _load_skill_manifest(self, path: Path) -> SkillManifest | None:
        data = self._read_yaml(path)
        if data is None:
            return None
        try:
            return SkillManifest(**data)
        except ValidationError as e:
            self._diagnostics.append(ManifestLoadDiagnostic(str(path), str(e)))
            return None

    def _read_yaml(self, path: Path) -> dict[str, Any] | None:
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, yaml.YAMLError) as e:
            self._diagnostics.append(ManifestLoadDiagnostic(str(path), str(e)))
            return None

    def get_capability(self, capability_id: str) -> CapabilityManifest | None:
        """Return manifest for capability_id or None."""
        return self._capabilities.get(capability_id)

    def get_skill(self, skill_id: str) -> SkillManifest | None:
        """Return manifest for skill_id or None."""
        return self._skills.get(skill_id)

    def list_capabilities(self) -> list[str]:
        """Return sorted list of registered capability IDs."""
        return sorted(self._capabilities)

    def list_skills(self) -> list[str]:
        """Return sorted list of registered skill IDs."""
        return sorted(self._skills)

    def get_diagnostics(self) -> list[ManifestLoadDiagnostic]:
        """Return diagnostics from last load (skipped manifests, parse errors)."""
        return list(self._diagnostics)
