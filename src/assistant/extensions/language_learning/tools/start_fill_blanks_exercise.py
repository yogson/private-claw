"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

Tool to launch a fill-in-the-blanks exercise in the Telegram Mini App.
The agent composes the exercise (sentences + word bank), calls this tool to
encode it and receive a WebApp URL.
"""

import os
from typing import Any

import structlog
from pydantic import ValidationError
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.extensions.language_learning.models import (
    CardDirection,
    FillBlanksPayload,
)
from assistant.extensions.language_learning.tools._encoding import encode_fill_blanks

logger = structlog.get_logger(__name__)

_WEBAPP_URL_ENV = "FILL_BLANKS_WEBAPP_URL"


async def start_fill_blanks_exercise(
    ctx: RunContext[TurnDeps],
    sentences: list[dict],
    word_bank: list[dict],
    direction: str = "forward",
) -> dict[str, Any]:
    """Launch a fill-in-the-blanks Telegram Mini App exercise.

    The agent composes the exercise content (sentences with ___ blanks and a word
    bank), then calls this tool to encode the payload and get a shareable WebApp URL.

    Args:
        sentences: List of sentence objects. Each must have:
            - id (str): unique sentence id, e.g. "s1"
            - template (str): sentence text with ___ for each blank
            - transliteration (str, optional): Latin transliteration with ___
            - translation (str, optional): Translation with ___
            - blanks (list): [{position: int, word_id: str}, ...] — position is
              zero-based blank index; word_id must match an entry in word_bank.
        word_bank: List of word chip objects. Each must have:
            - id (str): word id (matches a word_id in blanks)
            - word (str): Greek word shown on the chip
            - transliteration (str): Latin transliteration shown below the chip
            Include all words that appear in blanks, plus optional distractors.
        direction: Card direction for SM-2 scoring — "forward" or "reverse".
    """
    user_id = ctx.deps.user_id
    store = ctx.deps.vocabulary_store
    if user_id is None or store is None:
        logger.warning("ext.language_learning.start_fill_blanks", status="unavailable")
        return {"status": "unavailable", "reason": "language learning not configured"}

    try:
        card_direction = CardDirection(direction)
    except ValueError:
        return {
            "status": "rejected_invalid",
            "reason": f"Invalid direction '{direction}'. Use 'forward' or 'reverse'.",
        }

    try:
        payload = FillBlanksPayload(sentences=sentences, word_bank=word_bank)
    except (ValidationError, ValueError, TypeError) as exc:
        return {"status": "rejected_invalid", "reason": f"Invalid exercise data: {exc}"}

    if not payload.sentences:
        return {"status": "rejected_invalid", "reason": "Exercise must have at least one sentence."}

    raw_url = os.environ.get(_WEBAPP_URL_ENV)
    if raw_url is None:
        logger.warning(
            "ext.language_learning.start_fill_blanks",
            status="misconfigured",
            reason=f"{_WEBAPP_URL_ENV} not set",
        )
        return {
            "status": "error",
            "reason": "Exercise cannot be started: Fill-blanks WebApp URL is not configured.",
        }

    try:
        encoded = encode_fill_blanks(payload)
    except Exception as exc:
        logger.warning("ext.language_learning.start_fill_blanks", encode_error=str(exc))
        return {"status": "error", "reason": f"Failed to encode exercise: {exc}"}

    base_url = raw_url.rstrip("/")
    webapp_url = f"{base_url}?data={encoded}&dir={card_direction.value}"

    blank_count = sum(len(s.blanks) for s in payload.sentences)
    logger.info(
        "ext.language_learning.start_fill_blanks",
        status="ready",
        sentences=len(payload.sentences),
        blanks=blank_count,
        word_bank_size=len(payload.word_bank),
    )

    return {
        "status": "exercise_ready",
        "webapp_url": webapp_url,
        "sentence_count": len(payload.sentences),
        "blank_count": blank_count,
        "word_bank_size": len(payload.word_bank),
        "direction": card_direction.value,
        "message": (
            f"Fill-in-the-blanks exercise ready: {len(payload.sentences)} sentence(s), "
            f"{blank_count} blank(s). Tap the button to start!"
        ),
        "actions": [
            {
                "label": "✏️ Заполнить пропуски",
                "web_app_url": webapp_url,
                "callback_id": "start_fill_blanks",
                "callback_data": "",
            }
        ],
    }
