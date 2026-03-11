"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Telegram channel adapter package.
"""

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.models import (
    ActionButton,
    AttachmentMeta,
    CallbackQueryMeta,
    ChannelResponse,
    EventType,
    MessageType,
    NormalizedEvent,
    VoiceMeta,
)

__all__ = [
    "TelegramAdapter",
    "NormalizedEvent",
    "ChannelResponse",
    "EventType",
    "MessageType",
    "VoiceMeta",
    "AttachmentMeta",
    "CallbackQueryMeta",
    "ActionButton",
]
