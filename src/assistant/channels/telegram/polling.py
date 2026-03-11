"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Long-polling worker for Telegram updates. Runs as a background task when
Telegram is enabled. Reuses TelegramAdapter for normalization and egress.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import Update

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.allowlist import UnauthorizedUserError
from assistant.channels.telegram.models import ChannelResponse, NormalizedEvent
from assistant.core.config.schemas import TelegramChannelConfig

logger = structlog.get_logger(__name__)

TelegramEventHandler = Callable[[NormalizedEvent], Awaitable[ChannelResponse | None]]

_BACKOFF_MIN_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0
_BACKOFF_FACTOR = 1.5


async def run_polling(
    adapter: TelegramAdapter,
    config: TelegramChannelConfig,
    event_handler: TelegramEventHandler,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """
    Long-poll Telegram for updates and dispatch them through the adapter.

    Runs until cancelled or stop_event is set. On startup, deletes any existing
    webhook so polling can receive updates. Handles transient errors with
    exponential backoff.
    """
    bot = Bot(token=config.bot_token)
    # One-time webhook disable: required for polling to receive updates.
    # (Webhook endpoint and registration are removed; this is bootstrap-only.)
    try:
        await bot.delete_webhook(drop_pending_updates=config.startup_drop_pending_updates)
        logger.info("telegram.polling.started", drop_pending=config.startup_drop_pending_updates)
    except (TelegramAPIError, TelegramNetworkError) as exc:
        logger.warning("telegram.polling.delete_webhook_failed", error=str(exc))
        # Continue anyway; polling may still work if no webhook was set

    offset: int | None = None
    backoff = _BACKOFF_MIN_SECONDS
    stop = stop_event or asyncio.Event()

    try:
        while not stop.is_set():
            try:
                updates = await bot.get_updates(
                    offset=offset,
                    limit=100,
                    timeout=config.poll_timeout_seconds,
                )
            except asyncio.CancelledError:
                break
            except (TelegramAPIError, TelegramNetworkError) as exc:
                logger.warning(
                    "telegram.polling.get_updates_error",
                    error=str(exc),
                    backoff_seconds=backoff,
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=backoff)
                backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX_SECONDS)
                continue

            backoff = _BACKOFF_MIN_SECONDS

            for update in updates:
                if stop.is_set():
                    break
                if update.update_id is None:
                    logger.warning("telegram.polling.missing_update_id", update=update)
                    continue
                await _process_update(adapter, update, event_handler)
                offset = update.update_id + 1

            if config.poll_interval_seconds > 0 and updates:
                await asyncio.sleep(config.poll_interval_seconds)
    except Exception:
        logger.exception("telegram.polling.unexpected_error")
        raise

    await bot.session.close()
    logger.info("telegram.polling.stopped")


async def _process_update(
    adapter: TelegramAdapter,
    update: Update,
    event_handler: TelegramEventHandler,
) -> None:
    """Process a single update: normalize, dispatch, send response."""
    try:
        event = await adapter.process_update_async(update)
    except UnauthorizedUserError as exc:
        logger.warning("telegram.polling.unauthorized", user_id=exc.user_id)
        return
    except Exception:
        logger.exception("telegram.polling.process_error")
        return

    if event is None:
        return

    if event.callback_query is not None:
        await adapter.acknowledge_callback(event.callback_query.callback_id)

    try:
        response = await event_handler(event)
    except Exception:
        logger.exception(
            "telegram.polling.handler_error",
            event_id=event.event_id,
            event_type=event.event_type,
            trace_id=event.trace_id,
        )
        return

    if response is None:
        return

    chat_id = event.metadata.get("chat_id")
    if chat_id is None:
        logger.warning("telegram.polling.missing_chat_id", event_id=event.event_id)
        return

    try:
        await adapter.send_response(response, chat_id=int(chat_id))
    except Exception:
        logger.exception("telegram.polling.send_error", chat_id=chat_id)
