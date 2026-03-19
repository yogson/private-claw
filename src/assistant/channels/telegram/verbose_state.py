"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

In-memory verbose mode toggle per Telegram chat.
When verbose mode is on for a chat, each agent tool call is sent as a message.
"""


class VerboseStateService:
    """Tracks verbose mode (on/off) per Telegram chat_id."""

    def __init__(self) -> None:
        self._enabled: set[int] = set()

    def toggle(self, chat_id: int) -> bool:
        """Toggle verbose mode for chat_id. Returns True if now enabled."""
        if chat_id in self._enabled:
            self._enabled.discard(chat_id)
            return False
        else:
            self._enabled.add(chat_id)
            return True

    def is_enabled(self, chat_id: int) -> bool:
        """Return True if verbose mode is on for chat_id."""
        return chat_id in self._enabled
