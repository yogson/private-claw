"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

Tool to fetch a filtered/sorted slice of vocabulary for contextual exercise building.
"""

from typing import Any, Literal

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)

_MAX_LIMIT = 30


async def get_words(
    ctx: RunContext[TurnDeps],
    tags: list[str] | None = None,
    pos: list[str] | None = None,
    limit: int = 5,
    order_by: Literal["random", "newest", "oldest", "least_learned", "most_learned"] = "random",
    min_ease_factor: float | None = None,
    max_ease_factor: float | None = None,
) -> dict[str, Any]:
    """Fetch vocabulary words for building contextual exercises.

    Returns a filtered and sorted list of words that can be used to construct
    fill-in-the-blanks sentences, grammar exercises, or mini-dialogues.

    Args:
        tags: Restrict to words with at least one matching tag (e.g. ["food", "travel"]).
        pos: Restrict by part of speech — valid values: "noun", "verb", "adjective",
             "adverb", "phrase", "other".
        limit: Maximum number of words to return (max 30).
        order_by: Sort order — "random" (default), "newest", "oldest",
                  "least_learned" (hardest words first), "most_learned" (easiest words first).
        min_ease_factor: Only include words with FSRS stability >= this value.
        max_ease_factor: Only include words with FSRS stability <= this value.
    """
    user_id = ctx.deps.user_id
    store = ctx.deps.vocabulary_store
    if user_id is None or store is None:
        logger.warning("ext.language_learning.get_words", status="unavailable")
        return {"status": "unavailable", "reason": "language learning not configured"}

    bounded_limit = max(1, min(limit, _MAX_LIMIT))

    logger.info(
        "ext.language_learning.get_words",
        tags=tags or [],
        pos=pos or [],
        limit=bounded_limit,
        order_by=order_by,
    )

    try:
        entries = await store.get_words(
            user_id,
            tags=tags,
            pos=pos,
            limit=bounded_limit,
            order_by=order_by,
            min_stability=min_ease_factor,
            max_stability=max_ease_factor,
        )
    except Exception as exc:
        logger.warning("ext.language_learning.get_words", error=str(exc))
        return {"status": "error", "reason": str(exc)}

    words = []
    for entry in entries:
        item: dict[str, Any] = {
            "id": entry.id,
            "word": entry.word,
            "transliteration": entry.transliteration,
            "translation": entry.translation,
            "part_of_speech": entry.part_of_speech.value,
            "tags": entry.tags,
            "learning_status": entry.learning_status.value,
        }
        if entry.article:
            item["article"] = entry.article
        if entry.gender:
            item["gender"] = entry.gender.value
        if entry.verb_forms:
            item["verb_forms"] = {
                "present": entry.verb_forms.present,
                "present_tr": entry.verb_forms.present_tr,
                "aorist": entry.verb_forms.aorist,
                "aorist_tr": entry.verb_forms.aorist_tr,
                "future": entry.verb_forms.future,
                "future_tr": entry.verb_forms.future_tr,
            }
        if entry.example_sentence:
            item["example_sentence"] = entry.example_sentence
        if entry.example_translation:
            item["example_translation"] = entry.example_translation
        words.append(item)

    logger.info("ext.language_learning.get_words", status="ok", count=len(words))
    return {
        "status": "ok",
        "count": len(words),
        "words": words,
    }
