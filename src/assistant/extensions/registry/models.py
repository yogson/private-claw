"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Pydantic models for capability and skill manifests (CMP_DATA_MODEL_TOOL_MANIFEST,
CMP_DATA_MODEL_SKILL_MANIFEST).
"""

import re

from pydantic import BaseModel, Field, field_validator

_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")


class CapabilityPermissions(BaseModel):
    """Permissions object for capability manifests."""

    read_only: bool = False
    side_effecting: bool = False
    requires_confirmation: bool = False
    timeout_seconds: int = Field(default=30, ge=1)


class CapabilityManifest(BaseModel):
    """Capability manifest schema (CMP_DATA_MODEL_TOOL_MANIFEST).

    Loaded from plugins/capabilities/*/manifest.yaml.
    """

    capability_id: str
    version: str
    entrypoint: str
    capabilities: list[str]
    permissions: CapabilityPermissions

    @field_validator("capability_id")
    @classmethod
    def validate_capability_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("capability_id must not be empty")
        if not re.match(r"^cap\.([a-z0-9_]+\.)+[a-z0-9_]+$", v):
            raise ValueError("capability_id must follow cap.<domain>.<action> naming convention")
        return v.strip()

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("version must not be empty")
        if not _SEMVER_PATTERN.match(s):
            raise ValueError("version must be semantic version (X.Y.Z)")
        return s

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, v: str) -> str:
        if not v or ":" not in v:
            raise ValueError("entrypoint must be <python_module>:<callable_name> format")
        return v.strip()

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, v: list[str]) -> list[str]:
        for cap in v:
            if not re.match(r"^cap\.([a-z0-9_]+\.)+[a-z0-9_]+$", cap):
                raise ValueError(f"capability '{cap}' must follow cap.<domain>.<action> convention")
        return v


class SkillManifest(BaseModel):
    """Skill manifest schema (CMP_DATA_MODEL_SKILL_MANIFEST).

    Loaded from plugins/skills/*/manifest.yaml.
    """

    skill_id: str
    version: str
    entrypoint: str
    required_capabilities: list[str]
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("skill_id")
    @classmethod
    def validate_skill_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("skill_id must not be empty")
        return v.strip()

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("version must not be empty")
        if not _SEMVER_PATTERN.match(s):
            raise ValueError("version must be semantic version (X.Y.Z)")
        return s

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, v: str) -> str:
        if not v or ":" not in v:
            raise ValueError("entrypoint must be <python_module>:<callable_name> format")
        return v.strip()

    @field_validator("required_capabilities", "capabilities")
    @classmethod
    def validate_capability_refs(cls, v: list[str]) -> list[str]:
        for cap in v:
            if not re.match(r"^cap\.([a-z0-9_]+\.)+[a-z0-9_]+$", cap):
                raise ValueError(
                    f"capability ref '{cap}' must follow cap.<domain>.<action> convention"
                )
        return v
