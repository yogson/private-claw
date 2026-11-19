"""Tests for config domain Pydantic schemas."""

import pytest
from pydantic import ValidationError

from assistant.core.config.schemas import (
    ModelConfig,
    TelegramChannelConfig,
    ToolDefaultParams,
)


class TestTelegramChannelConfig:
    def test_disabled_allows_empty_credentials(self) -> None:
        cfg = TelegramChannelConfig(enabled=False, bot_token="", allowlist=[])
        assert cfg.enabled is False

    def test_defaults_are_disabled(self) -> None:
        cfg = TelegramChannelConfig()
        assert cfg.enabled is False
        assert cfg.bot_token == ""
        assert cfg.allowlist == []

    def test_enabled_requires_bot_token(self) -> None:
        with pytest.raises(ValidationError, match="bot_token must not be empty"):
            TelegramChannelConfig(enabled=True, bot_token="", allowlist=[123])

    def test_enabled_requires_allowlist(self) -> None:
        with pytest.raises(ValidationError, match="allowlist must contain at least one"):
            TelegramChannelConfig(enabled=True, bot_token="tok:abc", allowlist=[])

    def test_enabled_valid(self) -> None:
        cfg = TelegramChannelConfig(
            enabled=True,
            bot_token="tok:abc",
            allowlist=[42],
        )
        assert cfg.enabled is True
        assert cfg.allowlist == [42]

    def test_mtproto_both_absent_is_valid(self) -> None:
        cfg = TelegramChannelConfig(mtproto_api_id=None, mtproto_api_hash=None)
        assert cfg.mtproto_api_id is None
        assert cfg.mtproto_api_hash is None

    def test_mtproto_both_present_is_valid(self) -> None:
        cfg = TelegramChannelConfig(mtproto_api_id=12345, mtproto_api_hash="abc123hash")
        assert cfg.mtproto_api_id == 12345
        assert cfg.mtproto_api_hash == "abc123hash"

    def test_mtproto_only_api_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="mtproto_api_id and mtproto_api_hash"):
            TelegramChannelConfig(mtproto_api_id=12345, mtproto_api_hash=None)

    def test_mtproto_only_api_hash_rejected(self) -> None:
        with pytest.raises(ValidationError, match="mtproto_api_id and mtproto_api_hash"):
            TelegramChannelConfig(mtproto_api_id=None, mtproto_api_hash="abc123hash")


class TestModelConfig:
    def test_valid(self) -> None:
        cfg = ModelConfig(default_model_id="claude-sonnet", model_allowlist=["claude-sonnet"])
        assert cfg.default_model_id == "claude-sonnet"

    def test_empty_allowlist_rejected(self) -> None:
        with pytest.raises(ValidationError, match="model_allowlist must contain"):
            ModelConfig(default_model_id="claude-sonnet", model_allowlist=[])

    def test_default_not_in_allowlist_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be in model_allowlist"):
            ModelConfig(
                default_model_id="claude-opus",
                model_allowlist=["claude-sonnet"],
            )

    def test_default_in_allowlist_ok(self) -> None:
        cfg = ModelConfig(
            default_model_id="claude-sonnet",
            model_allowlist=["claude-sonnet", "claude-haiku"],
        )
        assert cfg.default_model_id == "claude-sonnet"


class TestDelegationPermissionMode:
    def test_unset_by_default(self) -> None:
        assert ToolDefaultParams().delegation_default_permission_mode is None

    @pytest.mark.parametrize("mode", ["auto", "acceptEdits", "dontAsk", "bypassPermissions"])
    def test_accepts_claude_code_modes(self, mode: str) -> None:
        params = ToolDefaultParams.model_validate({"delegation_default_permission_mode": mode})
        assert params.delegation_default_permission_mode == mode

    @pytest.mark.parametrize("mode", ["yolo", "", "AUTO", "BypassPermissions", "bypass"])
    def test_rejects_unknown_mode_at_startup(self, mode: str) -> None:
        with pytest.raises(ValidationError):
            ToolDefaultParams.model_validate({"delegation_default_permission_mode": mode})

    @pytest.mark.parametrize("alias", ["manual", "Manual", " manual "])
    def test_manual_alias_maps_to_default(self, alias: str) -> None:
        """Config accepts the same "manual" alias as per-request values."""
        params = ToolDefaultParams.model_validate({"delegation_default_permission_mode": alias})
        assert params.delegation_default_permission_mode == "default"

    def test_capability_override_accepts_manual_alias(self) -> None:
        from assistant.core.capabilities.schemas import CapabilityToolOverride

        override = CapabilityToolOverride.model_validate(
            {"delegation_default_permission_mode": "manual"}
        )
        assert override.delegation_default_permission_mode == "default"

    def test_yaml_null_means_unset(self) -> None:
        import yaml

        raw = yaml.safe_load("delegation_default_permission_mode: null\n")
        assert ToolDefaultParams.model_validate(raw).delegation_default_permission_mode is None
