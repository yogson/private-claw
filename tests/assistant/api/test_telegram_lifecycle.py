"""
Tests for Telegram polling lifecycle wiring in FastAPI startup/shutdown.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from assistant.channels.telegram.models import ChannelResponse, MessageType, NormalizedEvent
from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesPolicyConfig,
    McpServersConfig,
    MemoryConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
    ToolsConfig,
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
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=[], denied_capabilities=[]),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


def test_startup_calls_build_transcription_service_with_telegram_config() -> None:
    """
    Lifecycle test: verifies build_transcription_service is called with the
    Telegram config during app startup when telegram is enabled.
    """
    config = _runtime_config(allowlist=[123456])
    mock_mem0_client = MagicMock()
    with (
        patch("assistant.api.main.bootstrap", return_value=config),
        patch("assistant.api.main.build_transcription_service") as mock_factory,
        patch("assistant.api.main.run_polling", new_callable=AsyncMock) as mock_polling,
        patch("assistant.channels.telegram.adapter.TelegramAdapter.close", new_callable=AsyncMock),
        patch("assistant.memory.mem0.write.MemoryClient", mock_mem0_client),
        patch("assistant.memory.mem0.retrieval.MemoryClient", mock_mem0_client),
    ):
        from assistant.api.main import app

        with TestClient(app, raise_server_exceptions=True):
            pass

    mock_factory.assert_called_once_with(config.telegram)
    mock_polling.assert_called_once()


def test_startup_starts_polling_when_telegram_enabled() -> None:
    """Verifies run_polling is started as a background task when telegram is enabled."""
    config = _runtime_config(allowlist=[123456])
    mock_mem0_client = MagicMock()
    with (
        patch("assistant.api.main.bootstrap", return_value=config),
        patch("assistant.api.main.run_polling", new_callable=AsyncMock) as mock_polling,
        patch("assistant.channels.telegram.adapter.TelegramAdapter.close", new_callable=AsyncMock),
        patch("assistant.memory.mem0.write.MemoryClient", mock_mem0_client),
        patch("assistant.memory.mem0.retrieval.MemoryClient", mock_mem0_client),
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
    mock_adapter.is_session_new_request.return_value = False
    mock_adapter.is_session_reset_request.return_value = False
    mock_adapter.is_session_reset_available.return_value = True
    mock_adapter.is_session_resume_request.return_value = False
    mock_adapter.is_session_resume_callback.return_value = False
    mock_adapter.is_usage_request.return_value = False
    mock_adapter.is_memory_confirmation_callback.return_value = False

    mock_orchestrator = MagicMock()
    from assistant.core.orchestrator.models import OrchestratorResult

    mock_orchestrator.execute_turn = AsyncMock(return_value=OrchestratorResult(text="model reply"))

    handler = _build_orchestrator_handler(mock_adapter, mock_orchestrator, None)
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
    mock_adapter.is_session_new_request.return_value = False
    mock_adapter.is_session_reset_request.return_value = True
    mock_adapter.is_session_reset_available.return_value = True
    mock_adapter.is_usage_request.return_value = False
    mock_adapter.is_memory_confirmation_callback.return_value = False
    mock_adapter.reset_session_context = AsyncMock(return_value=True)

    mock_orchestrator = MagicMock()
    mock_orchestrator.execute_turn = AsyncMock()

    handler = _build_orchestrator_handler(mock_adapter, mock_orchestrator, None)
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
    mock_adapter.is_session_new_request.return_value = False
    mock_adapter.is_session_reset_request.return_value = True
    mock_adapter.is_session_reset_available.return_value = False
    mock_adapter.is_usage_request.return_value = False
    mock_adapter.is_memory_confirmation_callback.return_value = False
    mock_adapter.reset_session_context = AsyncMock()

    mock_orchestrator = MagicMock()
    mock_orchestrator.execute_turn = AsyncMock()

    handler = _build_orchestrator_handler(mock_adapter, mock_orchestrator, None)
    response = await handler(event)

    assert response is not None
    assert response.text == "Session reset is not available."
    mock_adapter.reset_session_context.assert_not_called()
    mock_orchestrator.execute_turn.assert_not_called()


@pytest.mark.asyncio
async def test_handler_handles_new_session_without_orchestrator_call() -> None:
    from assistant.api.main import _build_orchestrator_handler
    from assistant.core.events.models import EventSource, EventType

    event = NormalizedEvent(
        event_id="ev-new",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-new",
        text="/new",
        metadata={"chat_id": 123},
    )

    mock_adapter = MagicMock()
    mock_adapter.is_session_new_request.return_value = True
    mock_adapter.is_usage_request.return_value = False
    mock_adapter.is_memory_confirmation_callback.return_value = False
    mock_adapter.start_new_session.return_value = "tg:123:abcd1234ef56"

    mock_orchestrator = MagicMock()
    mock_orchestrator.execute_turn = AsyncMock()

    handler = _build_orchestrator_handler(mock_adapter, mock_orchestrator, None)
    response = await handler(event)

    assert response is not None
    assert response.session_id == "tg:123:abcd1234ef56"
    assert response.text == "Started a new session. Continue your conversation."
    mock_adapter.start_new_session.assert_called_once_with(event)
    mock_orchestrator.execute_turn.assert_not_called()


@pytest.mark.asyncio
async def test_handler_returns_interactive_reply_keyboard_when_pending_ask() -> None:
    """
    Acceptance: when orchestrator returns pending_ask, handler returns
    interactive response with ui_kind=reply_keyboard and combines
    response_text with question.
    """
    from assistant.api.main import _build_orchestrator_handler
    from assistant.core.events.models import EventSource, EventType
    from assistant.core.orchestrator.models import OrchestratorResult, PendingAskData

    event = NormalizedEvent(
        event_id="ev-ask",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-ask",
        text="Which one?",
        metadata={"chat_id": 123},
    )

    mock_adapter = MagicMock()
    mock_adapter.is_session_new_request.return_value = False
    mock_adapter.is_session_reset_request.return_value = False
    mock_adapter.is_session_resume_request.return_value = False
    mock_adapter.is_session_resume_callback.return_value = False
    mock_adapter.is_usage_request.return_value = False
    mock_adapter.is_memory_confirmation_callback.return_value = False

    pending_ask = PendingAskData(
        question_id="tc-1",
        question="Which option do you prefer?",
        options=[{"id": "0", "label": "A"}, {"id": "1", "label": "B"}],
        session_id="tg:123",
        turn_id="turn-1",
        tool_call_id="tc-1",
    )
    mock_orchestrator = MagicMock()
    mock_orchestrator.execute_turn = AsyncMock(
        return_value=OrchestratorResult(
            text="Sure!",
            pending_ask=pending_ask,
        )
    )

    mock_response = ChannelResponse(
        response_id="resp-1",
        channel="telegram",
        session_id="tg:123",
        trace_id="trace-ask",
        message_type=MessageType.INTERACTIVE,
        text="Sure!\n\nWhich option do you prefer?",
        ui_kind="reply_keyboard",
        actions=[],
    )
    mock_adapter.build_ask_question_response = MagicMock(return_value=mock_response)

    handler = _build_orchestrator_handler(mock_adapter, mock_orchestrator, None)
    response = await handler(event)

    assert response is not None
    assert response.message_type == MessageType.INTERACTIVE
    assert response.ui_kind == "reply_keyboard"
    mock_adapter.build_ask_question_response.assert_called_once()
    call_kwargs = mock_adapter.build_ask_question_response.call_args[1]
    assert call_kwargs["question"] == "Sure!\n\nWhich option do you prefer?"
    assert call_kwargs["options"] == [{"id": "0", "label": "A"}, {"id": "1", "label": "B"}]


@pytest.mark.asyncio
async def test_handler_handles_new_session_failure_without_orchestrator_call() -> None:
    from assistant.api.main import _build_orchestrator_handler
    from assistant.core.events.models import EventSource, EventType

    event = NormalizedEvent(
        event_id="ev-new-fail",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-new-fail",
        text="/new",
        metadata={"chat_id": 123},
    )

    mock_adapter = MagicMock()
    mock_adapter.is_session_new_request.return_value = True
    mock_adapter.is_usage_request.return_value = False
    mock_adapter.is_memory_confirmation_callback.return_value = False
    mock_adapter.start_new_session.return_value = None

    mock_orchestrator = MagicMock()
    mock_orchestrator.execute_turn = AsyncMock()

    handler = _build_orchestrator_handler(mock_adapter, mock_orchestrator, None)
    response = await handler(event)

    assert response is not None
    assert response.session_id == "tg:123"
    assert response.text == "Could not start a new session for this chat."
    mock_adapter.start_new_session.assert_called_once_with(event)
    mock_orchestrator.execute_turn.assert_not_called()


@pytest.mark.asyncio
async def test_handler_handles_usage_without_orchestrator_call() -> None:
    """Verifies /usage bypasses orchestrator and returns usage stats when service is provided."""
    from assistant.api.main import _build_orchestrator_handler
    from assistant.core.events.models import EventSource, EventType

    event = NormalizedEvent(
        event_id="ev-usage",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-usage",
        text="/usage",
        metadata={"chat_id": 123},
    )

    mock_adapter = MagicMock()
    mock_adapter.is_session_new_request.return_value = False
    mock_adapter.is_session_reset_request.return_value = False
    mock_adapter.is_session_resume_request.return_value = False
    mock_adapter.is_session_resume_callback.return_value = False
    mock_adapter.is_usage_request.return_value = True
    mock_adapter.is_memory_confirmation_callback.return_value = False

    mock_usage_service = MagicMock()
    mock_usage_service.build_usage_response = AsyncMock(
        return_value=ChannelResponse(
            response_id="resp-usage",
            channel="telegram",
            session_id="tg:123",
            trace_id="trace-usage",
            message_type=MessageType.TEXT,
            text="*Usage statistics*\n\n*Current session*\n  Tokens: 0 in / 0 out",
        )
    )

    mock_orchestrator = MagicMock()
    mock_orchestrator.execute_turn = AsyncMock()

    handler = _build_orchestrator_handler(
        mock_adapter, mock_orchestrator, None, usage_service=mock_usage_service
    )
    response = await handler(event)

    assert response is not None
    assert "Usage statistics" in response.text
    mock_usage_service.build_usage_response.assert_awaited_once_with(event)
    mock_orchestrator.execute_turn.assert_not_called()


@pytest.mark.asyncio
async def test_handler_handles_usage_unavailable_when_no_service() -> None:
    """Verifies /usage returns placeholder when usage service is not configured."""
    from assistant.api.main import _build_orchestrator_handler
    from assistant.core.events.models import EventSource, EventType

    event = NormalizedEvent(
        event_id="ev-usage-unavail",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-usage-unavail",
        text="/usage",
        metadata={"chat_id": 123},
    )

    mock_adapter = MagicMock()
    mock_adapter.is_session_new_request.return_value = False
    mock_adapter.is_session_reset_request.return_value = False
    mock_adapter.is_session_resume_request.return_value = False
    mock_adapter.is_session_resume_callback.return_value = False
    mock_adapter.is_usage_request.return_value = True
    mock_adapter.is_memory_confirmation_callback.return_value = False

    mock_orchestrator = MagicMock()
    mock_orchestrator.execute_turn = AsyncMock()

    handler = _build_orchestrator_handler(mock_adapter, mock_orchestrator, None)
    response = await handler(event)

    assert response is not None
    assert response.text == "Usage stats not available."
    mock_orchestrator.execute_turn.assert_not_called()
