"""
Component ID: CMP_CORE_CONFIG_SCHEMAS

Pydantic schemas for all configuration domains.
Each schema corresponds to one config/*.yaml file.
"""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator

# Claude Code permission modes a delegated sub-agent may run in (CLI config
# values; the CLI's "manual" is an alias of "default").  See
# assistant.subagents.permission_modes for how they are resolved per run.
SubagentPermissionMode = Literal[
    "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"
]


def normalize_permission_mode_alias(value: object) -> object:
    """Strip a permission-mode string and map the CLI alias "manual" to "default".

    Shared by config validation and the per-request resolver so both accept the
    same spellings.  Anything else is returned as-is for the caller to validate
    (mode names are case-sensitive, as in the CLI).
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    return "default" if stripped.lower() == "manual" else stripped


# Config field type: same values as SubagentPermissionMode, plus the "manual" alias.
SubagentPermissionModeSetting = Annotated[
    SubagentPermissionMode, BeforeValidator(normalize_permission_mode_alias)
]


class RuntimeMode(StrEnum):
    PROD = "prod"
    DEV = "dev"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class AppConfig(BaseModel):
    """Application runtime configuration (config/app.yaml)."""

    runtime_mode: RuntimeMode = RuntimeMode.PROD
    data_root: str
    timezone: str
    log_level: LogLevel = LogLevel.INFO
    logfire_token: str = ""
    # How long a user confirmation (e.g. memory update buttons) stays valid.
    confirmation_ttl_seconds: int = Field(default=86400, ge=60)
    # Versions kept per capability in <config_dir>/.capability_history (agent manifest edits).
    capability_history_retention: int = Field(default=50, ge=1, le=10000)
    # Most messages a capability_edit may use to show a prompt change in full (each part
    # is one Telegram message sent before the buttons); bigger prompt edits are refused.
    capability_edit_max_prompt_parts: int = Field(default=8, ge=1, le=50)
    # Optional label for this process when several instances share one config dir
    # (set per instance via ASSISTANT_APP_INSTANCE_NAME). Shown in logs and the
    # "System started" message; empty keeps the default output.
    instance_name: str = ""
    # Root for data shared by all instances on this machine (sub-agent worktrees and the
    # cross-instance in_place lock). Empty: data_root (a single process shares with
    # itself). The launcher (make up) sets it to <repo>/data via
    # ASSISTANT_APP_SHARED_DATA_ROOT unless that variable is already set.
    shared_data_root: str = ""

    @property
    def effective_shared_data_root(self) -> str:
        return self.shared_data_root.strip() or self.data_root


class TelegramChannelConfig(BaseModel):
    """Telegram channel configuration (config/channel.telegram.yaml).

    Uses polling for update delivery (no webhook). Suitable for local/single-user usage.
    """

    enabled: bool = False
    bot_token: str = ""
    allowlist: list[int] = Field(default_factory=list)
    poll_timeout_seconds: int = Field(default=30, ge=1)
    poll_interval_seconds: float = Field(default=0.0, ge=0)
    startup_drop_pending_updates: bool = Field(default=False)
    mtproto_api_id: int | None = None
    mtproto_api_hash: str | None = None
    transcription_timeout_seconds: int = Field(default=10, ge=1)
    throttle_max_per_minute: int = Field(default=20, ge=1)
    max_attachment_size_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    session_resume_hmac_secret: str = Field(default="")
    session_resume_max_sessions: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def validate_when_enabled(self) -> "TelegramChannelConfig":
        if self.enabled:
            if not self.bot_token.strip():
                raise ValueError("bot_token must not be empty when enabled=true")
            if not self.allowlist:
                raise ValueError("allowlist must contain at least one user ID when enabled=true")
        has_api_id = self.mtproto_api_id is not None
        has_api_hash = self.mtproto_api_hash is not None
        if has_api_id != has_api_hash:
            raise ValueError(
                "mtproto_api_id and mtproto_api_hash must both be set or both be absent"
            )
        return self


class QualityRouting(StrEnum):
    QUALITY_FIRST = "quality_first"
    COST_FIRST = "cost_first"


class ModelConfig(BaseModel):
    """LLM model configuration (config/model.yaml)."""

    default_model_id: str
    model_allowlist: list[str]
    quality_routing: QualityRouting = QualityRouting.QUALITY_FIRST
    max_tokens_default: int = Field(default=4096, ge=1)
    prompt_trace_enabled: bool = False

    @field_validator("model_allowlist")
    @classmethod
    def allowlist_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("model_allowlist must contain at least one model ID")
        return v

    @model_validator(mode="after")
    def default_in_allowlist(self) -> "ModelConfig":
        if self.model_allowlist and self.default_model_id not in self.model_allowlist:
            raise ValueError(
                f"default_model_id '{self.default_model_id}' must be in model_allowlist"
            )
        return self


class CommandAllowlistEntry(BaseModel):
    """Single command template for shell_execute_allowlisted tool."""

    id: str
    command_pattern: str
    allowed_args_pattern: str = ".*"
    max_timeout_seconds: int = Field(default=30, ge=1)


def _coerce_command_allowlist(v: object) -> list[CommandAllowlistEntry]:
    """Accept list[str] or list[dict] as command_allowlist."""
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


class ToolDefaultParams(BaseModel):
    """Default params for tools, merged with capability-level overrides."""

    shell_readonly_commands: list[str] | None = None
    command_allowlist: list[CommandAllowlistEntry] | None = None
    default_timeout_seconds: int | None = Field(default=None, ge=1)
    max_timeout_seconds: int | None = Field(default=None, ge=1)
    delegation_allowed_backends: list[str] | None = None
    delegation_default_model_id: str | None = None
    delegation_model_allowlist: list[str] | None = None
    delegation_default_ttl_seconds: int | None = Field(default=None, ge=1)
    delegation_max_ttl_seconds: int | None = Field(default=None, ge=1)
    delegation_max_concurrent_tasks: int | None = Field(default=None, ge=1)
    delegation_per_task_token_cap: int | None = Field(default=None, ge=1)
    delegation_per_session_token_cap: int | None = Field(default=None, ge=1)
    delegation_global_token_cap: int | None = Field(default=None, ge=1)
    delegation_budget_window_seconds: int | None = Field(default=None, ge=1)
    delegation_estimated_tokens_per_turn: int | None = Field(default=None, ge=1)
    delegation_default_max_turns: int | None = Field(default=None, ge=1)
    delegation_default_timeout_seconds: int | None = Field(default=None, ge=1)
    delegation_default_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # Claude Code permission mode for sub-agents unless the call sets its own;
    # unset means "auto".  It is also the ceiling: a per-request mode may only be
    # as restrictive or more.  "bypassPermissions" only when set explicitly.
    delegation_default_permission_mode: SubagentPermissionModeSetting | None = None
    # Sub-agent workspaces (worktree / readonly / in_place, see docs domains/subagents.md).
    # Read from the tools.yaml defaults only: capability overrides of these keys are ignored,
    # so every task and the startup cleanup agree on one worktree root.
    # Where coordinator-managed worktrees live; default <shared_data_root>/subagent_worktrees.
    delegation_worktrees_root: str | None = None
    # Base for new worktree branches when the call sets none; default: the repo's
    # origin/HEAD, else origin/master, origin/main, master, main.
    delegation_worktree_default_base: str | None = None
    # A clean, fully pushed managed worktree unused for this long is removed (default 7).
    delegation_worktree_ttl_days: int | None = Field(default=None, ge=1)
    # Delete the local branch when a managed worktree is removed because its PR was
    # merged (default true). Branches are never deleted otherwise.
    delegation_worktree_delete_merged_branches: bool | None = None
    # How long an in_place task waits for the checkout lock before failing (default 1800).
    delegation_in_place_lock_timeout_seconds: int | None = Field(default=None, ge=1)

    @field_validator("command_allowlist", mode="before")
    @classmethod
    def _coerce_command_allowlist(cls, v: object) -> list[CommandAllowlistEntry] | None:
        if v is None:
            return None
        return _coerce_command_allowlist(v)


class ToolDefinition(BaseModel):
    """Single tool definition in config/tools.yaml."""

    tool_id: str
    entrypoint: str
    enabled: bool = True
    max_retries: int | None = Field(default=None, ge=0)
    default_params: ToolDefaultParams | None = None

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, v: str) -> str:
        if not v or ":" not in v:
            raise ValueError("entrypoint must be <python_module>:<callable_name> format")
        return v.strip()


class ToolsConfig(BaseModel):
    """Tools configuration (config/tools.yaml)."""

    tools: list[ToolDefinition] = Field(default_factory=list)


class CapabilitiesPolicyConfig(BaseModel):
    """Operator-level capability policy (config/capabilities.yaml).

    Lists which capability manifests from config/capabilities/*.yaml are enabled.
    """

    enabled_capabilities: list[str] = Field(default_factory=list)
    denied_capabilities: list[str] = Field(default_factory=list)


class McpServerEntry(BaseModel):
    """Single MCP server entry.

    The whole server is connected and its tools are discovered at runtime; there is no
    per-tool mapping. Tools reach the model as ``mcp_<id>_<tool>``.

    Supports three transports:
    - sse (default): url points to the SSE endpoint.
    - stdio: command + args spawn the server process.
    - streamable-http: url points to the HTTP endpoint.
    """

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    transport: Literal["stdio", "streamable-http", "sse"] = "sse"
    url: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    # Environment for stdio servers.
    env: dict[str, str] = Field(default_factory=dict)
    # Extra HTTP headers for sse / streamable-http (e.g. Authorization).
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    # Server-side tool names. At most one of the two may be set.
    include_tools: list[str] | None = None
    exclude_tools: list[str] | None = None
    # Server-side tool names that need user confirmation before they run.
    confirm_tools: list[str] = Field(default_factory=list)
    # Append the server's own instructions to the agent's instructions.
    include_instructions: bool = True
    # Per-server override of timeouts.call_seconds (e.g. for slow data sources).
    call_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "McpServerEntry":
        if self.transport in ("sse", "streamable-http"):
            if not self.url:
                raise ValueError(
                    f"url is required for transport={self.transport!r} (server: {self.id})"
                )
            if self.command or self.args or self.env:
                raise ValueError(
                    f"command/args/env apply to transport='stdio' only (server: {self.id})"
                )
        else:
            if not self.command:
                raise ValueError(f"command is required for transport='stdio' (server: {self.id})")
            if self.url or self.headers:
                raise ValueError(f"url/headers apply to HTTP transports only (server: {self.id})")
        if self.include_tools is not None and self.exclude_tools is not None:
            raise ValueError(
                f"include_tools and exclude_tools are mutually exclusive (server: {self.id})"
            )
        return self


class McpDefaults(BaseModel):
    """Default settings applied to all MCP servers."""

    enabled: bool = False


class McpTimeouts(BaseModel):
    """MCP connection and call timeouts."""

    connect_seconds: int = Field(default=10, ge=1)
    call_seconds: int = Field(default=30, ge=1)


class McpServersConfig(BaseModel):
    """MCP servers configuration (config/mcp_servers.yaml)."""

    servers: list[McpServerEntry] = Field(default_factory=list)
    defaults: McpDefaults = Field(default_factory=McpDefaults)
    timeouts: McpTimeouts = Field(default_factory=McpTimeouts)


class RetryPolicy(BaseModel):
    """Scheduler retry policy."""

    max_attempts: int = Field(default=3, ge=1)
    backoff_seconds: int = Field(default=60, ge=1)


class SchedulerConfig(BaseModel):
    """Scheduler configuration (config/scheduler.yaml)."""

    tick_seconds: int = Field(default=10, ge=1)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    max_lateness_seconds: int = Field(default=300, ge=0)
    max_jobs: int = Field(default=500, ge=1)


class StoreBackend(StrEnum):
    FILESYSTEM = "filesystem"


class MemoryConfig(BaseModel):
    """Mem0 Platform memory configuration (config/memory.yaml).

    api_key is required when Mem0 is the active memory backend.
    Env override prefix: ASSISTANT_MEMORY_
    """

    api_key: str = Field(default="", description="Mem0 Platform API key")
    default_user_id: str = Field(
        default="default",
        description="Fallback user_id when session has no user_id",
    )
    org_id: str | None = Field(
        default=None,
        description="Unused: mem0ai 2.x resolves the org from the API key",
    )
    project_id: str | None = Field(
        default=None,
        description="Unused: mem0ai 2.x resolves the project from the API key",
    )


class StoreConfig(BaseModel):
    """Store / persistence configuration (config/store.yaml)."""

    backend: StoreBackend = StoreBackend.FILESYSTEM
    lock_ttl_seconds: int = Field(default=30, ge=1)
    atomic_write: bool = True
    idempotency_retention_seconds: int = Field(default=86400, ge=1)


class CompactionConfig(BaseModel):
    """Chat compaction configuration (config/compaction.yaml)."""

    enabled: bool = False
    token_threshold: int = Field(default=100_000, ge=10_000)
    summarizer_model_id: str = "claude-haiku-4-5"
    summarizer_max_tokens: int = Field(default=2048, ge=256)
    min_turns_before_compact: int = Field(default=5, ge=2)
    max_compactions: int = Field(default=3, ge=1)
    keep_recent_turns: int = Field(default=5, ge=0)
    compaction_prompt: str = Field(
        default="",
        description=(
            "Custom system prompt for the summarizer LLM. "
            "When empty the built-in default prompt is used."
        ),
    )


class RecapConfig(BaseModel):
    """Session recap (/wtf command) configuration (config/recap.yaml)."""

    enabled: bool = True
    model_id: str = "claude-haiku-4-5"
    max_tokens: int = Field(default=1024, ge=64)
    max_input_chars: int = Field(
        default=30_000,
        ge=1_000,
        description="Dialogue character budget; the most recent messages are kept.",
    )
    prompt: str = Field(
        default="",
        description=(
            "Custom system prompt for the recap LLM. "
            "When empty the built-in default prompt is used."
        ),
    )


class AgentNodeRepositoryConfig(BaseModel):
    """One repository the agent-node HTTP ingress may run jobs against."""

    path: str = Field(min_length=1, description="Absolute path of the local checkout")
    # Capability ids whose prompts are added to the context of every job on this repo
    # (nested capabilities included). Many-to-many: a capability may serve several repos.
    # ``capability: <id>`` is accepted as shorthand for a single entry.
    capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _merge_capability_shorthand(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "capability" not in data:
            return data
        data = dict(data)
        single = data.pop("capability")
        singles = single if isinstance(single, list) else [single]
        listed = data.get("capabilities") or []
        if not isinstance(listed, list):
            listed = [listed]
        merged: list[Any] = []
        for item in [*singles, *listed]:
            if item is not None and item not in merged:
                merged.append(item)
        data["capabilities"] = merged
        return data

    @field_validator("capabilities")
    @classmethod
    def _capability_ids_not_blank(cls, v: list[str]) -> list[str]:
        cleaned = [c.strip() for c in v]
        if any(not c for c in cleaned):
            raise ValueError("capability ids must not be blank")
        return list(dict.fromkeys(cleaned))

    @field_validator("path")
    @classmethod
    def _path_is_absolute(cls, v: str) -> str:
        if not Path(v).expanduser().is_absolute():
            raise ValueError("repository path must be absolute")
        return v


class AgentNodeConfig(BaseModel):
    """agent-node compatible HTTP ingress (config/agent_node.yaml).

    The whole domain is optional: an absent file means the ingress is disabled.
    Env override prefix: ASSISTANT_AGENT_NODE_ (e.g. ASSISTANT_AGENT_NODE_AUTH_TOKEN).
    """

    enabled: bool = False
    auth_token: str = ""
    default_model: str | None = None
    backend: str = "claude_code_streaming"
    # Claude Code permission mode for ingress jobs. "auto" lets the auto-mode
    # classifier approve safe actions and block risky ones; anything it would
    # escalate to a human is denied, since ingress runs are unattended.
    # Capped by the delegation ceiling (delegate_subagent_task's
    # delegation_default_permission_mode), like any per-request mode.
    permission_mode: SubagentPermissionModeSetting = "auto"
    repositories: dict[str, AgentNodeRepositoryConfig] = Field(default_factory=dict)
    webhook_timeout_seconds: float = Field(default=10.0, ge=1)
    # The webhook is abandoned this long after the job's effective deadline.
    webhook_deadline_margin_seconds: int = Field(default=149, ge=0)

    def auth_token_problem(self) -> str | None:
        """Why ``auth_token`` cannot be used as a bearer token, or None when it is fine."""
        token = self.auth_token
        if not token.strip():
            return "auth_token is empty (set ASSISTANT_AGENT_NODE_AUTH_TOKEN)"
        if not token.isascii():
            return "auth_token must be ASCII"
        if token != token.strip():
            return "auth_token must not have leading or trailing whitespace"
        return None


class RuntimeConfig(BaseModel):
    """Aggregated runtime configuration across all domains."""

    app: AppConfig
    telegram: TelegramChannelConfig
    model: ModelConfig
    capabilities: CapabilitiesPolicyConfig
    tools: ToolsConfig
    mcp_servers: McpServersConfig
    scheduler: SchedulerConfig
    store: StoreConfig
    memory: MemoryConfig
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    recap: RecapConfig = Field(default_factory=RecapConfig)
    agent_node: AgentNodeConfig | None = None  # None: config/agent_node.yaml absent
    config_dir: Path | None = None  # Injected by loader; used for capability/tool resolution
