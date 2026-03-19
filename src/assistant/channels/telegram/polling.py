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
from aiogram.types import MenuButtonCommands, Update

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.allowlist import UnauthorizedUserError
from assistant.channels.telegram.commands import build_bot_commands
from assistant.channels.telegram.models import ChannelResponse, NormalizedEvent
from assistant.core.config.schemas import TelegramChannelConfig

logger = structlog.get_logger(__name__)

TelegramEventHandler = Callable[[NormalizedEvent], Awaitable[ChannelResponse | None]]

_BACKOFF_MIN_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0
_BACKOFF_FACTOR = 1.5


class CancellationRegistry:
    """Tracks all running turn tasks per session for /stop support.

    A session may have multiple concurrent tasks (e.g. two messages in-flight),
    so we track a set and cancel all of them on /stop.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, set[asyncio.Task[None]]] = {}

    def register(self, session_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.setdefault(session_id, set()).add(task)

    def unregister(self, session_id: str, task: asyncio.Task[None]) -> None:
        """Remove a specific task from the registry, leaving sibling tasks intact."""
        tasks = self._tasks.get(session_id)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                del self._tasks[session_id]

    def cancel(self, session_id: str) -> bool:
        """Cancel all running tasks for session_id. Returns True if any were cancelled."""
        tasks = self._tasks.get(session_id)
        if not tasks:
            return False
        cancelled = False
        for task in list(tasks):
            cancelled |= task.cancel()
        return cancelled


async def run_polling(
    adapter: TelegramAdapter,
    config: TelegramChannelConfig,
    event_handler: TelegramEventHandler,
    *,
    stop_event: asyncio.Event | None = None,
    cancellation_registry: CancellationRegistry | None = None,
) -> None:
    """
    Long-poll Telegram for updates and dispatch them through the adapter.

    Runs until cancelled or stop_event is set. On startup, deletes any existing
    webhook so polling can receive updates. Handles transient errors with
    exponential backoff.

    Each update is dispatched as an independent asyncio task so that /stop can
    interrupt a long-running agent turn without waiting for it to complete.
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
    await _configure_bot_commands_menu(bot)

    offset: int | None = None
    backoff = _BACKOFF_MIN_SECONDS
    stop = stop_event or asyncio.Event()
    # Keep strong references to background tasks so they are not GC'd.
    _background_tasks: set[asyncio.Task[None]] = set()

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
                # Fire as a task so /stop can be processed concurrently with a
                # long-running agent turn for the same session.
                task: asyncio.Task[None] = asyncio.create_task(
                    _process_update(adapter, update, event_handler, cancellation_registry)
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
                offset = update.update_id + 1

            if config.poll_interval_seconds > 0 and updates:
                await asyncio.sleep(config.poll_interval_seconds)
    except Exception:
        logger.exception("telegram.polling.unexpected_error")
        raise
    finally:
        # Cancel any still-running background tasks on shutdown.
        pending = list(_background_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await bot.session.close()
        logger.info("telegram.polling.stopped")


async def _configure_bot_commands_menu(bot: Bot) -> None:
    # Register Telegram native commands and force Commands menu button on startup
    # to keep command discovery deterministic across runtime restarts.
    commands = build_bot_commands()
    try:
        await bot.set_my_commands(commands=commands)
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info(
            "telegram.polling.commands_menu_configured",
            commands=[f"/{item.command}" for item in commands],
        )
    except (TelegramAPIError, TelegramNetworkError) as exc:
        logger.warning("telegram.polling.commands_menu_config_failed", error=str(exc))


async def _process_update(
    adapter: TelegramAdapter,
    update: Update,
    event_handler: TelegramEventHandler,
    cancellation_registry: CancellationRegistry | None = None,
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

    # Register this task in the cancellation registry so /stop can cancel it.
    # Skip registration for /stop itself so it never appears as a cancellable turn.
    current_task = asyncio.current_task()
    should_track = (
        not adapter.is_stop_request(event)
        and cancellation_registry is not None
        and current_task is not None
    )
    if should_track:
        cancellation_registry.register(event.session_id, current_task)  # type: ignore[union-attr]

    try:
        response = await event_handler(event)
    except asyncio.CancelledError:
        logger.info(
            "telegram.polling.turn_cancelled",
            event_id=event.event_id,
            session_id=event.session_id,
            trace_id=event.trace_id,
        )
        return  # /stop handler already sends the user-facing "Stopped." response
    except Exception:
        logger.exception(
            "telegram.polling.handler_error",
            event_id=event.event_id,
            event_type=event.event_type,
            trace_id=event.trace_id,
        )
        return
    finally:
        if should_track:
            cancellation_registry.unregister(event.session_id, current_task)  # type: ignore[union-attr]

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
