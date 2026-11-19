"""
Component ID: CMP_API_FASTAPI_GATEWAY

Telegram glue for generic confirmations: deliver a turn's response followed by one
Approve/Reject message per new confirmation, and resolve button presses.

A confirmation with attachments (e.g. the full prompt diff of a ``capability_edit``) is
delivered as: every attachment part, in order, then the button message, sent as a
Telegram reply to part 1 (parts 2..N reply to part 1 too). The agent's own messages are
sent before all of this and are never replies, so they cannot pose as a part of it. The
buttons are sent only after every part was delivered; if any part or the button message
fails, the confirmation is discarded (``failed``, session slot released) and no buttons
are left behind for it.

Confirmation callbacks are always answered explicitly (real status toast + edited
message without keyboard) and never fall through to the orchestrator/LLM.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog

from assistant.channels.telegram import ChannelResponse, NormalizedEvent, TelegramAdapter
from assistant.channels.telegram.confirmation_callbacks import ConfirmationCallbackState
from assistant.channels.telegram.confirmation_render import (
    message_fits,
    render_attachment_messages,
    render_resolved_text,
    status_text,
)
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
    is returned. Every confirmation whose buttons were not sent (send or build error,
    no chat, cancellation) is discarded with a ``failed`` record, so it cannot block
    the session's single pending slot and the agent learns it was never shown.
    """
    if not confirmations:
        return response
    delivered: set[str] = set()
    details: dict[str, str] = {}
    try:
        if not chat_id:
            logger.warning("confirmation.render.no_chat_id", trace_id=trace_id)
            return response
        if response is not None:
            try:
                await adapter.send_response(response, chat_id=chat_id)
            except Exception:
                logger.exception("confirmation.render.response_send_failed", chat_id=chat_id)
        for request in confirmations:
            failure = await _send_confirmation(adapter, request, chat_id=chat_id, trace_id=trace_id)
            if failure is None:
                delivered.add(request.confirmation_id)
            else:
                details[request.confirmation_id] = failure
        return None
    finally:
        await discard_undelivered(
            service,
            [r for r in confirmations if r.confirmation_id not in delivered],
            details=details,
        )


@asynccontextmanager
async def discard_confirmations_on_error(
    service: ConfirmationService | None, confirmations: list[ConfirmationRequest]
) -> AsyncIterator[None]:
    """Discard ``confirmations`` if the wrapped block (e.g. building the turn's
    response before delivery) raises or is cancelled, then re-raise."""
    try:
        yield
    except BaseException:
        await discard_undelivered(service, confirmations)
        raise


async def discard_undelivered(
    service: ConfirmationService | None,
    confirmations: list[ConfirmationRequest],
    *,
    details: dict[str, str] | None = None,
) -> None:
    """Mark confirmations whose buttons never reached the user ``failed`` (with a record).

    ``details`` maps a confirmation id to a more specific reason (e.g. which attachment
    part failed). Shielded and error-tolerant: runs from ``finally``/``except`` blocks,
    including on cancellation. Already-resolved requests are left unchanged.
    """
    if service is None:
        return
    for request in confirmations:
        detail = (details or {}).get(request.confirmation_id, _NOT_DELIVERED_DETAIL)
        try:
            await asyncio.shield(service.discard(request.confirmation_id, detail))
        except Exception:
            logger.warning(
                "confirmation.render.discard_failed",
                confirmation_id=request.confirmation_id,
                exc_info=True,
            )


async def _send_confirmation(
    adapter: TelegramAdapter, request: ConfirmationRequest, *, chat_id: int, trace_id: str
) -> str | None:
    """Send the request's attachment parts, then its buttons; None when all were sent,
    otherwise the failure detail (the buttons are then never sent)."""
    reply_to: int | None = None
    if request.attachments:
        parts = render_attachment_messages(request)
        for index, text in enumerate(parts, start=1):
            message_id = await _send_part(
                adapter, request, text, reply_to, chat_id=chat_id, trace_id=trace_id
            )
            if message_id is None:
                return (
                    f"Part {index} of {len(parts)} of the content the user must read before "
                    "deciding could not be delivered, so the confirmation buttons were not "
                    "shown. Nothing was applied."
                )
            if reply_to is None:
                reply_to = message_id
    try:
        prompt = adapter.build_confirmation_response(
            chat_id=chat_id, trace_id=trace_id, request=request, reply_to_message_id=reply_to
        )
        sent = await adapter.send_response(prompt, chat_id=chat_id)
    except Exception:
        logger.exception(
            "confirmation.render.send_failed",
            confirmation_id=request.confirmation_id,
            chat_id=chat_id,
        )
        return _NOT_DELIVERED_DETAIL
    if sent is False:
        logger.warning(
            "confirmation.render.not_sent", confirmation_id=request.confirmation_id, chat_id=chat_id
        )
        return _NOT_DELIVERED_DETAIL
    logger.info(
        "confirmation.render.sent",
        confirmation_id=request.confirmation_id,
        kind=request.kind,
        chat_id=chat_id,
        attachment_parts=sum(len(a.parts) for a in request.attachments),
    )
    return None


async def _send_part(
    adapter: TelegramAdapter,
    request: ConfirmationRequest,
    text: str,
    reply_to: int | None,
    *,
    chat_id: int,
    trace_id: str,
) -> int | None:
    """Send one attachment part; its message id, or None if it was not (provably) sent."""
    if not message_fits(text):
        # Core bounds every part; never cut one to make it fit.
        logger.warning("confirmation.render.part_too_long", confirmation_id=request.confirmation_id)
        return None
    try:
        response = adapter.build_confirmation_part_response(
            trace_id=trace_id, request=request, text=text, reply_to_message_id=reply_to
        )
        message_ids = await adapter.send_response_message_ids(response, chat_id=chat_id)
    except Exception:
        logger.exception(
            "confirmation.render.part_send_failed",
            confirmation_id=request.confirmation_id,
            chat_id=chat_id,
        )
        return None
    if len(message_ids) != 1:
        logger.warning(
            "confirmation.render.part_not_sent",
            confirmation_id=request.confirmation_id,
            chat_id=chat_id,
            messages=len(message_ids),
        )
        return None
    return message_ids[0]


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
    # Buttons are signed for the session that requested them; a known request is
    # verified against its own session (the chat may have switched sessions since),
    # an unknown id against the chat's current session.
    known = service.get(adapter.confirmation_callback_id(event)) if service else None
    session_id = known.session_id if known is not None else event.session_id
    parsed = adapter.parse_confirmation_callback(event, session_id=session_id)
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
    if result.request is None:
        # Valid signature for this session but unknown id (e.g. pending across a
        # restart): tell the agent it expired so it does not wait for an outcome.
        await service.record_unknown_expired(parsed.confirmation_id, session_id)
    return result.request
