"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

Search vocabulary tool for the language learning agent.
"""

from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.extensions.language_learning.models import LearningStatus

logger = structlog.get_logger(__name__)

_MAX_LIMIT = 50
_DEFAULT_LIMIT = 20


def _format_entry(entry: Any) -> dict[str, Any]:
    """Format a VocabularyEntry into a readable dict for the agent."""
    now = datetime.now(UTC)
    is_due = entry.next_review <= now

    result: dict[str, Any] = {
        "id": entry.id,
        "word": entry.word,
        "transliteration": entry.transliteration,
        "translation": entry.translation,
        "part_of_speech": str(entry.part_of_speech),
        "status": str(entry.learning_status),
        "tags": entry.tags,
        "next_review": entry.next_review.isoformat(),
        "interval_days": entry.interval,
        "easiness_factor": round(entry.easiness_factor, 2),
        "total_reviews": entry.total_reviews,
        "due_now": is_due,
    }
    if entry.article:
        result["article"] = entry.article
    if entry.gender:
        result["gender"] = str(entry.gender)
    if entry.example_sentence:
        result["example_sentence"] = entry.example_sentence
    return result


async def search_vocabulary(
    ctx: RunContext[TurnDeps],
    query: str = "",
    tags: list[str] | None = None,
    status: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Search and browse the user's vocabulary list.

    Args:
        query: Search text matched against word, transliteration, or translation.
            Leave empty to list all.
        tags: Filter by one or more tags (OR logic — any matching tag is included).
        status: Filter by learning status: new, learning, known, or suspended.
        limit: Maximum number of results to return (max 50).
    """
    user_id = ctx.deps.user_id
    store = ctx.deps.vocabulary_store
    if user_id is None or store is None:
        logger.warning("ext.language_learning.search_vocabulary", status="unavailable")
        return {"status": "unavailable", "reason": "language learning not configured"}

    bounded_limit = max(1, min(limit, _MAX_LIMIT))

    # Validate status filter
    status_filter: LearningStatus | None = None
    if status is not None:
        try:
            status_filter = LearningStatus(status)
        except ValueError:
            return {
                "status": "rejected_invalid",
                "reason": (
                    f"Invalid status '{status}'. Valid values: new, learning, known, suspended"
                ),
            }

    logger.info(
        "ext.language_learning.search_vocabulary",
        query=query[:80] if query else "",
        tags=tags or [],
        status=status,
        limit=bounded_limit,
    )

    try:
        if query:
            entries = await store.search(user_id, query, limit=bounded_limit * 3)
            # Apply status/tags filter after search
            if status_filter is not None:
                entries = [e for e in entries if e.learning_status == status_filter]
            if tags:
                tag_set = set(tags)
                entries = [e for e in entries if tag_set.intersection(e.tags)]
            entries = entries[:bounded_limit]
        else:
            entries = await store.list_entries(
                user_id,
                tags=tags,
                status=status_filter,
                limit=bounded_limit,
            )
    except Exception as exc:
        logger.warning("ext.language_learning.search_vocabulary", error=str(exc))
        return {"status": "error", "reason": str(exc)}

    formatted = [_format_entry(e) for e in entries]

    logger.info(
        "ext.language_learning.search_vocabulary",
        result_count=len(formatted),
    )
    return {
        "status": "ok",
        "count": len(formatted),
        "entries": formatted,
    }
