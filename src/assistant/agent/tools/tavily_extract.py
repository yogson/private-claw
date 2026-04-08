"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Tavily URL content extraction tool for Pydantic AI agent.
Uses TAVILY_API_KEY from environment. Tool is only registered when key is set.

Tavily API failures are converted to ModelRetry so the model gets a retry prompt instead of
an uncaught exception.
"""

import os
from typing import Any, Literal

import structlog
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

_MAX_CONTENT_CHARS = 8_000
_BINARY_THRESHOLD = 0.1


def _is_binary(text: str, sample: int = 500) -> bool:
    chunk = text[:sample]
    if not chunk:
        return False
    non_printable = sum(1 for c in chunk if not c.isprintable() and c not in "\t\n\r")
    return (non_printable / len(chunk)) > _BINARY_THRESHOLD


def _sanitize_extracted(result: dict[str, Any]) -> dict[str, Any]:
    """Truncate or replace raw_content to prevent binary/oversized payloads."""
    content: str = result.get("raw_content") or ""
    if _is_binary(content):
        content = "[binary content omitted]"
    elif len(content) > _MAX_CONTENT_CHARS:
        content = (
            content[:_MAX_CONTENT_CHARS]
            + f"... [{len(content) - _MAX_CONTENT_CHARS} chars omitted]"
        )
    else:
        return result
    return {**result, "raw_content": content}


def get_tavily_extract_tool() -> Any | None:
    """Return Tavily extract tool if TAVILY_API_KEY is set, else None."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return None

    client = AsyncTavilyClient(api_key)
    logger = structlog.get_logger(__name__)

    async def tavily_extract(
        urls: list[str],
        extract_depth: Literal["basic", "advanced"] = "basic",
        format: Literal["markdown", "text"] = "markdown",
    ) -> dict[str, Any]:
        """Read the full content of one or more web pages by URL.

        Use this when you have a specific URL and need to read its content,
        rather than searching for information. Returns extracted text for each
        URL, plus a list of any URLs that failed.

        Args:
            urls: One or more (comma-separated) URLs to extract content from.
            extract_depth: 'basic' is fast and sufficient for most pages — always start with basic.
                'advanced' does deeper extraction for JavaScript-heavy or paginated sites;
                costs more — only use if basic returns empty or incomplete content, never use by default.
            format: 'markdown' preserves headings and structure (preferred);
                'text' returns plain text with no markup.
        """
        try:
            response = await client.extract(
                urls=urls,
                extract_depth=extract_depth,
                format=format,
            )
            results = [_sanitize_extracted(r) for r in response.get("results", [])]
            failed = response.get("failed_results", [])
            logger.info(
                "tavily_extract.result",
                urls=urls,
                result_count=len(results),
                failed_count=len(failed),
                results=[
                    {
                        "url": r.get("url"),
                        "content_length": len(r.get("raw_content") or ""),
                        "content_preview": (r.get("raw_content") or "")[:500],
                    }
                    for r in results
                ],
                failed_urls=[f.get("url") for f in failed],
            )
            return {"results": results, "failed_results": failed}
        except BadRequestError as exc:
            raise ModelRetry(f"Tavily extract bad request: {exc}") from exc
        except UsageLimitExceededError as exc:
            raise ModelRetry(f"Tavily usage limit exceeded: {exc}") from exc
        except ForbiddenError as exc:
            raise ModelRetry(f"Tavily access denied: {exc}") from exc
        except InvalidAPIKeyError as exc:
            raise ModelRetry(f"Tavily API key error: {exc}") from exc
        except TavilyTimeoutError as exc:
            raise ModelRetry(f"Tavily request timed out: {exc}") from exc
        except Exception as exc:
            raise ModelRetry(f"Tavily extract failed: {exc}") from exc

    return Tool(
        tavily_extract,
        name="tavily_extract",
        description="Extracts the full content of one or more web pages by URL.",
    )
