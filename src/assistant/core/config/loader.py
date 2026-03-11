"""
Component ID: CMP_CORE_CONFIG_LOADER

Config domain loader: reads YAML files, applies env overrides with provenance
tracking, validates all domains, and returns RuntimeConfig.
"""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from assistant.core.config.env_utils import apply_env_overrides
from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesConfig,
    McpServersConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
)

_REDACTED = "***REDACTED***"
_SENSITIVE_KEYS = {"bot_token", "api_key", "secret", "token", "password"}
_SENSITIVE_KEY_MARKERS = ("secret", "token", "password", "api_key")
_DEFAULT_CONFIG_DIR = "config"
_CONFIG_DIR_ENV_VAR = "ASSISTANT_CONFIG_DIR"

# Maps domain name → (yaml filename, Pydantic schema class, env prefix)
_DOMAIN_MAP: dict[str, tuple[str, type[BaseModel], str]] = {
    "app": ("app.yaml", AppConfig, "ASSISTANT_APP"),
    "telegram": ("channel.telegram.yaml", TelegramChannelConfig, "ASSISTANT_CHANNEL_TELEGRAM"),
    "model": ("model.yaml", ModelConfig, "ASSISTANT_MODEL"),
    "capabilities": ("capabilities.yaml", CapabilitiesConfig, "ASSISTANT_CAPABILITIES"),
    "mcp_servers": ("mcp_servers.yaml", McpServersConfig, "ASSISTANT_MCP"),
    "scheduler": ("scheduler.yaml", SchedulerConfig, "ASSISTANT_SCHEDULER"),
    "store": ("store.yaml", StoreConfig, "ASSISTANT_STORE"),
}


class ConfigLoadError(Exception):
    """Raised when config loading or validation fails at startup."""


def resolve_config_dir(config_dir: str | Path | None = None) -> Path:
    """Resolve the active config directory from argument/env/default.

    Priority:
    1) explicit function argument
    2) ASSISTANT_CONFIG_DIR env var
    3) repository default `config`
    """
    if config_dir is not None:
        return Path(config_dir)
    env_config_dir = os.environ.get(_CONFIG_DIR_ENV_VAR, "").strip()
    if env_config_dir:
        return Path(env_config_dir)
    return Path(_DEFAULT_CONFIG_DIR)


class ConfigLoader:
    """Loads and validates all configuration domains from YAML files and env overrides."""

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self._config_dir = resolve_config_dir(config_dir)

    def load(self) -> RuntimeConfig:
        """Load all config domains and return aggregated RuntimeConfig.

        Raises ConfigLoadError with an actionable report on any validation failure.
        """
        errors: list[str] = []
        domains: dict[str, Any] = {}
        for domain_name, (filename, schema_cls, env_prefix) in _DOMAIN_MAP.items():
            obj, _ = self._load_domain(filename, schema_cls, env_prefix, errors)
            domains[domain_name] = obj

        if errors:
            raise ConfigLoadError(
                "Startup failed: configuration validation errors:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )
        return RuntimeConfig(**domains)

    def reload_domain(self, domain_name: str) -> Any | None:
        """Reload a single domain from disk and current env overrides.

        Used to update live runtime state after a config apply.
        Returns None if the domain name is unknown or reload fails.
        """
        spec = _DOMAIN_MAP.get(domain_name)
        if spec is None:
            return None
        filename, schema_cls, env_prefix = spec
        errors: list[str] = []
        obj, _ = self._load_domain(filename, schema_cls, env_prefix, errors)
        return obj

    def effective_config(self) -> dict[str, Any]:
        """Return effective config with secret redaction and per-field provenance.

        Performs a fresh read from disk and current env on every call.
        The provenance map is keyed as "<domain>.<field>" with values
        "file", "env_override", or "default".
        """
        config: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        for domain_name, (filename, schema_cls, env_prefix) in _DOMAIN_MAP.items():
            errors: list[str] = []
            obj, domain_provenance = self._load_domain(filename, schema_cls, env_prefix, errors)
            if obj is not None:
                raw: dict[str, Any] = obj.model_dump()
                config[domain_name] = self._redact_dict(raw)
            else:
                config[domain_name] = None
            for field, source in domain_provenance.items():
                provenance[f"{domain_name}.{field}"] = source
        return {"config": config, "provenance": provenance}

    def _load_domain(
        self,
        filename: str,
        schema_cls: type[BaseModel],
        env_prefix: str,
        errors: list[str],
    ) -> tuple[Any, dict[str, str]]:
        yaml_data = self._read_yaml(filename, errors)
        if yaml_data is None:
            return None, {}
        merged, env_overridden = apply_env_overrides(yaml_data, env_prefix)
        try:
            obj = schema_cls(**merged)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(x) for x in err["loc"])
                errors.append(f"[{filename}] {loc}: {err['msg']}")
            return None, {}
        domain_provenance = self._build_provenance(yaml_data, env_overridden, schema_cls)
        return obj, domain_provenance

    def _read_yaml(self, filename: str, errors: list[str]) -> dict[str, Any] | None:
        path = self._config_dir / filename
        if not path.exists():
            errors.append(f"Config file not found: {path}")
            return None
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except yaml.YAMLError as exc:
            errors.append(f"YAML parse error in {path}: {exc}")
            return None

    def _build_provenance(
        self,
        yaml_data: dict[str, Any],
        env_overridden: set[str],
        schema_cls: type[BaseModel],
    ) -> dict[str, str]:
        provenance: dict[str, str] = {}
        for field_name in schema_cls.model_fields:
            if field_name in env_overridden:
                provenance[field_name] = "env_override"
            elif field_name in yaml_data:
                provenance[field_name] = "file"
            else:
                provenance[field_name] = "default"
        return provenance

    def _redact_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for k, v in data.items():
            if self._is_sensitive_key(k):
                result[k] = _REDACTED
            elif isinstance(v, dict):
                result[k] = self._redact_dict(v)
            elif isinstance(v, list):
                result[k] = [
                    self._redact_dict(item) if isinstance(item, dict) else item for item in v
                ]
            else:
                result[k] = v
        return result

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        lowered = key.lower()
        return lowered in _SENSITIVE_KEYS or any(
            marker in lowered for marker in _SENSITIVE_KEY_MARKERS
        )
