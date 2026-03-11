"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

TelegramAdapter: composed entry point for Telegram ingress and egress.
Wires together AllowlistGuard, TelegramIngress (with optional transcription),
and TelegramEgress.
"""

from typing import Any

import structlog
from aiogram.types import Update

from assistant.channels.telegram.allowlist import AllowlistGuard, UnauthorizedUserError
from assistant.channels.telegram.egress import TelegramEgress
from assistant.channels.telegram.ingestion.transcription import VoiceTranscriptionService
from assistant.channels.telegram.ingress import TelegramIngress
from assistant.channels.telegram.models import ChannelResponse, NormalizedEvent
from assistant.core.config.schemas import TelegramChannelConfig

logger = structlog.get_logger(__name__)


class TelegramAdapter:
    """
    Composed Telegram channel adapter.

    Exposes process_update() for sync ingress normalization and
    process_update_async() for voice-transcription-enriched normalization.
    Enforces allowlist on every inbound update.

    Pass a VoiceTranscriptionService at construction to enable synchronous
    MTProto transcript enrichment for voice messages.
    """

    def __init__(
        self,
        config: TelegramChannelConfig,
        transcription_service: VoiceTranscriptionService | None = None,
    ) -> None:
        self._config = config
        guard = AllowlistGuard(config.allowlist)
        self._ingress = TelegramIngress(guard, transcription_service=transcription_service)
        self._egress = TelegramEgress(bot_token=config.bot_token)

    def process_update(self, update: dict[str, Any] | Update) -> NormalizedEvent | None:
        """
        Normalize a raw Telegram update dict into a NormalizedEvent.

        Returns None for unsupported update types.
        Unauthorized users are rejected; UnauthorizedUserError is logged and
        re-raised so the caller can handle the webhook response appropriately.
        Voice events will not have MTProto transcript; use process_update_async().
        """
        try:
            update_payload = (
                update.model_dump(mode="python", exclude_none=True, by_alias=True)
                if isinstance(update, Update)
                else update
            )
            return self._ingress.normalize(update_payload)
        except UnauthorizedUserError:
            raise
        except Exception:
            logger.exception("telegram.adapter.process_update.error")
            return None

    async def process_update_async(self, update: dict[str, Any] | Update) -> NormalizedEvent | None:
        """
        Normalize a Telegram update with MTProto transcription enrichment for voice.

        For voice messages, calls the configured VoiceTranscriptionService before
        returning the event. Falls back to sync normalization when transcription
        is not configured or fails.
        """
        try:
            update_payload = (
                update.model_dump(mode="python", exclude_none=True, by_alias=True)
                if isinstance(update, Update)
                else update
            )
            return await self._ingress.normalize_async(update_payload)
        except UnauthorizedUserError:
            raise
        except Exception:
            logger.exception("telegram.adapter.process_update_async.error")
            return None

    async def send_response(self, response: ChannelResponse, chat_id: int) -> bool:
        """
        Deliver a ChannelResponse to the specified Telegram chat.

        Returns True on successful delivery. Raises TelegramSendError after
        all retry attempts are exhausted.
        """
        return await self._egress.send(response, chat_id)

    async def acknowledge_callback(self, callback_id: str) -> None:
        """Acknowledges a callback query so Telegram client stops the loading spinner."""
        await self._egress.acknowledge_callback(callback_id)

    async def set_webhook(self) -> None:
        """Registers Telegram webhook for this bot token."""
        await self._egress.set_webhook(
            webhook_url=self._config.webhook_url,
            secret_token=self._config.webhook_secret_token,
        )

    async def delete_webhook(self) -> None:
        """Removes Telegram webhook for this bot token."""
        await self._egress.delete_webhook()

    async def close(self) -> None:
        """Closes underlying network resources."""
        await self._egress.close()
