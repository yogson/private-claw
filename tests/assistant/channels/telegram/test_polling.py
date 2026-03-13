"""
Tests for Telegram polling worker.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import Update

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.models import ChannelResponse, MessageType
from assistant.channels.telegram.polling import (
    _configure_bot_commands_menu,
    _process_update,
    run_polling,
)
from assistant.core.config.schemas import TelegramChannelConfig


def _make_config() -> TelegramChannelConfig:
    return TelegramChannelConfig(
        enabled=True,
        bot_token="12345:test-token",
        allowlist=[123456],
        poll_timeout_seconds=1,
        startup_drop_pending_updates=False,
    )


def _make_text_update(update_id: int = 1, user_id: int = 123456, text: str = "hi") -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": 1,
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "chat": {"id": user_id, "type": "private"},
                "date": 1700000000,
                "text": text,
            },
        }
    )


def _send_method() -> SendMessage:
    return SendMessage(chat_id=123, text="x")


@pytest.mark.asyncio
async def test_process_update_dispatches_to_handler_and_sends_response() -> None:
    """Verifies _process_update normalizes, calls handler, and sends response."""
    config = _make_config()
    adapter = TelegramAdapter(config)
    update = _make_text_update()

    response_obj = ChannelResponse(
        response_id="resp-1",
        channel="telegram",
        session_id="tg:123456",
        trace_id="trace-1",
        message_type=MessageType.TEXT,
        text="hi there",
    )
    handler = AsyncMock(return_value=response_obj)

    with patch.object(adapter, "send_response", new_callable=AsyncMock) as mock_send:
        await _process_update(adapter, update, handler)

    handler.assert_called_once()
    mock_send.assert_called_once_with(response_obj, chat_id=123456)


@pytest.mark.asyncio
async def test_process_update_ignores_none_event() -> None:
    """Verifies _process_update does not call handler or send when event is None."""
    config = TelegramChannelConfig(
        enabled=True,
        bot_token="12345:test-token",
        allowlist=[999],
    )
    adapter = TelegramAdapter(config)
    update = _make_text_update(user_id=123456)

    handler = AsyncMock(
        return_value=ChannelResponse(
            response_id="r",
            channel="telegram",
            session_id="tg:123456",
            trace_id="t",
            message_type=MessageType.TEXT,
            text="x",
        )
    )

    await _process_update(adapter, update, handler)

    handler.assert_not_called()


@pytest.mark.asyncio
async def test_process_update_ignores_none_response() -> None:
    """Verifies _process_update does not send when handler returns None."""
    config = _make_config()
    adapter = TelegramAdapter(config)
    update = _make_text_update()

    handler = AsyncMock(return_value=None)

    with patch.object(adapter, "send_response", new_callable=AsyncMock) as mock_send:
        await _process_update(adapter, update, handler)

    handler.assert_called_once()
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_run_polling_stops_on_stop_event() -> None:
    """Verifies run_polling exits when stop_event is set."""
    config = _make_config()
    adapter = TelegramAdapter(config)
    stop = asyncio.Event()
    stop.set()

    with patch("assistant.channels.telegram.polling.Bot") as mock_bot_cls:
        mock_bot = MagicMock()
        mock_bot.get_updates = AsyncMock(return_value=[])
        mock_bot.delete_webhook = AsyncMock()
        mock_bot.set_my_commands = AsyncMock()
        mock_bot.set_chat_menu_button = AsyncMock()
        mock_bot.session.close = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        await run_polling(
            adapter,
            config,
            AsyncMock(),
            stop_event=stop,
        )

    mock_bot.delete_webhook.assert_called_once()
    mock_bot.set_my_commands.assert_called_once()
    mock_bot.set_chat_menu_button.assert_called_once()


@pytest.mark.asyncio
async def test_configure_bot_commands_menu_registers_expected_commands() -> None:
    """Verifies native Telegram commands menu is configured on startup."""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    bot.set_chat_menu_button = AsyncMock()

    await _configure_bot_commands_menu(bot)

    bot.set_my_commands.assert_called_once()
    commands = bot.set_my_commands.call_args.kwargs["commands"]
    assert [f"/{item.command}" for item in commands] == [
        "/new",
        "/reset",
        "/sessions",
        "/usage",
    ]
    bot.set_chat_menu_button.assert_called_once()


@pytest.mark.asyncio
async def test_configure_bot_commands_menu_tolerates_set_my_commands_api_error() -> None:
    bot = MagicMock()
    bot.set_my_commands = AsyncMock(side_effect=TelegramAPIError(_send_method(), "Forbidden"))
    bot.set_chat_menu_button = AsyncMock()

    await _configure_bot_commands_menu(bot)

    bot.set_my_commands.assert_called_once()
    bot.set_chat_menu_button.assert_not_called()


@pytest.mark.asyncio
async def test_configure_bot_commands_menu_tolerates_set_chat_menu_button_network_error() -> None:
    bot = MagicMock()
    bot.set_my_commands = AsyncMock(return_value=True)
    bot.set_chat_menu_button = AsyncMock(
        side_effect=TelegramNetworkError(_send_method(), "connection refused")
    )

    await _configure_bot_commands_menu(bot)

    bot.set_my_commands.assert_called_once()
    bot.set_chat_menu_button.assert_called_once()
