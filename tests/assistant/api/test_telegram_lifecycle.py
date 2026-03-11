"""
Tests for Telegram polling lifecycle wiring in FastAPI startup/shutdown.
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

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


def _runtime_config(allowlist: list[int]) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(
            enabled=True,
            bot_token="12345:test-token",
            allowlist=allowlist,
        ),
        model=ModelConfig(
            default_model_id="claude-3-5-sonnet-20241022",
            model_allowlist=["claude-3-5-sonnet-20241022"],
        ),
        capabilities=CapabilitiesConfig(allowed_capabilities=[]),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
    )


def test_startup_calls_build_transcription_service_with_telegram_config() -> None:
    """
    Lifecycle test: verifies build_transcription_service is called with the
    Telegram config during app startup when telegram is enabled.
    """
    config = _runtime_config(allowlist=[123456])
    with (
        patch("assistant.api.main.bootstrap", return_value=config),
        patch("assistant.api.main.build_transcription_service") as mock_factory,
        patch("assistant.api.main.run_polling", new_callable=AsyncMock) as mock_polling,
        patch("assistant.channels.telegram.adapter.TelegramAdapter.close", new_callable=AsyncMock),
    ):
        from assistant.api.main import app

        with TestClient(app, raise_server_exceptions=True):
            pass

    mock_factory.assert_called_once_with(config.telegram)
    mock_polling.assert_called_once()


def test_startup_starts_polling_when_telegram_enabled() -> None:
    """Verifies run_polling is started as a background task when telegram is enabled."""
    config = _runtime_config(allowlist=[123456])
    with (
        patch("assistant.api.main.bootstrap", return_value=config),
        patch("assistant.api.main.run_polling", new_callable=AsyncMock) as mock_polling,
        patch("assistant.channels.telegram.adapter.TelegramAdapter.close", new_callable=AsyncMock),
    ):
        from assistant.api.main import app

        with TestClient(app, raise_server_exceptions=True):
            pass

    mock_polling.assert_called_once()
    call_kwargs = mock_polling.call_args[1]
    assert "stop_event" in call_kwargs
    assert call_kwargs["stop_event"] is not None


def test_startup_skips_telegram_when_disabled() -> None:
    """Verifies no polling or adapter when telegram is disabled."""
    config = _runtime_config(allowlist=[123456])
    config = config.model_copy(
        update={"telegram": config.telegram.model_copy(update={"enabled": False})}
    )
    with (
        patch("assistant.api.main.bootstrap", return_value=config),
        patch("assistant.api.main.run_polling", new_callable=AsyncMock) as mock_polling,
    ):
        from assistant.api.main import app

        with TestClient(app, raise_server_exceptions=True):
            pass

    mock_polling.assert_not_called()
