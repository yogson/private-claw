"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Tavily URL content extraction tool for Pydantic AI agent.
Uses TAVILY_API_KEY from environment. Tool is only registered when key is set.

Tavily API failures are returned to the model as a structured result (never raised):
with agent-level `retries=0`, a raised ModelRetry aborts the whole turn with
UnexpectedModelBehavior, which is far worse than the model reading an error and replying.
"""

import json
import os
import re
from typing import Any, Literal
from urllib.parse import urlparse

import structlog
from pydantic_ai.tools import Tool
from tavily import AsyncTavilyClient

_MAX_CONTENT_CHARS = 8_000
_BINARY_THRESHOLD = 0.1

# Wrapper punctuation models tend to leave around a URL: quotes, backticks, angle
# brackets, JSON list brackets. Parentheses are NOT stripped — they are legal in URLs
# (e.g. wikipedia.org/wiki/Cat_(disambiguation)); markdown links are unwrapped separately.
_WRAPPER_CHARS = " \t\r\n\"'`<>[]"
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((?P<url>[^)\s]+)\)")
# Split only where a new URL clearly begins, so commas inside a URL survive
# (e.g. Google Maps `/@34.7,33.0,17z`).
_URL_SPLIT_RE = re.compile(r"[\s,]+(?=https?://)")


def _is_binary(text: str, sample: int = 500) -> bool:
    chunk = text[:sample]
    if not chunk:
        return False
    non_printable = sum(1 for c in chunk if not c.isprintable() and c not in "\t\n\r")
    return (non_printable / len(chunk)) > _BINARY_THRESHOLD


def _split_items(raw: Any) -> list[str]:
    """Flatten arbitrary model input into candidate URL strings.

    Models send `urls` in every shape imaginable: a proper list, a bare string, a
    JSON-encoded list (`'["https://x"]'` — the shape that broke this tool in production),
    a markdown link, or several URLs in one comma/whitespace-separated string.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [part for item in raw for part in _split_items(item)]
    if not isinstance(raw, str):
        return [str(raw)]

    text = raw.strip()
    if not text:
        return []

    # JSON-encoded list or object (`'["https://x"]'`, `'{"url": "https://x"}'`).
    if text[0] in "[{":
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(decoded, dict):
                return _split_items(list(decoded.values()))
            if decoded != text:
                return _split_items(decoded)

    markdown = _MARKDOWN_LINK_RE.findall(text)
    if markdown:
        return markdown

    text = text.strip(_WRAPPER_CHARS)
    if "http" in text:
        parts = _URL_SPLIT_RE.split(text)
    else:
        # No scheme to anchor a split on: only break the string apart when every piece
        # is itself a plausible URL, so junk like "not a url" is rejected as a whole.
        parts = [part for part in re.split(r"[\s,]+", text) if part]
        if not all(_clean_url(part) for part in parts):
            parts = [text]
    return [cleaned for part in parts if (cleaned := part.strip(_WRAPPER_CHARS))]


def _clean_url(candidate: str) -> str | None:
    """Return a URL Tavily will accept, or None if the candidate is not a URL at all."""
    url = candidate.strip(_WRAPPER_CHARS).rstrip(",;")
    if not url or " " in url:
        return None
    if "://" not in url:
        # Tavily accepts scheme-less hosts, but only if they look like a host at all.
        if "." not in url.split("/")[0]:
            return None
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or "." not in parsed.netloc:
        return None
    return url


def _normalize_urls(urls: Any) -> tuple[list[str], list[str]]:
    """Coerce model input to (valid URLs, rejected candidates)."""
    valid: list[str] = []
    rejected: list[str] = []
    for candidate in _split_items(urls):
        cleaned = _clean_url(candidate)
        if cleaned is None:
            rejected.append(candidate)
        elif cleaned not in valid:
            valid.append(cleaned)
    return valid, rejected


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


def _error_result(error: str, failed: list[str]) -> dict[str, Any]:
    failed_results = [{"url": url, "error": error} for url in failed]
    return {
        "results": [],
        "failed_results": failed_results or [{"error": error}],
        "error": error,
    }


def get_tavily_extract_tool() -> Any | None:
    """Return Tavily extract tool if TAVILY_API_KEY is set, else None."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return None

    client = AsyncTavilyClient(api_key)
    logger = structlog.get_logger(__name__)

    async def tavily_extract(
        urls: list[str] | str,
        extract_depth: Literal["basic", "advanced"] = "basic",
        format: Literal["markdown", "text"] = "markdown",
    ) -> dict[str, Any]:
        """Read the full content of one or more web pages by URL.

        Use this when you have a specific URL and need to read its content,
        rather than searching for information. Returns extracted text for each
        URL, plus a list of any URLs that failed.

        Args:
            urls: Plain URLs to read, as a list of strings, e.g.
                ["https://example.com"]. Do not JSON-encode the list and do not wrap
                URLs in quotes, brackets or markdown link syntax.
            extract_depth: 'basic' is fast and sufficient for most pages — always start with basic.
                'advanced' does deeper extraction for JavaScript-heavy or paginated sites;
                costs more — only use if basic returns empty or incomplete content,
                never use by default.
            format: 'markdown' preserves headings and structure (preferred);
                'text' returns plain text with no markup.
        """
        raw_urls = urls
        # Models send this argument in every shape: bare string, comma-separated string,
        # JSON-encoded list. Normalizing here is what keeps the call from reaching Tavily
        # as an unparseable URL and coming back 400.
        cleaned, rejected = _normalize_urls(urls)
        if not cleaned:
            logger.warning("tavily_extract.no_valid_urls", raw_urls=raw_urls, rejected=rejected)
            return _error_result(
                "No valid URL found in `urls`. Pass plain URLs as a list of strings, "
                'e.g. ["https://example.com"].',
                rejected,
            )
        if rejected:
            logger.info("tavily_extract.rejected_urls", raw_urls=raw_urls, rejected=rejected)

        try:
            response = await client.extract(
                urls=cleaned,
                extract_depth=extract_depth,
                format=format,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("tavily_extract.failed", raw_urls=raw_urls, urls=cleaned, error=error)
            return _error_result(f"Tavily extract failed — {error}", cleaned)

        results = [_sanitize_extracted(r) for r in response.get("results", [])]
        failed = list(response.get("failed_results", []))
        failed.extend({"url": url, "error": "not a valid URL"} for url in rejected)
        logger.info(
            "tavily_extract.result",
            urls=cleaned,
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

    return Tool(
        tavily_extract,
        name="tavily_extract",
        description="Extracts the full content of one or more web pages by URL.",
    )
