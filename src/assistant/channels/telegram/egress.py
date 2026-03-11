"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Outbound Telegram message delivery via aiogram with bounded retry policy.
Handles text and interactive (inline keyboard) response types and webhook lifecycle.
"""

import asyncio
from typing import Final

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from assistant.channels.telegram.models import ChannelResponse, MessageType
from assistant.channels.telegram.reliability.audit import ChannelAuditLogger

logger = structlog.get_logger(__name__)

_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 1.0
_RETRY_BACKOFF_FACTOR = 2.0
_CALLBACK_ACK_TEXT: Final[str] = "Received."


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
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=response.text,
                    parse_mode=response.parse_mode,
                    reply_markup=self._build_inline_keyboard(response),
                )
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

    async def set_webhook(self, webhook_url: str, secret_token: str = "") -> None:
        await self._bot.set_webhook(url=webhook_url, secret_token=secret_token or None)

    async def delete_webhook(self) -> None:
        await self._bot.delete_webhook(drop_pending_updates=False)

    async def close(self) -> None:
        await self._bot.session.close()

    @staticmethod
    def _build_inline_keyboard(response: ChannelResponse) -> InlineKeyboardMarkup | None:
        if response.message_type != MessageType.INTERACTIVE or not response.actions:
            return None
        buttons = [
            [
                InlineKeyboardButton(
                    text=action.label,
                    callback_data=action.callback_data,
                )
            ]
            for action in response.actions
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @property
    def _backoff_factor(self) -> float:
        return _RETRY_BACKOFF_FACTOR
