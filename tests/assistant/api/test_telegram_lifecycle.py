"""
Tests for Telegram polling lifecycle wiring in FastAPI startup/shutdown.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from assistant.channels.telegram.models import NormalizedEvent
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


@pytest.mark.asyncio
async def test_handler_returns_orchestrator_output_not_echo() -> None:
    """
    Acceptance: a Telegram text event goes through orchestrator and returns
    model/greeting output, not echo of input. Prevents regression of hello->hello.
    """
    from assistant.api.main import _build_orchestrator_handler
    from assistant.core.events.models import EventSource, EventType

    event = NormalizedEvent(
        event_id="ev-1",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-1",
        text="hello",
        metadata={"chat_id": 123},
    )

    mock_adapter = MagicMock()
    mock_adapter.is_session_reset_request.return_value = False
    mock_adapter.is_session_reset_available.return_value = True
    mock_adapter.is_session_resume_request.return_value = False
    mock_adapter.is_session_resume_callback.return_value = False

    mock_orchestrator = MagicMock()
    mock_orchestrator.execute_turn = AsyncMock(return_value="model reply")

    handler = _build_orchestrator_handler(mock_adapter, mock_orchestrator)
    response = await handler(event)

    assert response is not None
    assert response.text == "model reply"
    assert response.text != event.text
    mock_orchestrator.execute_turn.assert_called_once()


@pytest.mark.asyncio
async def test_handler_handles_reset_without_orchestrator_call() -> None:
    from assistant.api.main import _build_orchestrator_handler
    from assistant.core.events.models import EventSource, EventType

    event = NormalizedEvent(
        event_id="ev-reset",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-reset",
        text="/reset",
        metadata={"chat_id": 123},
    )

    mock_adapter = MagicMock()
    mock_adapter.is_session_reset_request.return_value = True
    mock_adapter.is_session_reset_available.return_value = True
    mock_adapter.reset_session_context = AsyncMock(return_value=True)

    mock_orchestrator = MagicMock()
    mock_orchestrator.execute_turn = AsyncMock()

    handler = _build_orchestrator_handler(mock_adapter, mock_orchestrator)
    response = await handler(event)

    assert response is not None
    assert response.text == "Session context reset. Starting fresh."
    mock_adapter.reset_session_context.assert_awaited_once_with(event)
    mock_orchestrator.execute_turn.assert_not_called()


@pytest.mark.asyncio
async def test_handler_handles_reset_unavailable_without_orchestrator_call() -> None:
    from assistant.api.main import _build_orchestrator_handler
    from assistant.core.events.models import EventSource, EventType

    event = NormalizedEvent(
        event_id="ev-reset-unavailable",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-reset-unavailable",
        text="/reset",
        metadata={"chat_id": 123},
    )

    mock_adapter = MagicMock()
    mock_adapter.is_session_reset_request.return_value = True
    mock_adapter.is_session_reset_available.return_value = False
    mock_adapter.reset_session_context = AsyncMock()

    mock_orchestrator = MagicMock()
    mock_orchestrator.execute_turn = AsyncMock()

    handler = _build_orchestrator_handler(mock_adapter, mock_orchestrator)
    response = await handler(event)

    assert response is not None
    assert response.text == "Session reset is not available."
    mock_adapter.reset_session_context.assert_not_called()
    mock_orchestrator.execute_turn.assert_not_called()
