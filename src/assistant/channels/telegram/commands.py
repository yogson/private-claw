"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Telegram command catalog and parsing helpers.
"""

from dataclasses import dataclass
from enum import StrEnum

from aiogram.types import BotCommand


class TelegramCommand(StrEnum):
    """Supported Telegram slash commands handled by the runtime."""

    NEW = "/new"
    RESET = "/reset"
    SESSIONS = "/sessions"


@dataclass(frozen=True)
class TelegramCommandSpec:
    """Command metadata used for Telegram native commands menu registration."""

    command: TelegramCommand
    description: str


TELEGRAM_COMMAND_SPECS: tuple[TelegramCommandSpec, ...] = (
    TelegramCommandSpec(
        command=TelegramCommand.NEW,
        description="Start a fresh session for this chat.",
    ),
    TelegramCommandSpec(
        command=TelegramCommand.RESET,
        description="Clear context for the active session.",
    ),
    TelegramCommandSpec(
        command=TelegramCommand.SESSIONS,
        description="List recent sessions and resume one.",
    ),
)


def build_bot_commands() -> list[BotCommand]:
    """Build BotCommand entries for Telegram's native commands menu."""
    return [
        BotCommand(
            command=spec.command.value.removeprefix("/"),
            description=spec.description,
        )
        for spec in TELEGRAM_COMMAND_SPECS
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
    try:
        return TelegramCommand(token)
    except ValueError:
        return None
