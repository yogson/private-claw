"""
Tests for Telegram polling worker.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramConflictError, TelegramNetworkError
from aiogram.methods import GetUpdates, SendMessage
from aiogram.types import Update
from structlog.testing import capture_logs

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.models import (
    CallbackQueryMeta,
    ChannelResponse,
    EventSource,
    EventType,
    MessageType,
    NormalizedEvent,
)
from assistant.channels.telegram.polling import (
    _configure_bot_commands_menu,
    _dispatch_event,
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
        "/compact",
        "/wtf",
        "/sessions",
        "/model",
        "/capabilities",
        "/usage",
        "/stop",
        "/verbose",
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


def _callback_event(data: str) -> NormalizedEvent:
    return NormalizedEvent(
        event_id="ev-cb",
        event_type=EventType.USER_CALLBACK_QUERY,
        source=EventSource.TELEGRAM,
        session_id="tg:123456",
        user_id="123456",
        created_at=datetime.now(UTC),
        trace_id="trace-cb",
        callback_query=CallbackQueryMeta(callback_id="cq-1", callback_data=data),
        metadata={"chat_id": 123456},
    )


@pytest.mark.asyncio
async def test_dispatch_acks_ordinary_callback_and_survives_ack_failure() -> None:
    adapter = TelegramAdapter(_make_config())
    adapter.acknowledge_callback = AsyncMock(side_effect=RuntimeError("ack failed"))  # type: ignore[method-assign]
    handler = AsyncMock(return_value=None)

    await _dispatch_event(adapter, _callback_event("tk:task:status:1:sig"), handler, None)

    adapter.acknowledge_callback.assert_awaited_once_with("cq-1", None)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_leaves_confirmation_callback_ack_to_handler() -> None:
    adapter = TelegramAdapter(_make_config())
    adapter.acknowledge_callback = AsyncMock()  # type: ignore[method-assign]
    handler = AsyncMock(return_value=None)

    await _dispatch_event(adapter, _callback_event("cf:abc:a:sig"), handler, None)

    adapter.acknowledge_callback.assert_not_awaited()
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_polling_logs_conflict_and_keeps_backing_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 (another process polling the same token) is logged as an error and retried."""
    monkeypatch.setattr("assistant.channels.telegram.polling._BACKOFF_MIN_SECONDS", 0.01)
    config = _make_config()
    adapter = TelegramAdapter(config)
    stop = asyncio.Event()
    conflict = TelegramConflictError(
        GetUpdates(), "Conflict: terminated by other getUpdates request"
    )
    calls = 0

    async def _get_updates(**_: object) -> list[Update]:
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise conflict
        stop.set()
        return []

    with patch("assistant.channels.telegram.polling.Bot") as mock_bot_cls:
        mock_bot = MagicMock()
        mock_bot.get_updates = AsyncMock(side_effect=_get_updates)
        mock_bot.delete_webhook = AsyncMock()
        mock_bot.set_my_commands = AsyncMock()
        mock_bot.set_chat_menu_button = AsyncMock()
        mock_bot.session.close = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        with capture_logs() as logs:
            await run_polling(adapter, config, AsyncMock(), stop_event=stop)

    assert calls == 4
    conflicts = [e for e in logs if e["event"] == "telegram.polling.conflict"]
    assert len(conflicts) == 3
    assert all(e["log_level"] == "error" for e in conflicts)
    assert "another process is polling this bot token" in conflicts[0]["hint"]
    backoffs = [e["backoff_seconds"] for e in conflicts]
    assert backoffs == sorted(backoffs) and backoffs[0] < backoffs[-1]
    assert not [e for e in logs if e["event"] == "telegram.polling.get_updates_error"]


@pytest.mark.asyncio
async def test_run_polling_tags_media_group_with_update_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An album fed as untagged Updates reaches the handler as one event with all update ids."""
    monkeypatch.setattr(
        "assistant.channels.telegram.ingestion.media_group._DEFAULT_FLUSH_DELAY_SECONDS", 0.05
    )
    config = _make_config()
    adapter = TelegramAdapter(config)
    stop = asyncio.Event()
    album = [
        Update.model_validate(
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "from": {"id": 123456, "is_bot": False, "first_name": "Test"},
                    "chat": {"id": 123456, "type": "private"},
                    "date": 1700000000,
                    "media_group_id": "album1",
                    "photo": [
                        {
                            "file_id": f"f{update_id}",
                            "file_unique_id": f"u{update_id}",
                            "width": 1,
                            "height": 1,
                        }
                    ],
                },
            }
        )
        for update_id in (800, 801, 802)
    ]
    sent = False

    async def _get_updates(**_: object) -> list[Update]:
        nonlocal sent
        if sent:
            await asyncio.sleep(0.01)
            return []
        sent = True
        return album

    events: list[NormalizedEvent] = []

    async def _handler(event: NormalizedEvent) -> ChannelResponse | None:
        events.append(event)
        stop.set()
        return None

    with patch("assistant.channels.telegram.polling.Bot") as mock_bot_cls:
        mock_bot = MagicMock()
        mock_bot.get_updates = AsyncMock(side_effect=_get_updates)
        mock_bot.delete_webhook = AsyncMock()
        mock_bot.set_my_commands = AsyncMock()
        mock_bot.set_chat_menu_button = AsyncMock()
        mock_bot.session.close = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        await asyncio.wait_for(run_polling(adapter, config, _handler, stop_event=stop), 5)

    assert len(events) == 1
    assert events[0].metadata["update_id"] == 800
    assert events[0].metadata["update_ids"] == [800, 801, 802]


@pytest.mark.asyncio
async def test_process_update_logs_update_id_for_message_and_callback() -> None:
    """The ingress audit log event and event metadata carry the Telegram update_id."""
    adapter = TelegramAdapter(_make_config())
    callback = Update.model_validate(
        {
            "update_id": 502,
            "callback_query": {
                "id": "cq1",
                "from": {"id": 123456, "is_bot": False, "first_name": "Test"},
                "chat_instance": "ci",
                "data": "x:y",
                "message": {
                    "message_id": 7,
                    "date": 1700000000,
                    "chat": {"id": 123456, "type": "private"},
                },
            },
        }
    )
    seen: list[NormalizedEvent] = []

    async def handler(event: NormalizedEvent) -> None:
        seen.append(event)
        return None

    with capture_logs() as logs:
        await _process_update(adapter, _make_text_update(update_id=501), handler)
        await _process_update(adapter, callback, handler)

    audit = [e for e in logs if e["event"] == "telegram.audit.ingress.authorized"]
    assert [e["update_id"] for e in audit] == [501, 502]
    assert [e.metadata["update_id"] for e in seen] == [501, 502]
