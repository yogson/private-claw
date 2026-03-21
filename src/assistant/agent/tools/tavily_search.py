"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Tavily web search tool for Pydantic AI agent.
Uses TAVILY_API_KEY from environment. Tool is only registered when key is set.

Tavily API failures are converted to ModelRetry so the model gets a retry prompt instead of
an uncaught exception (see pydantic_ai._tool_manager / function toolset).
"""

import os
from typing import Any, Literal

from pydantic_ai.common_tools.tavily import TavilySearchResult, TavilySearchTool
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import Tool
from tavily import AsyncTavilyClient
from tavily.errors import (
    BadRequestError,
    ForbiddenError,
    InvalidAPIKeyError,
    UsageLimitExceededError,
)
from tavily.errors import (
    TimeoutError as TavilyTimeoutError,
)

_MAX_CONTENT_CHARS = 2_000
_BINARY_THRESHOLD = 0.1  # fraction of non-printable chars that marks content as binary


def _is_binary(text: str, sample: int = 500) -> bool:
    """Return True if the text looks like binary data (e.g. raw XLS/PDF bytes)."""
    chunk = text[:sample]
    if not chunk:
        return False
    non_printable = sum(1 for c in chunk if ord(c) > 126 or (ord(c) < 32 and c not in "\t\n\r"))
    return (non_printable / len(chunk)) > _BINARY_THRESHOLD


def _sanitize_result(result: TavilySearchResult) -> TavilySearchResult:
    """Truncate or replace content to prevent binary/oversized payloads inflating context."""
    content: str = result.get("content") or ""  # type: ignore[union-attr]
    if _is_binary(content):
        content = "[binary content omitted]"
    elif len(content) > _MAX_CONTENT_CHARS:
        content = content[:_MAX_CONTENT_CHARS] + f"... [{len(content) - _MAX_CONTENT_CHARS} chars omitted]"
    else:
        return result
    return {**result, "content": content}  # type: ignore[return-value]


def get_tavily_search_tool() -> Any | None:
    """Return Tavily search tool if TAVILY_API_KEY is set, else None."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return None

    inner = TavilySearchTool(client=AsyncTavilyClient(api_key), max_results=5)

    async def tavily_search(
        query: str,
        search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] = "basic",
        topic: Literal["general", "news", "finance"] = "general",
        time_range: Literal["day", "week", "month", "year"] | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> list[TavilySearchResult]:
        """Searches Tavily for the given query and returns the results."""
        try:
            results = await inner(
                query,
                search_depth=search_depth,
                topic=topic,
                time_range=time_range,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
            )
            return [_sanitize_result(r) for r in results]
        except BadRequestError as exc:
            raise ModelRetry(
                "Tavily rejected the search query: "
                f"{exc}\n"
                "If the query is only `site:...` (or similar operators), add real keywords or "
                "use include_domains with a plain keyword query. "
                "Tavily requires non-operator terms."
            ) from exc
        except UsageLimitExceededError as exc:
            raise ModelRetry(f"Tavily usage limit exceeded: {exc}") from exc
        except ForbiddenError as exc:
            raise ModelRetry(f"Tavily access denied: {exc}") from exc
        except InvalidAPIKeyError as exc:
            raise ModelRetry(f"Tavily API key error: {exc}") from exc
        except TavilyTimeoutError as exc:
            raise ModelRetry(f"Tavily request timed out: {exc}") from exc
        except Exception as exc:
            raise ModelRetry(f"Tavily search failed: {exc}") from exc

    return Tool(
        tavily_search,
        name="tavily_search",
        description="Searches Tavily for the given query and returns the results.",
    )
