"""Tests for Tavily tool error handling."""

from unittest.mock import patch

import pytest
from pydantic_ai.exceptions import ModelRetry
from tavily.errors import BadRequestError

from assistant.agent.tools.tavily_search import get_tavily_search_tool


def test_get_tavily_search_tool_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert get_tavily_search_tool() is None


@pytest.mark.asyncio
async def test_bad_request_maps_to_model_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    class BoomTavily:
        async def __call__(self, *args: object, **kwargs: object) -> None:
            raise BadRequestError(
                "Query cannot consist only of site: operators. Please provide search terms."
            )

    with patch("assistant.agent.tools.tavily_search.TavilySearchTool", return_value=BoomTavily()):
        tool = get_tavily_search_tool()

    assert tool is not None
    with pytest.raises(ModelRetry) as exc_info:
        await tool.function(query="site:example.com/test")

    msg = exc_info.value.message.lower()
    assert "tavily rejected" in msg
    assert "site" in msg
    assert "include_domains" in msg
