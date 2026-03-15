"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Outbound Telegram message delivery via aiogram with bounded retry policy.
Handles text and interactive (inline keyboard) response types.
"""

import asyncio
from typing import Final

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from assistant.channels.telegram.formatter import format_markdown_for_telegram
from assistant.channels.telegram.models import ChannelResponse, MessageType
from assistant.channels.telegram.reliability.audit import ChannelAuditLogger

logger = structlog.get_logger(__name__)

_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 1.0
_RETRY_BACKOFF_FACTOR = 2.0
_CALLBACK_ACK_TEXT: Final[str] = "Received."
_TELEGRAM_MAX_MESSAGE_LENGTH: Final[int] = 4096


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
    ) -> None:
        self._bot = Bot(token=bot_token)
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._audit_logger = audit_logger

    async def send(self, response: ChannelResponse, chat_id: int) -> bool:
        """
        Send a ChannelResponse to the given Telegram chat_id.

        Returns True on success. Raises TelegramSendError after exhausting retries.
        """
        last_error = ""
        attempts_made = 0

        use_formatting = response.parse_mode is None
        if use_formatting:
            formatted_chunks = format_markdown_for_telegram(response.text)
        else:
            text_chunks = (
                [response.text]
                if response.message_type == MessageType.INTERACTIVE
                else self._split_text(response.text)
            )
            formatted_chunks = [(chunk, []) for chunk in text_chunks]

        sent_chunks = 0
        for attempt in range(1, self._max_attempts + 1):
            attempts_made = attempt
            if self._audit_logger is not None:
                self._audit_logger.log_egress_attempt(
                    chat_id=chat_id,
                    response_id=response.response_id,
                    attempt=attempt,
                    trace_id=response.trace_id,
                )
            try:
                for chunk_text, chunk_entities in formatted_chunks[sent_chunks:]:
                    reply_markup = self._build_reply_markup(response)
                    send_kwargs: dict = {
                        "chat_id": chat_id,
                        "text": chunk_text,
                        "reply_markup": reply_markup,
                    }
                    if chunk_entities:
                        send_kwargs["entities"] = chunk_entities
                    else:
                        send_kwargs["parse_mode"] = response.parse_mode
                    await self._bot.send_message(**send_kwargs)
                    sent_chunks += 1
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
                return True
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
                if attempt < self._max_attempts:
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

            if attempt < self._max_attempts:
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

    async def acknowledge_callback(self, callback_id: str) -> None:
        await self._bot.answer_callback_query(
            callback_query_id=callback_id, text=_CALLBACK_ACK_TEXT
        )

    async def close(self) -> None:
        await self._bot.session.close()

    @staticmethod
    def _build_reply_markup(
        response: ChannelResponse,
    ) -> InlineKeyboardMarkup | ReplyKeyboardMarkup | None:
        if response.message_type != MessageType.INTERACTIVE or not response.actions:
            return None
        if response.ui_kind == "reply_keyboard":
            keyboard: list[list[KeyboardButton]] = [
                [KeyboardButton(text=action.label)] for action in response.actions
            ]
            return ReplyKeyboardMarkup(
                keyboard=keyboard,
                resize_keyboard=True,
                one_time_keyboard=True,
            )
        inline_buttons: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text=action.label,
                    callback_data=action.callback_data,
                )
            ]
            for action in response.actions
        ]
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
