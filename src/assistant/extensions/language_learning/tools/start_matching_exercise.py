"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

Start matching exercise tool for the language learning agent.
Selects a small set of due words, encodes them as CompactWordPayload, and builds
a WebApp URL pointing to the matching mini-app.
"""

import os
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.extensions.language_learning.models import (
    CardDirection,
    LearningStatus,
)
from assistant.extensions.language_learning.tools._encoding import encode_words

logger = structlog.get_logger(__name__)

# Matching works best with fewer items on screen at once
_DEFAULT_LIMIT = 8
_MAX_LIMIT = 10
_WEBAPP_URL_ENV = "MATCHING_WEBAPP_URL"


async def start_matching_exercise(
    ctx: RunContext[TurnDeps],
    direction: str = "forward",
    limit: int = _DEFAULT_LIMIT,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Start a word-translation matching exercise. Selects due words and returns an exercise URL.

    Shows Greek words and their Russian translations in two columns; the user taps
    to match each pair. Works best with 8–10 words so the screen remains readable.

    Selects words by priority: NEW words first, then LEARNING words due per FSRS schedule,
    then KNOWN words when their FSRS review date is reached. SUSPENDED words
    are never included.

    Args:
        direction: Card direction — 'forward' (Greek→Russian) or 'reverse' (Russian→Greek).
        limit: Maximum number of words to include (max 10, default 8).
        tags: Optional tag filter to restrict the word pool.
    """
    user_id = ctx.deps.user_id
    store = ctx.deps.vocabulary_store
    if user_id is None or store is None:
        logger.warning("ext.language_learning.start_matching_exercise", status="unavailable")
        return {"status": "unavailable", "reason": "language learning not configured"}

    # Validate direction
    try:
        card_direction = CardDirection(direction)
    except ValueError:
        return {
            "status": "rejected_invalid",
            "reason": f"Invalid direction '{direction}'. Use 'forward' or 'reverse'.",
        }

    # Minimum of 2: a single-pair matching game is nonsensical
    bounded_limit = max(2, min(limit, _MAX_LIMIT))

    logger.info(
        "ext.language_learning.start_matching_exercise",
        direction=direction,
        limit=bounded_limit,
        tags=tags or [],
    )

    try:
        selected = await store.get_due_words(
            user_id,
            limit=bounded_limit,
            tags=tags,
            direction=card_direction,
        )
    except Exception as exc:
        logger.warning("ext.language_learning.start_matching_exercise", error=str(exc))
        return {"status": "error", "reason": str(exc)}

    if not selected:
        logger.info("ext.language_learning.start_matching_exercise", status="no_words_due")
        return {
            "status": "no_words_due",
            "message": "No words due for review right now. Great job keeping up!",
            "word_count": 0,
        }

    # Categorize selected words for summary
    new_count = sum(1 for w in selected if w.learning_status == LearningStatus.NEW)
    due_count = sum(1 for w in selected if w.learning_status == LearningStatus.LEARNING)
    refresher_count = sum(1 for w in selected if w.learning_status == LearningStatus.KNOWN)

    # Encode words
    try:
        encoded = encode_words(selected)
    except Exception as exc:
        logger.warning("ext.language_learning.start_matching_exercise", encode_error=str(exc))
        return {"status": "error", "reason": f"Failed to encode words: {exc}"}

    # Build WebApp URL
    raw_url = os.environ.get(_WEBAPP_URL_ENV)
    if raw_url is None:
        logger.warning(
            "ext.language_learning.start_matching_exercise",
            status="misconfigured",
            reason=f"{_WEBAPP_URL_ENV} environment variable is not set",
        )
        return {
            "status": "error",
            "reason": "Exercise cannot be started: Matching WebApp URL is not configured.",
        }
    base_url = raw_url.rstrip("/")
    webapp_url = f"{base_url}?words={encoded}&dir={direction}"

    # Build summary text
    breakdown_parts: list[str] = []
    if due_count:
        breakdown_parts.append(f"{due_count} due")
    if new_count:
        breakdown_parts.append(f"{new_count} new")
    if refresher_count:
        breakdown_parts.append(f"{refresher_count} refresher")
    breakdown_str = " + ".join(breakdown_parts) if breakdown_parts else str(len(selected))
    message = (
        f"Matching exercise ready: {len(selected)} word pairs ({breakdown_str}). "
        "Tap the button to start!"
    )

    logger.info(
        "ext.language_learning.start_matching_exercise",
        status="ready",
        word_count=len(selected),
        new_count=new_count,
        due_count=due_count,
        refresher_count=refresher_count,
    )
    return {
        "status": "exercise_ready",
        "webapp_url": webapp_url,
        "word_count": len(selected),
        "breakdown": {
            "due": due_count,
            "new": new_count,
            "refresher": refresher_count,
        },
        "direction": direction,
        "message": message,
        "actions": [
            {
                "label": "🔤 Начать сопоставление",
                "web_app_url": webapp_url,
                "callback_id": "start_matching_exercise",
                "callback_data": "",
            }
        ],
    }
