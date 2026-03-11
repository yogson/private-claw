"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Pydantic schemas for all configuration domains.
Each schema corresponds to one config/*.yaml file.
"""

from enum import StrEnum

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
    """Telegram channel configuration (config/channel.telegram.yaml)."""

    enabled: bool = False
    bot_token: str = ""
    allowlist: list[int] = Field(default_factory=list)
    webhook_url: str = ""
    webhook_secret_token: str = ""

    @model_validator(mode="after")
    def validate_when_enabled(self) -> "TelegramChannelConfig":
        if self.enabled:
            if not self.bot_token.strip():
                raise ValueError("bot_token must not be empty when enabled=true")
            if not self.allowlist:
                raise ValueError("allowlist must contain at least one user ID when enabled=true")
            if not self.webhook_url.strip():
                raise ValueError("webhook_url must not be empty when enabled=true")
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


class CapabilitiesConfig(BaseModel):
    """Capabilities and skills configuration (config/capabilities.yaml)."""

    allowed_capabilities: list[str]
    denied_capabilities: list[str] = Field(default_factory=list)
    command_allowlist: list[str] = Field(default_factory=list)


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
    capabilities: CapabilitiesConfig
    mcp_servers: McpServersConfig
    scheduler: SchedulerConfig
    store: StoreConfig
