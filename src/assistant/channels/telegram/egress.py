"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Outbound Telegram message delivery via aiogram with bounded retry policy.
Handles text and interactive (inline keyboard) response types. Formatted replies can go
out as native rich messages (sendRichMessage) with fallback to the entity path.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    KeyboardButton,
    MessageEntity,
    ReplyKeyboardMarkup,
    ReplyParameters,
    WebAppInfo,
)

from assistant.channels.telegram.formatter import format_markdown_for_telegram
from assistant.channels.telegram.models import ChannelResponse, MessageType
from assistant.channels.telegram.reliability.audit import ChannelAuditLogger
from assistant.channels.telegram.rich_markdown import contains_gfm_table, split_rich_markdown
from assistant.core.config.schemas import TelegramRichMessagesMode

logger = structlog.get_logger(__name__)

_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 1.0
_RETRY_BACKOFF_FACTOR = 2.0
_PARSE_ENTITIES_ERROR: Final[str] = "can't parse entities"
_CALLBACK_ACK_TEXT: Final[str] = "Received."
_TELEGRAM_MAX_MESSAGE_LENGTH: Final[int] = 4096
_CALLBACK_ANSWER_MAX_LENGTH: Final[int] = 200
# Error texts (lowercased) meaning the Bot API server does not know sendRichMessage; a 404
# (TelegramNotFound) means the same.
_UNSUPPORTED_METHOD_ERRORS: Final[tuple[str, ...]] = (
    "method not found",
    "unknown method",
    "method is not supported",
)


class _RichMessagesLatch:
    """Process-wide switch, set once the Bot API rejects sendRichMessage as unsupported."""

    unsupported: bool = False


_RICH_LATCH: Final = _RichMessagesLatch()


@dataclass(frozen=True)
class _OutboundChunk:
    """One message to send: rich Markdown, or text with entities (empty: parse_mode/plain)."""

    text: str
    entities: list[MessageEntity] = field(default_factory=list)
    rich: bool = False


def _is_unsupported_method_error(exc: TelegramAPIError) -> bool:
    if isinstance(exc, TelegramNotFound):
        return True
    message = exc.message.lower()
    return any(marker in message for marker in _UNSUPPORTED_METHOD_ERRORS)


def _fails_on_entity_path_too(exc: TelegramAPIError) -> bool:
    """Errors a resend through the entity path would hit as well (no point in falling back)."""
    return (
        isinstance(exc, TelegramForbiddenError | TelegramServerError)
        or "message to be replied not found" in exc.message.lower()
    )


class TelegramSendError(Exception):
    """Raised when all retry attempts for an outbound Telegram send have failed."""

    def __init__(self, chat_id: int, attempts: int, last_error: str) -> None:
        super().__init__(
            f"Failed to send to chat {chat_id} after {attempts} attempts: {last_error}"
        )
        self.chat_id = chat_id
        self.attempts = attempts
        self.last_error = last_error


class TelegramEgress:
    """
    Sends ChannelResponse objects to Telegram via the Bot API.

    Applies bounded exponential-backoff retry on transient failures.
    Pass a ChannelAuditLogger to emit structured retry telemetry for all send attempts.
    """

    def __init__(
        self,
        bot_token: str,
        max_attempts: int = _MAX_RETRY_ATTEMPTS,
        base_delay: float = _RETRY_BASE_DELAY_SECONDS,
        audit_logger: ChannelAuditLogger | None = None,
        rich_messages: TelegramRichMessagesMode = TelegramRichMessagesMode.TABLES_ONLY,
    ) -> None:
        self._bot = Bot(token=bot_token)
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._audit_logger = audit_logger
        self._rich_messages = rich_messages

    async def send(self, response: ChannelResponse, chat_id: int) -> bool:
        """
        Send a ChannelResponse to the given Telegram chat_id.

        Returns True on success. Raises TelegramSendError after exhausting retries.
        """
        await self.send_message_ids(response, chat_id)
        return True

    async def send_message_ids(self, response: ChannelResponse, chat_id: int) -> list[int]:
        """Like :meth:`send`, returning the Telegram message ids of the sent chunks (a
        chunk whose id Telegram did not report is missing from the list)."""
        message_ids: list[int] = []
        last_error = ""
        attempts_made = 0

        chunks = self._plan_chunks(response)
        plain_fallback = False
        sent_chunks = 0
        base_reply_markup = self._build_reply_markup(response)
        attempt_limit = self._max_attempts
        attempt = 0
        while attempt < attempt_limit:
            attempt += 1
            attempts_made = attempt
            if self._audit_logger is not None:
                self._audit_logger.log_egress_attempt(
                    chat_id=chat_id,
                    response_id=response.response_id,
                    attempt=attempt,
                    trace_id=response.trace_id,
                )
            try:
                for chunk_index, chunk in enumerate(chunks[sent_chunks:], start=sent_chunks):
                    is_last_chunk = chunk_index == len(chunks) - 1
                    reply_markup = base_reply_markup if is_last_chunk else None
                    send_kwargs: dict[str, Any] = {
                        "chat_id": chat_id,
                        "reply_markup": reply_markup,
                    }
                    if chunk.rich:
                        send_kwargs["rich_message"] = InputRichMessage(markdown=chunk.text)
                    else:
                        send_kwargs["text"] = chunk.text
                        if plain_fallback:
                            pass
                        elif chunk.entities:
                            send_kwargs["entities"] = chunk.entities
                        else:
                            send_kwargs["parse_mode"] = response.parse_mode
                    if chunk_index == 0 and response.reply_to_message_id is not None:
                        send_kwargs["reply_parameters"] = ReplyParameters(
                            message_id=response.reply_to_message_id,
                            allow_sending_without_reply=False,
                        )
                    if chunk.rich:
                        message = await self._bot.send_rich_message(**send_kwargs)
                    else:
                        message = await self._bot.send_message(**send_kwargs)
                    sent_chunks += 1
                    message_id = getattr(message, "message_id", None)
                    if isinstance(message_id, int):
                        message_ids.append(message_id)
                if self._audit_logger is not None:
                    self._audit_logger.log_egress_success(
                        chat_id=chat_id,
                        response_id=response.response_id,
                        attempts=attempt,
                        trace_id=response.trace_id,
                    )
                else:
                    logger.info(
                        "telegram.egress.sent",
                        chat_id=chat_id,
                        response_id=response.response_id,
                        trace_id=response.trace_id,
                        attempt=attempt,
                    )
                return message_ids
            except TelegramRetryAfter as exc:
                last_error = str(exc)
                if self._audit_logger is not None:
                    self._audit_logger.log_egress_retry_after(
                        chat_id=chat_id,
                        response_id=response.response_id,
                        attempt=attempt,
                        retry_after=float(exc.retry_after),
                        trace_id=response.trace_id,
                    )
                else:
                    logger.warning(
                        "telegram.egress.retry_after",
                        chat_id=chat_id,
                        attempt=attempt,
                        retry_after=exc.retry_after,
                        error=last_error,
                    )
                if attempt < attempt_limit:
                    await asyncio.sleep(float(exc.retry_after))
                continue
            except TelegramNetworkError as exc:
                last_error = str(exc)
                if self._audit_logger is not None:
                    self._audit_logger.log_egress_network_error(
                        chat_id=chat_id,
                        response_id=response.response_id,
                        attempt=attempt,
                        error=last_error,
                        trace_id=response.trace_id,
                    )
                else:
                    logger.warning(
                        "telegram.egress.network_error",
                        chat_id=chat_id,
                        attempt=attempt,
                        error=last_error,
                    )
            except TelegramAPIError as exc:
                last_error = str(exc)
                if (
                    sent_chunks < len(chunks)
                    and chunks[sent_chunks].rich
                    and not _fails_on_entity_path_too(exc)
                ):
                    # Rich message rejected: send this and the remaining chunks through the
                    # entity path on an extra attempt (chunks already delivered stay as is).
                    chunks = chunks[:sent_chunks] + self._entity_chunks(
                        "\n\n".join(chunk.text for chunk in chunks[sent_chunks:])
                    )
                    attempt_limit += 1
                    self._handle_rich_failure(exc, chat_id, response, attempt)
                    continue
                if (
                    isinstance(exc, TelegramBadRequest)
                    and _PARSE_ENTITIES_ERROR in last_error.lower()
                    and not plain_fallback
                ):
                    # Markup rejected by Telegram: resend the same text once, unformatted,
                    # on an extra attempt so it works even when retries are used up.
                    plain_fallback = True
                    attempt_limit += 1
                    logger.warning(
                        "telegram.egress.plain_text_fallback",
                        chat_id=chat_id,
                        response_id=response.response_id,
                        trace_id=response.trace_id,
                        attempt=attempt,
                        error=last_error,
                    )
                    continue
                if self._audit_logger is not None:
                    self._audit_logger.log_egress_api_error(
                        chat_id=chat_id,
                        response_id=response.response_id,
                        attempt=attempt,
                        error=last_error,
                        trace_id=response.trace_id,
                    )
                else:
                    logger.warning(
                        "telegram.egress.api_error",
                        chat_id=chat_id,
                        attempt=attempt,
                        error=last_error,
                    )
                break

            if attempt < attempt_limit:
                delay = self._base_delay * (self._backoff_factor**attempt)
                await asyncio.sleep(delay)

        if self._audit_logger is not None:
            self._audit_logger.log_egress_failure(
                chat_id=chat_id,
                response_id=response.response_id,
                attempts=attempts_made,
                error=last_error,
                trace_id=response.trace_id,
            )
        raise TelegramSendError(chat_id=chat_id, attempts=attempts_made, last_error=last_error)

    def _plan_chunks(self, response: ChannelResponse) -> list[_OutboundChunk]:
        use_formatting = response.parse_mode is None and not response.plain_text
        if not use_formatting:
            return [_OutboundChunk(text=chunk) for chunk in self._split_text(response.text)]
        rich_chunks = self._plan_rich_chunks(response.text)
        if rich_chunks is not None:
            return [_OutboundChunk(text=chunk, rich=True) for chunk in rich_chunks]
        return self._entity_chunks(response.text)

    def _plan_rich_chunks(self, markdown: str) -> list[str] | None:
        """Rich message chunks for this reply, or None to use the entity path."""
        if self._rich_messages == TelegramRichMessagesMode.OFF or _RICH_LATCH.unsupported:
            return None
        if self._rich_messages == TelegramRichMessagesMode.TABLES_ONLY and not (
            contains_gfm_table(markdown)
        ):
            return None
        return split_rich_markdown(markdown)

    @staticmethod
    def _entity_chunks(markdown: str) -> list[_OutboundChunk]:
        return [
            _OutboundChunk(text=text, entities=entities)
            for text, entities in format_markdown_for_telegram(markdown)
        ]

    def _handle_rich_failure(
        self, exc: TelegramAPIError, chat_id: int, response: ChannelResponse, attempt: int
    ) -> None:
        if self._audit_logger is not None:
            self._audit_logger.log_egress_api_error(
                chat_id=chat_id,
                response_id=response.response_id,
                attempt=attempt,
                error=str(exc),
                trace_id=response.trace_id,
            )
        if _is_unsupported_method_error(exc) and not _RICH_LATCH.unsupported:
            _RICH_LATCH.unsupported = True
            logger.warning(
                "telegram.egress.rich_messages_unsupported",
                chat_id=chat_id,
                response_id=response.response_id,
                trace_id=response.trace_id,
                error=str(exc),
            )
        logger.warning(
            "telegram.egress.rich_fallback",
            chat_id=chat_id,
            response_id=response.response_id,
            trace_id=response.trace_id,
            attempt=attempt,
            error=str(exc),
        )

    async def acknowledge_callback(self, callback_id: str, text: str | None = None) -> None:
        """Answer a callback query (stops the client spinner); text shows as a toast."""
        toast = (text or _CALLBACK_ACK_TEXT)[:_CALLBACK_ANSWER_MAX_LENGTH]
        await self._bot.answer_callback_query(callback_query_id=callback_id, text=toast)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        """Replace a sent message's text (plain) and drop its inline keyboard.

        An edit to identical content (e.g. a repeated press of an already-resolved
        confirmation) is rejected by Telegram as "message is not modified": the message
        is already in the requested state, so that is not an error.
        """
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text[:_TELEGRAM_MAX_MESSAGE_LENGTH],
                reply_markup=None,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                raise
            logger.debug("telegram.edit_not_modified", chat_id=chat_id, message_id=message_id)

    async def close(self) -> None:
        await self._bot.session.close()

    @staticmethod
    def _build_reply_markup(
        response: ChannelResponse,
    ) -> InlineKeyboardMarkup | ReplyKeyboardMarkup | None:
        if response.message_type != MessageType.INTERACTIVE or not response.actions:
            return None
        if response.ui_kind == "reply_keyboard":
            keyboard: list[list[KeyboardButton]] = []
            for action in response.actions:
                if action.web_app_url:
                    btn = KeyboardButton(
                        text=action.label,
                        web_app=WebAppInfo(url=action.web_app_url),
                    )
                else:
                    btn = KeyboardButton(text=action.label)
                keyboard.append([btn])
            return ReplyKeyboardMarkup(
                keyboard=keyboard,
                resize_keyboard=True,
                one_time_keyboard=True,
            )
        inline_buttons: list[list[InlineKeyboardButton]] = []
        for action in response.actions:
            if action.web_app_url:
                inline_btn = InlineKeyboardButton(
                    text=action.label,
                    web_app=WebAppInfo(url=action.web_app_url),
                )
            else:
                inline_btn = InlineKeyboardButton(
                    text=action.label,
                    callback_data=action.callback_data,
                )
            inline_buttons.append([inline_btn])
        return InlineKeyboardMarkup(inline_keyboard=inline_buttons)

    @staticmethod
    def _split_text(text: str) -> list[str]:
        if len(text) <= _TELEGRAM_MAX_MESSAGE_LENGTH:
            return [text]
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + _TELEGRAM_MAX_MESSAGE_LENGTH, len(text))
            if end < len(text):
                split_at = text.rfind("\n", start, end)
                if split_at > start:
                    end = split_at + 1
            chunk = text[start:end]
            chunks.append(chunk)
            start = end
        return chunks

    @property
    def _backoff_factor(self) -> float:
        return _RETRY_BACKOFF_FACTOR
