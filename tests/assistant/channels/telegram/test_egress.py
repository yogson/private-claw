"""
Unit tests for TelegramEgress.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.methods import SendMessage

from assistant.channels.telegram.egress import TelegramEgress, TelegramSendError
from assistant.channels.telegram.models import ActionButton, ChannelResponse, MessageType


def _make_text_response() -> ChannelResponse:
    return ChannelResponse(
        response_id=str(uuid.uuid4()),
        channel="telegram",
        session_id="tg:123",
        trace_id="trace-1",
        message_type=MessageType.TEXT,
        text="Hello!",
    )


def _make_interactive_response() -> ChannelResponse:
    return ChannelResponse(
        response_id=str(uuid.uuid4()),
        channel="telegram",
        session_id="tg:123",
        trace_id="trace-2",
        message_type=MessageType.INTERACTIVE,
        text="Pick one:",
        actions=[
            ActionButton(label="Option A", callback_id="opt_a", callback_data="a"),
            ActionButton(label="Option B", callback_id="opt_b", callback_data="b"),
        ],
    )


def _send_method() -> SendMessage:
    return SendMessage(chat_id=123, text="x")


class TestTelegramEgresSend:
    @pytest.mark.asyncio
    async def test_text_send_success(self) -> None:
        egress = TelegramEgress(bot_token="12345:test-token", max_attempts=1)
        with patch.object(egress._bot, "send_message", new_callable=AsyncMock) as mock_send:
            result = await egress.send(_make_text_response(), chat_id=123)
        assert result is True
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_interactive_send_success(self) -> None:
        egress = TelegramEgress(bot_token="12345:test-token", max_attempts=1)
        with patch.object(egress._bot, "send_message", new_callable=AsyncMock) as mock_send:
            result = await egress.send(_make_interactive_response(), chat_id=123)
        assert result is True
        kwargs = mock_send.call_args.kwargs
        assert kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_retryable_network_error_exhausts_attempts(self) -> None:
        egress = TelegramEgress(bot_token="12345:test-token", max_attempts=2, base_delay=0.0)
        with (
            patch.object(egress._bot, "send_message", new_callable=AsyncMock) as mock_send,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_send.side_effect = TelegramNetworkError(_send_method(), "connection refused")
            with pytest.raises(TelegramSendError) as exc_info:
                await egress.send(_make_text_response(), chat_id=123)
        assert exc_info.value.attempts == 2
        assert mock_send.call_count == 2

    @pytest.mark.asyncio
    async def test_non_retryable_api_error_stops_immediately(self) -> None:
        egress = TelegramEgress(bot_token="12345:test-token", max_attempts=3, base_delay=0.0)
        with patch.object(egress._bot, "send_message", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = TelegramAPIError(_send_method(), "Forbidden")
            with pytest.raises(TelegramSendError) as exc_info:
                await egress.send(_make_text_response(), chat_id=123)
        assert exc_info.value.attempts == 1
        assert mock_send.call_count == 1

    @pytest.mark.asyncio
    async def test_acknowledge_callback_delegates(self) -> None:
        egress = TelegramEgress(bot_token="12345:test-token")
        with patch.object(
            egress._bot, "answer_callback_query", new_callable=AsyncMock
        ) as mock_answer:
            await egress.acknowledge_callback("cq-123")
        mock_answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_delegates_to_bot_session(self) -> None:
        egress = TelegramEgress(bot_token="12345:test-token", max_attempts=2, base_delay=0.0)
        with patch.object(egress._bot.session, "close", new_callable=AsyncMock) as mock_close:
            await egress.close()
        mock_close.assert_called_once()

    def test_inline_keyboard_payload_structure(self) -> None:
        egress = TelegramEgress(bot_token="12345:tok")
        response = _make_interactive_response()
        markup = egress._build_inline_keyboard(response)
        assert markup is not None
        keyboard = markup.inline_keyboard
        assert len(keyboard) == 2
        assert keyboard[0][0].text == "Option A"
        assert keyboard[1][0].callback_data == "b"

    def test_text_payload_no_reply_markup(self) -> None:
        egress = TelegramEgress(bot_token="12345:tok")
        markup = egress._build_inline_keyboard(_make_text_response())
        assert markup is None
