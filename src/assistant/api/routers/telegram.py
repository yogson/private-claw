"""
Component ID: CMP_API_FASTAPI_GATEWAY

Telegram webhook endpoint backed by aiogram update parsing.
"""

import secrets
from collections.abc import Awaitable, Callable

import structlog
from aiogram.types import Update
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from assistant.channels.telegram.allowlist import UnauthorizedUserError
from assistant.channels.telegram.models import ChannelResponse, NormalizedEvent

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["telegram"])

TelegramEventHandler = Callable[[NormalizedEvent], Awaitable[ChannelResponse | None]]


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    """Receives Telegram webhook updates and routes them through the channel adapter."""
    runtime_config = request.app.state.runtime_config
    if not runtime_config.telegram.enabled:
        return {"ok": True}

    secret = runtime_config.telegram.webhook_secret_token
    if secret:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(header, secret):
            logger.warning("telegram.webhook.invalid_secret")
            return {"ok": True}

    adapter = getattr(request.app.state, "telegram_adapter", None)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram adapter is not initialised",
        )

    update_payload = await request.json()
    try:
        update = Update.model_validate(update_payload)
    except ValidationError:
        logger.warning("telegram.webhook.invalid_payload")
        return {"ok": True}

    try:
        event = adapter.process_update(update)
    except UnauthorizedUserError as exc:
        logger.warning("telegram.webhook.unauthorized", user_id=exc.user_id)
        return {"ok": True}

    if event is None:
        return {"ok": True}

    if event.callback_query is not None:
        await adapter.acknowledge_callback(event.callback_query.callback_id)

    response = await _dispatch_to_handler(request, event)
    if response is None:
        return {"ok": True}

    chat_id = event.metadata.get("chat_id")
    if chat_id is None:
        logger.warning("telegram.webhook.missing_chat_id", event_id=event.event_id)
        return {"ok": True}

    await adapter.send_response(response, chat_id=int(chat_id))
    return {"ok": True}


async def _dispatch_to_handler(request: Request, event: NormalizedEvent) -> ChannelResponse | None:
    handler: TelegramEventHandler | None = getattr(
        request.app.state, "telegram_event_handler", None
    )
    if handler is None:
        logger.info("telegram.webhook.no_event_handler", event_type=event.event_type)
        return None
    return await handler(event)
