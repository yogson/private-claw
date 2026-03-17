"""
Component ID: CMP_CORE_CONFIG_SCHEMAS

Pydantic schemas for all configuration domains.
Each schema corresponds to one config/*.yaml file.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator


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
    """Single MCP server entry."""

    id: str
    url: str
    enabled: bool = True
    tool_policy: str = "deny_by_default"


class McpDefaults(BaseModel):
    """Default settings applied to all MCP servers."""

    enabled: bool = False
    tool_policy: str = "deny_by_default"


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
    org_id: str | None = Field(default=None, description="Mem0 org ID (optional)")
    project_id: str | None = Field(default=None, description="Mem0 project ID (optional)")


class StoreConfig(BaseModel):
    """Store / persistence configuration (config/store.yaml)."""

    backend: StoreBackend = StoreBackend.FILESYSTEM
    lock_ttl_seconds: int = Field(default=30, ge=1)
    atomic_write: bool = True
    idempotency_retention_seconds: int = Field(default=86400, ge=1)


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
    config_dir: Path | None = None  # Injected by loader; used for capability/tool resolution
