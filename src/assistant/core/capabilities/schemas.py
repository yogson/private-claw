"""
Component ID: CMP_CORE_CAPABILITIES

Pydantic schemas for capability manifests (config/capabilities/*.yaml).
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator

from assistant.core.config.schemas import CommandAllowlistEntry


def _coerce_command_allowlist(v: object) -> list[CommandAllowlistEntry]:
    if not isinstance(v, list):
        return []
    result: list[CommandAllowlistEntry] = []
    for item in v:
        if isinstance(item, str) and item.strip():
            result.append(
                CommandAllowlistEntry(
                    id=item.strip().lower(),
                    command_pattern=item.strip(),
                    allowed_args_pattern=".*",
                    max_timeout_seconds=30,
                )
            )
        elif isinstance(item, dict):
            result.append(CommandAllowlistEntry(**item))
    return result


class CapabilityToolOverride(BaseModel):
    """Per-tool param overrides within a capability."""

    shell_readonly_commands: list[str] | None = None
    command_allowlist: list[CommandAllowlistEntry] | None = None
    default_timeout_seconds: int | None = None
    max_timeout_seconds: int | None = None
    delegation_allowed_backends: list[str] | None = None
    delegation_default_model_id: str | None = None
    delegation_model_allowlist: list[str] | None = None
    delegation_default_ttl_seconds: int | None = None
    delegation_max_ttl_seconds: int | None = None
    delegation_max_concurrent_tasks: int | None = None
    delegation_per_task_token_cap: int | None = None
    delegation_per_session_token_cap: int | None = None
    delegation_global_token_cap: int | None = None
    delegation_budget_window_seconds: int | None = None
    delegation_estimated_tokens_per_turn: int | None = None
    delegation_default_max_turns: int | None = None
    delegation_default_timeout_seconds: int | None = None

    @field_validator("command_allowlist", mode="before")
    @classmethod
    def _coerce_command_allowlist(cls, v: object) -> list[CommandAllowlistEntry] | None:
        if v is None:
            return None
        return _coerce_command_allowlist(v)


class CapabilityToolBinding(BaseModel):
    """Tool binding within a capability manifest."""

    tool_id: str
    enabled: bool = True
    params_override: CapabilityToolOverride | None = None


class ClaudeCodePermissions(BaseModel):
    """Permissions block for ~/.claude/settings.json."""

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class ClaudeCodeSettings(BaseModel):
    """Settings to merge into ~/.claude/settings.json when capability is activated."""

    permissions: ClaudeCodePermissions = Field(default_factory=ClaudeCodePermissions)
    # Merged into settings.json as top-level "mcpServers" (Claude Code key).
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class CapabilityDefinition(BaseModel):
    """Capability manifest schema (config/capabilities/*.yaml).

    Each file defines prompt + toolset + optional per-tool overrides.
    """

    capability_id: str
    prompt: str = ""
    tools: list[CapabilityToolBinding] = Field(default_factory=list)
    tool_overrides: dict[str, CapabilityToolOverride] = Field(default_factory=dict)
    claude_code_settings: ClaudeCodeSettings | None = None

    def get_effective_tool_overrides(self, tool_id: str) -> dict[str, Any]:
        """Merge tool_overrides[tool_id] with any inline params from tools list."""
        effective: dict[str, Any] = {}
        for binding in self.tools:
            if binding.tool_id != tool_id:
                continue
            if binding.params_override is not None:
                for k, v in binding.params_override.model_dump(exclude_none=True).items():
                    effective[k] = v
        override = self.tool_overrides.get(tool_id)
        if override is not None:
            for k, v in override.model_dump(exclude_none=True).items():
                effective[k] = v
        return effective
