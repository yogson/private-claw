"""
Component ID: CMP_CORE_CAPABILITIES

Pydantic schemas for capability manifests (config/capabilities/*.yaml).
"""

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from assistant.core.config.schemas import CommandAllowlistEntry

# Manifests are strict (unknown keys are validation errors, so a typo such as
# ``nested_capabilites:`` fails loudly instead of being dropped) and immutable: the
# catalog shares one parsed instance across snapshots, turns and adapters.
_MANIFEST_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)


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

    model_config = _MANIFEST_MODEL_CONFIG

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
    delegation_default_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    @field_validator("command_allowlist", mode="before")
    @classmethod
    def _coerce_command_allowlist(cls, v: object) -> list[CommandAllowlistEntry] | None:
        if v is None:
            return None
        return _coerce_command_allowlist(v)


class CapabilityToolBinding(BaseModel):
    """Tool binding within a capability manifest."""

    model_config = _MANIFEST_MODEL_CONFIG

    tool_id: str
    enabled: bool = True
    params_override: CapabilityToolOverride | None = None


class ClaudeCodePermissions(BaseModel):
    """Permissions block for ~/.claude/settings.json."""

    model_config = _MANIFEST_MODEL_CONFIG

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


# Expanded size bound of free-form MCP server settings (every container and scalar
# counted once per reference). Real settings have a few dozen nodes; a YAML alias chain
# of < 700 bytes can expand to billions, which ``json.dumps`` (sub-agent launch) and any
# naive walk would try to materialise.
MAX_JSON_NODES = 100_000


def check_json_native(value: object, *, max_nodes: int = MAX_JSON_NODES) -> None:
    """Raise ``ValueError`` unless ``value`` is plain JSON: dicts with string keys, lists,
    strings, ints, finite floats, booleans and null, with at most ``max_nodes`` nodes
    once expanded and no cycles.

    MCP server settings are handed to the Claude Code CLI as ``json.dumps`` output. YAML
    can also produce dates, bytes (``!!binary``), sets (``!!set``), ``.nan`` / ``.inf``
    and non-string keys, which either crash ``json.dumps`` when a sub-agent starts or
    become invalid / silently converted JSON.

    ``yaml.safe_load`` shares the object of an anchor between all its aliases, and an
    alias can even point to its own ancestor (a cycle). The walk is iterative (deep
    nesting cannot overflow) and memoised by object identity: each distinct container is
    checked once, so its cost is linear in the size of the YAML document, and the
    expanded size is computed from the memo without expanding anything.
    """
    sizes: dict[int, int] = {}  # id of a checked container -> its expanded node count
    on_path: set[int] = set()  # containers entered but not finished (ancestors)
    stack: list[tuple[object, str, bool]] = [(value, "", False)]
    while stack:
        item, path, done = stack.pop()
        if not isinstance(item, dict | list):
            _check_json_scalar(item, path)
            continue
        key = id(item)
        children = list(item.values()) if isinstance(item, dict) else item
        if done:
            on_path.discard(key)
            total = 1 + sum(sizes.get(id(c), 1) for c in children)
            if total > max_nodes:
                raise ValueError(
                    f"{path or 'value'}: more than {max_nodes} values once YAML aliases are "
                    "expanded; MCP server settings must be small plain JSON"
                )
            sizes[key] = total
            continue
        if key in sizes:
            continue  # shared through an alias and already checked
        if key in on_path:
            raise ValueError(
                f"{path or 'value'}: refers to itself (recursive YAML alias); MCP server "
                "settings must be plain JSON"
            )
        on_path.add(key)
        stack.append((item, path, True))
        if isinstance(item, dict):
            for sub_key, sub in item.items():
                if not isinstance(sub_key, str):
                    raise ValueError(
                        f"{path or 'value'}: key of type {type(sub_key).__name__}; MCP server "
                        "settings must be plain JSON (string keys only)"
                    )
                stack.append((sub, f"{path}.{sub_key}" if path else sub_key, False))
        else:
            stack.extend((sub, f"{path}[{i}]", False) for i, sub in enumerate(item))


def _check_json_scalar(item: object, path: str) -> None:
    if isinstance(item, float) and not math.isfinite(item):
        raise ValueError(f"{path or 'value'}: {item} is not valid JSON")
    if item is not None and not isinstance(item, str | int | float):  # bool is an int
        raise ValueError(
            f"{path or 'value'}: value of type {type(item).__name__}; MCP server settings "
            "must be plain JSON (strings, numbers, booleans, null, lists, mappings)"
        )


class ClaudeCodeSettings(BaseModel):
    """Settings to merge into ~/.claude/settings.json when capability is activated."""

    model_config = _MANIFEST_MODEL_CONFIG

    permissions: ClaudeCodePermissions = Field(default_factory=ClaudeCodePermissions)
    # Merged into settings.json as top-level "mcpServers" (Claude Code key); handed to
    # sub-agents as JSON, so only plain JSON values are accepted.
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("mcp_servers")
    @classmethod
    def _json_native(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        check_json_native(v)
        return v


class CapabilityDefinition(BaseModel):
    """Capability manifest schema (config/capabilities/*.yaml).

    Each file defines prompt + toolset + optional per-tool overrides.
    """

    model_config = _MANIFEST_MODEL_CONFIG

    capability_id: str
    prompt: str = ""
    tools: list[CapabilityToolBinding] = Field(default_factory=list)
    tool_overrides: dict[str, CapabilityToolOverride] = Field(default_factory=dict)
    claude_code_settings: ClaudeCodeSettings | None = None
    nested_capabilities: list[str] = Field(default_factory=list)
    # Whole MCP servers (ids from config/mcp_servers.yaml) whose tools this capability grants.
    mcp_servers: list[str] = Field(default_factory=list)
    # Opt-in for agent-proposed edits (CapabilityEditService, kind ``capability_edit``):
    # only manifests a human marked ``self_edit: true`` may be edited, and only after the
    # user approves each edit with buttons. The agent can never change this flag itself.
    # Strict: only a real YAML boolean opts in ("yes", "true", 1 are validation errors).
    self_edit: StrictBool = False

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
