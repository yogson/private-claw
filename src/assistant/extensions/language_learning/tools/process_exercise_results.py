"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

Process exercise results tool for the language learning agent.
Accepts the JSON payload sent by the Mini App via web_app_data and applies
FSRS spaced-repetition updates to the user's vocabulary store.
"""

import json
from typing import Any

import structlog
from pydantic import ValidationError
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.extensions.language_learning.models import (
    CardDirection,
    ExerciseResultPayload,
    LearningStatus,
)

logger = structlog.get_logger(__name__)


async def process_exercise_results(
    ctx: RunContext[TurnDeps],
    results_json: str,
) -> dict[str, Any]:
    """Process flashcard exercise results received from the Mini App.

    Call this tool when the user submits exercise results (the message text
    will be a JSON string matching the ExerciseResultPayload schema).

    Args:
        results_json: JSON string containing exercise results from the Mini App.
            Must match the ExerciseResultPayload schema with a ``results`` list
            of CardResult objects (word_id, rating 0-3, optional time_ms,
            optional direction).
    """
    user_id = ctx.deps.user_id
    store = ctx.deps.vocabulary_store
    if user_id is None or store is None:
        logger.warning("ext.language_learning.process_exercise_results", status="unavailable")
        return {"status": "unavailable", "reason": "language learning not configured"}

    # Parse JSON payload
    try:
        raw = json.loads(results_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return {
            "status": "rejected_invalid",
            "reason": f"Invalid JSON payload: {exc}",
        }

    # Validate against ExerciseResultPayload schema
    try:
        payload = ExerciseResultPayload.model_validate(raw)
    except ValidationError as exc:
        return {
            "status": "rejected_invalid",
            "reason": f"Payload does not match expected schema: {exc}",
        }

    if not payload.results:
        return {
            "status": "rejected_invalid",
            "reason": "No results provided in payload.",
        }

    logger.info(
        "ext.language_learning.process_exercise_results",
        user_id=user_id,
        result_count=len(payload.results),
    )

    try:
        updated_entries = await store.process_exercise_results(
            user_id=user_id,
            results=payload.results,
        )
    except Exception as exc:
        logger.warning("ext.language_learning.process_exercise_results", error=str(exc))
        return {"status": "error", "reason": str(exc)}

    # Build per-status breakdown of updated entries
    status_counts: dict[str, int] = {}
    updated_count = 0
    not_found_count = 0

    for _word_id, entry in updated_entries.items():
        if entry is None:
            not_found_count += 1
            continue
        updated_count += 1
        status_key = entry.learning_status.value
        status_counts[status_key] = status_counts.get(status_key, 0) + 1

    # Build human-readable summary
    direction_summary: dict[str, int] = {}
    for result in payload.results:
        dir_key = (
            result.direction.value
            if isinstance(result.direction, CardDirection)
            else str(result.direction)
        )
        direction_summary[dir_key] = direction_summary.get(dir_key, 0) + 1

    summary_parts: list[str] = [f"Updated {updated_count} word(s)."]
    if status_counts:
        breakdown = ", ".join(
            f"{count} {LearningStatus(status).value}" for status, count in status_counts.items()
        )
        summary_parts.append(f"Status breakdown: {breakdown}.")
    if not_found_count:
        summary_parts.append(f"{not_found_count} word(s) not found and skipped.")

    summary = " ".join(summary_parts)

    logger.info(
        "ext.language_learning.process_exercise_results",
        status="ok",
        updated=updated_count,
        not_found=not_found_count,
        status_counts=status_counts,
    )
    return {
        "status": "ok",
        "summary": summary,
        "updated_count": updated_count,
        "not_found_count": not_found_count,
        "status_breakdown": status_counts,
        "direction_summary": direction_summary,
    }
