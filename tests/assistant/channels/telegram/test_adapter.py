"""
Unit tests for TelegramAdapter.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import Chat, Message, Update, User

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.allowlist import UnauthorizedUserError
from assistant.channels.telegram.models import ChannelResponse, EventType, MessageType
from assistant.core.config.schemas import TelegramChannelConfig


def _make_config(allowlist: list[int] | None = None) -> TelegramChannelConfig:
    return TelegramChannelConfig(
        enabled=True,
        bot_token="12345:test-token",
        allowlist=allowlist or [123456],
        webhook_url="https://example.com/telegram/webhook",
    )


def _text_update(user_id: int = 123456, text: str = "hi") -> dict:
    return {
        "message": {
            "message_id": 1,
            "from": {"id": user_id},
            "chat": {"id": user_id},
            "date": 1700000000,
            "text": text,
        }
    }


def _make_response() -> ChannelResponse:
    return ChannelResponse(
        response_id=str(uuid.uuid4()),
        channel="telegram",
        session_id="tg:123456",
        trace_id="trace-x",
        message_type=MessageType.TEXT,
        text="Reply!",
    )


class TestTelegramAdapterIngress:
    def test_process_update_allowed_user(self) -> None:
        adapter = TelegramAdapter(_make_config())
        event = adapter.process_update(_text_update())
        assert event is not None
        assert event.event_type == EventType.USER_TEXT_MESSAGE

    def test_process_update_unauthorized_raises(self) -> None:
        adapter = TelegramAdapter(_make_config(allowlist=[111]))
        with pytest.raises(UnauthorizedUserError):
            adapter.process_update(_text_update(user_id=999))

    def test_process_update_unsupported_returns_none(self) -> None:
        adapter = TelegramAdapter(_make_config())
        assert adapter.process_update({"some_other": {}}) is None

    def test_process_update_unexpected_exception_returns_none(self) -> None:
        adapter = TelegramAdapter(_make_config())
        with patch.object(adapter._ingress, "normalize", side_effect=RuntimeError("boom")):
            result = adapter.process_update(_text_update())
        assert result is None

    def test_process_update_accepts_aiogram_update(self) -> None:
        adapter = TelegramAdapter(_make_config())
        update = Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=1700000000,
                chat=Chat(id=123456, type="private"),
                from_user=User(id=123456, is_bot=False, first_name="User"),
                text="hi",
            ),
        )
        event = adapter.process_update(update)
        assert event is not None
        assert event.event_type == EventType.USER_TEXT_MESSAGE


class TestTelegramAdapterEgress:
    @pytest.mark.asyncio
    async def test_send_response_delegates_to_egress(self) -> None:
        adapter = TelegramAdapter(_make_config())
        with patch.object(adapter._egress, "send", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            result = await adapter.send_response(_make_response(), chat_id=123456)
        assert result is True
        mock_send.assert_called_once()
