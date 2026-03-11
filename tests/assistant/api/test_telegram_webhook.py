"""
Tests for Telegram webhook API endpoint and lifecycle wiring.
"""

from unittest.mock import AsyncMock, patch

import pytest
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


def _runtime_config(allowlist: list[int], secret: str = "") -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(
            enabled=True,
            bot_token="12345:test-token",
            allowlist=allowlist,
            webhook_url="https://example.com/telegram/webhook",
            webhook_secret_token=secret,
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


@pytest.fixture()
def client() -> TestClient:
    config = _runtime_config(allowlist=[123456], secret="secret-1")
    with (
        patch("assistant.api.main.bootstrap", return_value=config),
        patch(
            "assistant.channels.telegram.adapter.TelegramAdapter.set_webhook",
            new_callable=AsyncMock,
        ),
        patch(
            "assistant.channels.telegram.adapter.TelegramAdapter.delete_webhook",
            new_callable=AsyncMock,
        ),
        patch("assistant.channels.telegram.adapter.TelegramAdapter.close", new_callable=AsyncMock),
    ):
        from assistant.api.main import app

        with TestClient(app, raise_server_exceptions=True) as test_client:
            yield test_client


def test_webhook_rejects_bad_secret(client: TestClient) -> None:
    response = client.post("/telegram/webhook", json={})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_webhook_rejects_unauthorized_user(client: TestClient) -> None:
    update = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "date": 1700000000,
            "chat": {"id": 999, "type": "private"},
            "from": {"id": 999, "is_bot": False, "first_name": "Bad"},
            "text": "hello",
        },
    }
    response = client.post(
        "/telegram/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-1"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_webhook_acknowledges_callback(client: TestClient) -> None:
    from assistant.api.main import app

    callback_update = {
        "update_id": 2,
        "callback_query": {
            "id": "cq-1",
            "from": {"id": 123456, "is_bot": False, "first_name": "Good"},
            "chat_instance": "instance-1",
            "data": "resume",
            "message": {"message_id": 11, "chat": {"id": 123456, "type": "private"}},
        },
    }
    with (
        patch.object(
            app.state.telegram_adapter, "acknowledge_callback", new_callable=AsyncMock
        ) as mock_ack,
        patch.object(app.state.telegram_adapter, "send_response", new_callable=AsyncMock),
    ):
        response = client.post(
            "/telegram/webhook",
            json=callback_update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret-1"},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    mock_ack.assert_called_once_with("cq-1")


def test_webhook_ignores_malformed_payload(client: TestClient) -> None:
    response = client.post(
        "/telegram/webhook",
        json={"update_id": "not-an-int"},
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-1"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_voice_webhook_uses_async_path_and_enriches_transcript(client: TestClient) -> None:
    """
    Regression: webhook must call process_update_async (not process_update)
    so that VoiceTranscriptionService enrichment runs for voice messages.
    Injects a mock transcription service into the live adapter and verifies
    the event reaching the handler carries transcript_text.
    """
    from assistant.api.main import app
    from assistant.channels.telegram.ingestion.interfaces import TranscriptionWorkerInterface
    from assistant.channels.telegram.ingestion.transcription import VoiceTranscriptionService

    class _MockWorker(TranscriptionWorkerInterface):
        async def transcribe(self, file_id: str, duration_seconds: int) -> str | None:
            return "webhook transcribed"

    svc = VoiceTranscriptionService(_MockWorker(), timeout_seconds=5)
    # Inject service into the live ingress component
    app.state.telegram_adapter._ingress._transcription_service = svc

    voice_update = {
        "update_id": 5,
        "message": {
            "message_id": 15,
            "date": 1700000000,
            "chat": {"id": 123456, "type": "private"},
            "from": {"id": 123456, "is_bot": False, "first_name": "Good"},
            "voice": {"file_id": "voice_wh", "file_unique_id": "unique_wh", "duration": 6},
        },
    }
    from assistant.channels.telegram.models import ChannelResponse as _CR
    from assistant.channels.telegram.models import NormalizedEvent as _NE

    captured_events: list[_NE] = []

    async def _capture_handler(event: _NE) -> _CR | None:
        captured_events.append(event)
        return None

    app.state.telegram_event_handler = _capture_handler

    response = client.post(
        "/telegram/webhook",
        json=voice_update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-1"},
    )
    assert response.status_code == 200
    assert len(captured_events) == 1
    event = captured_events[0]
    assert event.voice is not None
    assert event.voice.transcript_text == "webhook transcribed"
    assert event.text == "webhook transcribed"


def test_webhook_dispatches_to_handler_and_sends_response(client: TestClient) -> None:
    from assistant.api.main import app
    from assistant.channels.telegram.models import ChannelResponse, MessageType

    update = {
        "update_id": 4,
        "message": {
            "message_id": 14,
            "date": 1700000000,
            "chat": {"id": 123456, "type": "private"},
            "from": {"id": 123456, "is_bot": False, "first_name": "Good"},
            "text": "hello",
        },
    }
    response_obj = ChannelResponse(
        response_id="resp-1",
        channel="telegram",
        session_id="tg:123456",
        trace_id="trace-1",
        message_type=MessageType.TEXT,
        text="hi there",
    )
    handler = AsyncMock(return_value=response_obj)
    app.state.telegram_event_handler = handler

    with patch.object(
        app.state.telegram_adapter, "send_response", new_callable=AsyncMock
    ) as mock_send:
        response = client.post(
            "/telegram/webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret-1"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    handler.assert_called_once()
    mock_send.assert_called_once_with(response_obj, chat_id=123456)


def test_startup_calls_build_transcription_service_with_telegram_config() -> None:
    """
    Lifecycle test: verifies build_transcription_service is called with the
    Telegram config during app startup, without accessing any private attributes.
    """
    config = _runtime_config(allowlist=[123456], secret="secret-1")
    with (
        patch("assistant.api.main.bootstrap", return_value=config),
        patch(
            "assistant.api.main.build_transcription_service",
        ) as mock_factory,
        patch(
            "assistant.channels.telegram.adapter.TelegramAdapter.set_webhook",
            new_callable=AsyncMock,
        ),
        patch(
            "assistant.channels.telegram.adapter.TelegramAdapter.delete_webhook",
            new_callable=AsyncMock,
        ),
        patch("assistant.channels.telegram.adapter.TelegramAdapter.close", new_callable=AsyncMock),
    ):
        from assistant.api.main import app

        with TestClient(app, raise_server_exceptions=True):
            pass

    mock_factory.assert_called_once_with(config.telegram)
