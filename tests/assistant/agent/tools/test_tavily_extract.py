"""Tests for Tavily extract argument handling and error mapping."""

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from tavily.errors import BadRequestError

from assistant.agent.tools.tavily_extract import _normalize_urls, get_tavily_extract_tool


def _tool_with_client(extract: object) -> Any:
    """Build the extract tool against a stubbed Tavily client."""
    with patch("assistant.agent.tools.tavily_extract.AsyncTavilyClient") as client_cls:
        client_cls.return_value.extract = extract
        return get_tavily_extract_tool()


def test_get_tavily_extract_tool_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert get_tavily_extract_tool() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://a.com", ["https://a.com"]),
        ("https://a.com,https://b.com", ["https://a.com", "https://b.com"]),
        ("https://a.com, https://b.com", ["https://a.com", "https://b.com"]),
        (["https://a.com", "https://b.com"], ["https://a.com", "https://b.com"]),
        (["https://a.com,https://b.com"], ["https://a.com", "https://b.com"]),
        # The shape that broke production: the model JSON-encodes the list into a string.
        ('["https://a.com"]', ["https://a.com"]),
        (['["https://a.com", "https://b.com"]'], ["https://a.com", "https://b.com"]),
        ('"https://a.com"', ["https://a.com"]),
        ("<https://a.com>", ["https://a.com"]),
        ("`https://a.com`", ["https://a.com"]),
        ("[Shop](https://a.com/x)", ["https://a.com/x"]),
        ("a.com/page", ["https://a.com/page"]),
        # A comma inside the URL must not split it.
        (
            "https://maps.google.com/maps/@34.7,33.0,17z",
            ["https://maps.google.com/maps/@34.7,33.0,17z"],
        ),
        (["https://a.com", "https://a.com"], ["https://a.com"]),
        ("", []),
        ([" ", ""], []),
    ],
)
def test_normalize_urls(raw: list[str] | str, expected: list[str]) -> None:
    assert _normalize_urls(raw)[0] == expected


def test_normalize_urls_reports_rejected() -> None:
    valid, rejected = _normalize_urls(["https://a.com", "not a url"])
    assert valid == ["https://a.com"]
    assert rejected == ["not a url"]


def test_urls_schema_accepts_bare_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Models routinely send `urls` as a string; that must not fail argument validation.

    Regression: a bare string used to raise a validation error that, with retries at 0,
    killed the whole turn with UnexpectedModelBehavior before Tavily was ever called.
    """
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    tool = _tool_with_client(AsyncMock(return_value={"results": [], "failed_results": []}))

    assert tool is not None
    args = json.dumps({"urls": "https://example.com/page"})
    validated = tool.function_schema.validator.validate_json(args)
    assert validated["urls"] == "https://example.com/page"


@pytest.mark.asyncio
async def test_string_urls_reach_tavily_as_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    extract = AsyncMock(
        return_value={
            "results": [{"url": "https://example.com/page", "raw_content": "hello"}],
            "failed_results": [],
        }
    )
    tool = _tool_with_client(extract)

    assert tool is not None
    result = await tool.function(urls="https://example.com/page")

    assert extract.call_args.kwargs["urls"] == ["https://example.com/page"]
    assert result["results"][0]["raw_content"] == "hello"


@pytest.mark.asyncio
async def test_json_encoded_list_reaches_tavily_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: `'["https://…"]'` was sent to Tavily verbatim and came back 400."""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    extract = AsyncMock(return_value={"results": [], "failed_results": []})
    tool = _tool_with_client(extract)

    assert tool is not None
    await tool.function(urls='["https://example.com/page"]')

    assert extract.call_args.kwargs["urls"] == ["https://example.com/page"]


@pytest.mark.asyncio
async def test_empty_urls_returns_error_without_calling_tavily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    extract = AsyncMock()
    tool = _tool_with_client(extract)

    assert tool is not None
    result = await tool.function(urls="   ")

    assert "No valid URL" in result["error"]
    assert result["results"] == []
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_tavily_failure_is_returned_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Tavily error must not raise: with retries=0 that aborts the entire turn."""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    extract = AsyncMock(side_effect=BadRequestError("bad url"))
    tool = _tool_with_client(extract)

    assert tool is not None
    result = await tool.function(urls=["https://example.com"])

    assert result["results"] == []
    assert "BadRequestError" in result["error"]
    assert result["failed_results"][0]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_invalid_urls_are_reported_alongside_valid_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    extract = AsyncMock(
        return_value={
            "results": [{"url": "https://example.com", "raw_content": "hi"}],
            "failed_results": [],
        }
    )
    tool = _tool_with_client(extract)

    assert tool is not None
    result = await tool.function(urls=["https://example.com", "not a url"])

    assert extract.call_args.kwargs["urls"] == ["https://example.com"]
    assert result["failed_results"] == [{"url": "not a url", "error": "not a valid URL"}]
