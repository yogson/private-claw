"""
Component ID: CMP_API_FASTAPI_GATEWAY

Telegram glue for generic confirmations: deliver a turn's response followed by one
Approve/Reject message per new confirmation, and resolve button presses.

Confirmation callbacks are always answered explicitly (real status toast + edited
message without keyboard) and never fall through to the orchestrator/LLM.
"""

import structlog

from assistant.channels.telegram import ChannelResponse, NormalizedEvent, TelegramAdapter
from assistant.channels.telegram.confirmation_callbacks import ConfirmationCallbackState
from assistant.channels.telegram.confirmation_render import render_resolved_text, status_text
from assistant.channels.telegram.polling import acknowledge_callback_safely
from assistant.core.confirmation import (
    ConfirmationRequest,
    ConfirmationService,
    ConfirmationUiRef,
)

logger = structlog.get_logger(__name__)

_NOT_DELIVERED_DETAIL = "The confirmation buttons could not be delivered to the user."
_PROCESSING_FAILED_TEXT = "⚠️ Failed: could not process this confirmation."


async def deliver_with_confirmations(
    adapter: TelegramAdapter,
    service: ConfirmationService | None,
    *,
    response: ChannelResponse | None,
    confirmations: list[ConfirmationRequest],
    chat_id: int | None,
    trace_id: str,
) -> ChannelResponse | None:
    """Send ``response`` and then one message per confirmation.

    Without confirmations the response is returned unchanged for the caller to send.
    With confirmations everything is sent here (so the buttons arrive last) and None
    is returned. Undeliverable confirmations are discarded so they cannot block the
    session's single pending slot.
    """
    if not confirmations:
        return response
    if not chat_id:
        await _discard_all(service, confirmations)
        logger.warning("confirmation.render.no_chat_id", trace_id=trace_id)
        return response
    if response is not None:
        try:
            await adapter.send_response(response, chat_id=chat_id)
        except Exception:
            logger.exception("confirmation.render.response_send_failed", chat_id=chat_id)
    for request in confirmations:
        prompt = adapter.build_confirmation_response(
            chat_id=chat_id, trace_id=trace_id, request=request
        )
        try:
            await adapter.send_response(prompt, chat_id=chat_id)
            logger.info(
                "confirmation.render.sent",
                confirmation_id=request.confirmation_id,
                kind=request.kind,
                chat_id=chat_id,
            )
        except Exception:
            logger.exception(
                "confirmation.render.send_failed",
                confirmation_id=request.confirmation_id,
                chat_id=chat_id,
            )
            await _discard_all(service, [request])
    return None


async def handle_confirmation_callback(
    event: NormalizedEvent,
    adapter: TelegramAdapter,
    service: ConfirmationService | None,
) -> None:
    """Resolve an Approve/Reject press; always answer the callback and edit the message."""
    if event.callback_query is None:
        return
    chat_id = int(event.metadata.get("chat_id", 0))
    message_id = event.callback_query.origin_message_id
    original_text = str(event.metadata.get("origin_message_text") or "")
    answer = _PROCESSING_FAILED_TEXT
    edited_text: str | None = None
    try:
        request = await _resolve(event, adapter, service, chat_id, message_id)
        answer = status_text(request)
        edited_text = render_resolved_text(original_text, request)
    except Exception:
        logger.exception("confirmation.callback.failed", trace_id=event.trace_id)
    finally:
        await acknowledge_callback_safely(adapter, event.callback_query.callback_id, answer)
    if edited_text is not None and chat_id and message_id is not None:
        try:
            await adapter.edit_message_text(chat_id, message_id, edited_text)
        except Exception:
            logger.warning(
                "confirmation.callback.edit_failed",
                chat_id=chat_id,
                message_id=message_id,
                exc_info=True,
            )


async def _resolve(
    event: NormalizedEvent,
    adapter: TelegramAdapter,
    service: ConfirmationService | None,
    chat_id: int,
    message_id: int | None,
) -> ConfirmationRequest | None:
    parsed = adapter.parse_confirmation_callback(event)
    if parsed.state != ConfirmationCallbackState.VALID or parsed.decision is None:
        logger.info(
            "confirmation.callback.not_valid", state=parsed.state.value, trace_id=event.trace_id
        )
        return None
    if service is None:
        return None
    ui_ref = (
        ConfirmationUiRef(chat_id=chat_id, message_id=message_id)
        if chat_id and message_id is not None
        else None
    )
    result = await service.resolve(
        parsed.confirmation_id, parsed.decision, user_id=event.user_id, ui_ref=ui_ref
    )
    logger.info(
        "confirmation.callback.resolved",
        confirmation_id=parsed.confirmation_id,
        known=result.request is not None,
        changed=result.changed,
        status=result.request.status.value if result.request else "unknown",
        trace_id=event.trace_id,
    )
    return result.request


async def _discard_all(
    service: ConfirmationService | None, confirmations: list[ConfirmationRequest]
) -> None:
    if service is None:
        return
    for request in confirmations:
        await service.discard(request.confirmation_id, _NOT_DELIVERED_DETAIL)
