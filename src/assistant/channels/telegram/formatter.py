"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Markdown-to-Telegram formatter using telegramify-markdown.

Converts agent markdown output into (text, entities) pairs for Telegram Bot API,
handles long messages via split_entities, and falls back to plain text on errors.
"""

from __future__ import annotations

import structlog
from aiogram.types import MessageEntity
from telegramify_markdown import convert, split_entities
from telegramify_markdown.config import get_runtime_config

logger = structlog.get_logger(__name__)

_TELEGRAM_MAX_UTF16_LEN = 4096
_CONFIGURED = False


def _ensure_clean_headings() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    cfg = get_runtime_config()
    cfg.markdown_symbol.heading_level_1 = ""
    cfg.markdown_symbol.heading_level_2 = ""
    cfg.markdown_symbol.heading_level_3 = ""
    cfg.markdown_symbol.heading_level_4 = ""
    cfg.markdown_symbol.heading_level_5 = ""
    cfg.markdown_symbol.heading_level_6 = ""
    _CONFIGURED = True


def format_markdown_for_telegram(
    markdown: str, max_utf16_len: int = _TELEGRAM_MAX_UTF16_LEN
) -> list[tuple[str, list[MessageEntity]]]:
    """
    Convert markdown text to Telegram (text, entities) chunks.

    Uses telegramify-markdown for parsing and entity generation. Splits long
    messages at newlines while preserving entities. Returns plain-text chunks
    on conversion errors.

    Returns:
        List of (chunk_text, chunk_entities) for send_message(entities=...).
    """
    _ensure_clean_headings()
    try:
        text, entities = convert(markdown, latex_escape=True)
    except Exception:
        logger.warning(
            "telegram.formatter.convert_failed",
            exc_info=True,
        )
        return [(markdown, [])]

    try:
        chunks: list[tuple[str, list[MessageEntity]]] = []
        for chunk_text, chunk_entities in split_entities(
            text, entities, max_utf16_len=max_utf16_len
        ):
            aiogram_entities = [_to_aiogram_entity(e) for e in chunk_entities]
            chunks.append((chunk_text, aiogram_entities))
        return chunks if chunks else [(text, [])]
    except Exception:
        logger.warning(
            "telegram.formatter.split_failed",
            exc_info=True,
        )
        return [(text, [])]


def _to_aiogram_entity(entity: object) -> MessageEntity:
    d: object = getattr(entity, "to_dict", lambda: {})()
    if not isinstance(d, dict):
        raise TypeError(f"Expected dict from entity.to_dict(), got {type(d)}")
    filtered = {k: v for k, v in d.items() if v is not None}
    return MessageEntity.model_validate(filtered)
