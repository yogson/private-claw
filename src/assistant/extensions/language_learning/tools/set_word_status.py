"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

Set word status tool for the language learning agent.
"""

from typing import Any

import structlog
from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.extensions.language_learning.models import LearningStatus

logger = structlog.get_logger(__name__)

_ALLOWED_TARGET_STATUSES = {LearningStatus.LEARNING, LearningStatus.SUSPENDED}


class StatusUpdate(BaseModel):
    """A single word status update."""

    word_id: str = Field(..., description="ID of the word to update")
    status: str = Field(..., description="Target status: 'learning' or 'suspended'")


async def set_word_status(
    ctx: RunContext[TurnDeps],
    updates: list[StatusUpdate],
) -> dict[str, Any]:
    """Manually update the learning status of one or more words.

    Valid target statuses are 'learning' (to re-activate a suspended word or demote from known)
    and 'suspended' (to exclude from exercises). The statuses 'new' and 'known' are
    system-managed and cannot be set manually.

    Args:
        updates: List of word_id + status pairs to update.
    """
    user_id = ctx.deps.user_id
    store = ctx.deps.vocabulary_store
    if user_id is None or store is None:
        logger.warning("ext.language_learning.set_word_status", status="unavailable")
        return {"status": "unavailable", "reason": "language learning not configured"}

    if not updates:
        return {"status": "rejected_invalid", "reason": "updates list cannot be empty"}

    results: list[str] = []
    errors: list[str] = []

    for upd in updates:
        # Validate target status
        try:
            target_status = LearningStatus(upd.status)
        except ValueError:
            errors.append(f"{upd.word_id}: invalid status '{upd.status}'")
            continue

        if target_status not in _ALLOWED_TARGET_STATUSES:
            errors.append(
                f"{upd.word_id}: status '{upd.status}' cannot be set manually "
                "(only 'learning' and 'suspended' are allowed)"
            )
            continue

        # Get the entry
        entry = await store.get(user_id, upd.word_id)
        if entry is None:
            errors.append(f"{upd.word_id}: word not found")
            continue

        # Apply status update
        updated_entry = entry.model_copy(update={"learning_status": target_status})
        try:
            await store.update(updated_entry)
            results.append(f"{entry.word} → {target_status}")
            logger.info(
                "ext.language_learning.set_word_status",
                word_id=upd.word_id,
                word=entry.word,
                old_status=str(entry.learning_status),
                new_status=str(target_status),
            )
        except Exception as exc:
            errors.append(f"{entry.word}: {exc}")
            logger.warning(
                "ext.language_learning.set_word_status",
                word_id=upd.word_id,
                error=str(exc),
            )

    parts: list[str] = []
    if results:
        parts.append(f"Updated {len(results)} word(s): {', '.join(results)}.")
    if errors:
        parts.append(f"Errors: {'; '.join(errors)}.")

    summary = " ".join(parts) if parts else "No words were updated."
    logger.info(
        "ext.language_learning.set_word_status",
        updated_count=len(results),
        error_count=len(errors),
    )
    return {
        "status": "ok",
        "summary": summary,
        "updated": results,
        "errors": errors,
    }
