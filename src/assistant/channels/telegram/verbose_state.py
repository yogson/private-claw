"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Verbose mode toggle per Telegram chat with optional filesystem persistence.
When verbose mode is on for a chat, each agent tool call is sent as a message.
State is persisted to a JSON file so it survives bot restarts.
"""

import json
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


class VerboseStateService:
    """Tracks verbose mode (on/off) per Telegram chat_id with optional persistence."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self._storage_path = storage_path
        self._enabled: set[int] = self._load()

    def toggle(self, chat_id: int) -> bool:
        """Toggle verbose mode for chat_id. Returns True if now enabled."""
        if chat_id in self._enabled:
            self._enabled.discard(chat_id)
            self._save()
            return False
        else:
            self._enabled.add(chat_id)
            self._save()
            return True

    def is_enabled(self, chat_id: int) -> bool:
        """Return True if verbose mode is on for chat_id."""
        return chat_id in self._enabled

    def _load(self) -> set[int]:
        path = self._storage_path
        if path is None or not path.exists():
            return set()
        try:
            raw = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("verbose_state.load_failed", path=str(path))
            return set()
        if not isinstance(raw, list):
            return set()
        result: set[int] = set()
        for item in raw:
            if isinstance(item, int):
                result.add(item)
        return result

    def _save(self) -> None:
        path = self._storage_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(sorted(self._enabled))
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(encoded)
            tmp_path.replace(path)
        except OSError:
            logger.warning("verbose_state.save_failed", path=str(path))
