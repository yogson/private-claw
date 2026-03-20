"""
Tests for Telegram command catalog and parser helpers.
"""

import pytest

from assistant.channels.telegram.commands import (
    TelegramCommand,
    build_bot_commands,
    extract_supported_command,
)


def test_build_bot_commands_contains_supported_command_set() -> None:
    commands = build_bot_commands()
    assert [f"/{item.command}" for item in commands] == [
        "/new",
        "/reset",
        "/sessions",
        "/model",
        "/capabilities",
        "/usage",
        "/stop",
        "/verbose",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/new", TelegramCommand.NEW),
        (" /NEW ", TelegramCommand.NEW),
        ("/new@my_bot", TelegramCommand.NEW),
        ("/reset", TelegramCommand.RESET),
        ("/sessions", TelegramCommand.SESSIONS),
        ("/sessions@my_bot", TelegramCommand.SESSIONS),
        ("/model", TelegramCommand.MODEL),
        ("/model@my_bot", TelegramCommand.MODEL),
        ("/usage", TelegramCommand.USAGE),
        ("/usage@my_bot", TelegramCommand.USAGE),
    ],
)
def test_extract_supported_command_matches_known_commands(
    text: str, expected: TelegramCommand
) -> None:
    assert extract_supported_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "hello",
        "new",
        "/unknown",
        "/sessions-extra",
        "/sessions_extra",
        "/model-extra",
        "/model_extra",
        "/usage-extra",
    ],
)
def test_extract_supported_command_ignores_unknown_inputs(text: str | None) -> None:
    assert extract_supported_command(text) is None
