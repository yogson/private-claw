"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Manifest discovery, validation, and registry lifecycle for capabilities and skills.
"""

from assistant.extensions.registry.models import (
    CapabilityManifest,
    CapabilityPermissions,
    SkillManifest,
)
from assistant.extensions.registry.registry import CapabilityRegistry

__all__ = [
    "CapabilityManifest",
    "CapabilityPermissions",
    "SkillManifest",
    "CapabilityRegistry",
]
