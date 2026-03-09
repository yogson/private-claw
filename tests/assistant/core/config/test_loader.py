"""Tests for ConfigLoader: loading, provenance, redaction, and hot-reload."""

from pathlib import Path

import pytest
import yaml

from assistant.core.config.loader import ConfigLoader, ConfigLoadError
from assistant.core.config.schemas import RuntimeConfig


class TestLoad:
    def test_load_returns_runtime_config(self, config_dir: Path) -> None:
        cfg = ConfigLoader(config_dir).load()
        assert isinstance(cfg, RuntimeConfig)
        assert cfg.app.timezone == "UTC"

    def test_load_fails_on_missing_file(self, config_dir: Path) -> None:
        (config_dir / "app.yaml").unlink()
        with pytest.raises(ConfigLoadError, match="Config file not found"):
            ConfigLoader(config_dir).load()

    def test_load_fails_on_invalid_field(self, config_dir: Path) -> None:
        (config_dir / "app.yaml").write_text(
            yaml.dump(
                {
                    "runtime_mode": "prod",
                    "data_root": "./data",
                    "timezone": "UTC",
                    "log_level": "INVALID_LEVEL",
                }
            )
        )
        with pytest.raises(ConfigLoadError, match="log_level"):
            ConfigLoader(config_dir).load()

    def test_load_collects_all_errors_before_raising(self, config_dir: Path) -> None:
        (config_dir / "app.yaml").unlink()
        (config_dir / "store.yaml").unlink()
        with pytest.raises(ConfigLoadError) as exc_info:
            ConfigLoader(config_dir).load()
        msg = str(exc_info.value)
        assert "app.yaml" in msg
        assert "store.yaml" in msg

    def test_telegram_enabled_validates_token(self, config_dir: Path) -> None:
        (config_dir / "channel.telegram.yaml").write_text(
            yaml.dump({"enabled": True, "bot_token": "", "allowlist": [99]})
        )
        with pytest.raises(ConfigLoadError, match="bot_token"):
            ConfigLoader(config_dir).load()


class TestEffectiveConfig:
    def test_returns_config_and_provenance_keys(self, config_dir: Path) -> None:
        result = ConfigLoader(config_dir).effective_config()
        assert "config" in result
        assert "provenance" in result

    def test_redacts_bot_token(self, config_dir: Path) -> None:
        (config_dir / "channel.telegram.yaml").write_text(
            yaml.dump({"enabled": False, "bot_token": "real-secret", "allowlist": []})
        )
        result = ConfigLoader(config_dir).effective_config()
        assert result["config"]["telegram"]["bot_token"] == "***REDACTED***"

    def test_provenance_file_for_yaml_fields(self, config_dir: Path) -> None:
        result = ConfigLoader(config_dir).effective_config()
        assert result["provenance"]["app.timezone"] == "file"

    def test_provenance_default_for_missing_fields(self, config_dir: Path) -> None:
        result = ConfigLoader(config_dir).effective_config()
        assert result["provenance"]["app.log_level"] == "default"

    def test_provenance_env_override(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ASSISTANT_APP_LOG_LEVEL", "DEBUG")
        result = ConfigLoader(config_dir).effective_config()
        assert result["config"]["app"]["log_level"] == "DEBUG"
        assert result["provenance"]["app.log_level"] == "env_override"

    def test_nested_env_override_applied(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ASSISTANT_SCHEDULER_RETRY_POLICY__MAX_ATTEMPTS", "7")
        result = ConfigLoader(config_dir).effective_config()
        assert result["config"]["scheduler"]["retry_policy"]["max_attempts"] == 7


class TestReloadDomain:
    def test_reload_returns_updated_value(self, config_dir: Path) -> None:
        loader = ConfigLoader(config_dir)
        (config_dir / "store.yaml").write_text(
            yaml.dump({"backend": "filesystem", "lock_ttl_seconds": 99, "atomic_write": True})
        )
        updated = loader.reload_domain("store")
        assert updated is not None
        assert updated.lock_ttl_seconds == 99

    def test_reload_unknown_domain_returns_none(self, config_dir: Path) -> None:
        assert ConfigLoader(config_dir).reload_domain("nonexistent") is None
