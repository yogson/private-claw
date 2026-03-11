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
        patch("assistant.core.bootstrap.bootstrap", return_value=config),
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
    with patch.object(
        app.state.telegram_adapter, "acknowledge_callback", new_callable=AsyncMock
    ) as mock_ack, patch.object(
        app.state.telegram_adapter, "send_response", new_callable=AsyncMock
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
