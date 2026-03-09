"""Tests for environment variable override utilities."""

import pytest

from assistant.core.config.env_utils import apply_env_overrides, parse_env_value


class TestParseEnvValue:
    def test_plain_string(self) -> None:
        assert parse_env_value("hello") == "hello"

    def test_integer(self) -> None:
        assert parse_env_value("42") == 42

    def test_bool_true(self) -> None:
        assert parse_env_value("true") is True

    def test_bool_false(self) -> None:
        assert parse_env_value("false") is False

    def test_json_list(self) -> None:
        assert parse_env_value("[1, 2, 3]") == [1, 2, 3]

    def test_json_dict(self) -> None:
        assert parse_env_value('{"a": 1}') == {"a": 1}

    def test_invalid_json_returns_string(self) -> None:
        assert parse_env_value("not json {") == "not json {"


class TestApplyEnvOverrides:
    def test_flat_key_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASSISTANT_APP_LOG_LEVEL", "DEBUG")
        result, overridden = apply_env_overrides({"log_level": "INFO"}, "ASSISTANT_APP")
        assert result["log_level"] == "DEBUG"
        assert "log_level" in overridden

    def test_flat_key_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASSISTANT_STORE_LOCK_TTL_SECONDS", "60")
        result, _ = apply_env_overrides({}, "ASSISTANT_STORE")
        assert result["lock_ttl_seconds"] == 60

    def test_nested_key_double_underscore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASSISTANT_SCHEDULER_RETRY_POLICY__MAX_ATTEMPTS", "5")
        base = {"retry_policy": {"max_attempts": 3, "backoff_seconds": 60}}
        result, overridden = apply_env_overrides(base, "ASSISTANT_SCHEDULER")
        assert result["retry_policy"]["max_attempts"] == 5
        assert result["retry_policy"]["backoff_seconds"] == 60
        assert "retry_policy" in overridden

    def test_list_field_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASSISTANT_MODEL_MODEL_ALLOWLIST", '["claude-sonnet", "claude-haiku"]')
        result, _ = apply_env_overrides({}, "ASSISTANT_MODEL")
        assert result["model_allowlist"] == ["claude-sonnet", "claude-haiku"]

    def test_unrelated_prefix_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTHER_APP_LOG_LEVEL", "DEBUG")
        result, overridden = apply_env_overrides({"log_level": "INFO"}, "ASSISTANT_APP")
        assert result["log_level"] == "INFO"
        assert not overridden

    def test_original_data_not_mutated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASSISTANT_APP_LOG_LEVEL", "DEBUG")
        original = {"log_level": "INFO"}
        apply_env_overrides(original, "ASSISTANT_APP")
        assert original["log_level"] == "INFO"
