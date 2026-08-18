"""Tests for tool result parsing used when recording and replaying tool calls."""

from typing import Any

import pytest

from assistant.agent.message_converters import _parse_tool_result_content


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ({"status": "ok"}, {"status": "ok"}),
        ('{"status": "ok"}', {"status": "ok"}),
        # tavily_search returns a list; recording it as a failure made the next turn
        # replay every search as a failed tool call.
        (
            [{"title": "t", "url": "https://a.com"}],
            {"results": [{"title": "t", "url": "https://a.com"}]},
        ),
        ('[{"url": "https://a.com"}]', {"results": [{"url": "https://a.com"}]}),
        ("plain text", {"result": "plain text"}),
        (42, {"result": 42}),
        (None, {"result": None}),
    ],
)
def test_parse_tool_result_content(content: Any, expected: dict[str, Any]) -> None:
    assert _parse_tool_result_content(content) == expected


def test_parse_tool_result_content_stringifies_unknown_objects() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "opaque"

    assert _parse_tool_result_content(Opaque()) == {"result": "opaque"}
