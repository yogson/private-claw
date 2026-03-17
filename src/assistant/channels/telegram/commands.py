"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Telegram command catalog and parsing helpers.
"""

from enum import StrEnum
from typing import cast

from aiogram.types import BotCommand


class TelegramCommand(StrEnum):
    """Supported Telegram slash commands and menu metadata."""

    NEW = "/new", "Start a fresh session for this chat."
    RESET = "/reset", "Clear context for the active session."
    SESSIONS = "/sessions", "List recent sessions and resume one."
    MODEL = "/model", "Select LLM model for the current session."
    USAGE = "/usage", "Show token and cost usage statistics."

    def __new__(cls, value: str, description: str) -> "TelegramCommand":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.description = description
        return obj

    description: str


def build_bot_commands() -> list[BotCommand]:
    """Build BotCommand entries for Telegram's native commands menu."""
    return [
        BotCommand(
            command=command.value.removeprefix("/"),
            description=command.description,
        )
        for command in TelegramCommand
    ]


def extract_supported_command(text: str | None) -> TelegramCommand | None:
    """Parse a bot command token and return a known command if supported."""
    raw = (text or "").strip()
    if not raw:
        return None
    first = raw.split(maxsplit=1)[0].lower()
    if not first.startswith("/"):
        return None
    token, _, _bot_suffix = first.partition("@")
    if not token:
        return None
    candidate = TelegramCommand._value2member_map_.get(token)
    if candidate is None:
        return None
    return cast(TelegramCommand, candidate)
