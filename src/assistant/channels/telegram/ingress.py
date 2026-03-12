"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Compatibility facade for Telegram ingress service symbols.
"""

from assistant.channels.telegram.ingress_service import (
    _VOICE_MISSING_TRANSCRIPT,
    TelegramIngress,
)

__all__ = ["TelegramIngress", "_VOICE_MISSING_TRANSCRIPT"]
