"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

SM-2 Spaced Repetition algorithm implementation.

The SM-2 algorithm by Piotr Wozniak calculates optimal review intervals
based on user performance ratings.

Rating mapping:
- 0 = Again (complete blackout, SM-2 quality 0)
- 1 = Hard (remembered with difficulty, SM-2 quality 3)
- 2 = Good (remembered with some effort, SM-2 quality 4)
- 3 = Easy (remembered effortlessly, SM-2 quality 5)
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from assistant.extensions.language_learning.models import CardDirection, VocabularyEntry

# Rating to SM-2 quality mapping
RATING_TO_QUALITY: dict[int, int] = {
    0: 0,  # Again -> complete blackout
    1: 3,  # Hard -> correct with serious difficulty
    2: 4,  # Good -> correct with some hesitation
    3: 5,  # Easy -> perfect response
}

# Minimum easiness factor (prevents EF from going too low)
MIN_EASINESS_FACTOR: float = 1.3

# Default easiness factor for new cards
DEFAULT_EASINESS_FACTOR: float = 2.5

# Threshold for "learned" status (interval in days)
LEARNED_THRESHOLD_DAYS: int = 21


@dataclass
class SM2Result:
    """Result of SM-2 calculation for a single review."""

    easiness_factor: float
    interval: int
    repetitions: int
    next_review: datetime
    was_correct: bool


class SM2Engine:
    """SM-2 Spaced Repetition algorithm engine."""

    @staticmethod
    def calculate(
        rating: int,
        easiness_factor: float = DEFAULT_EASINESS_FACTOR,
        interval: int = 0,
        repetitions: int = 0,
        review_time: datetime | None = None,
    ) -> SM2Result:
        """
        Calculate new SM-2 parameters after a review.

        Args:
            rating: User rating (0-3)
            easiness_factor: Current easiness factor
            interval: Current interval in days
            repetitions: Current consecutive correct recalls
            review_time: Time of review (defaults to now)

        Returns:
            SM2Result with updated parameters
        """
        if rating < 0 or rating > 3:
            raise ValueError(f"Rating must be 0-3, got {rating}")

        now = review_time or datetime.now(UTC)
        quality = RATING_TO_QUALITY[rating]

        # Update easiness factor using SM-2 formula
        # EF' = EF + (0.1 - (5-q) * (0.08 + (5-q) * 0.02))
        new_ef = easiness_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
        new_ef = max(MIN_EASINESS_FACTOR, new_ef)

        # Check if recall was successful (quality >= 3 in SM-2 terms)
        was_correct = quality >= 3

        if not was_correct:
            # Failed recall - reset to beginning
            new_interval = 1
            new_repetitions = 0
        else:
            # Successful recall - progress through intervals
            new_repetitions = repetitions + 1

            if new_repetitions == 1:
                new_interval = 1
            elif new_repetitions == 2:
                new_interval = 6
            else:
                # For subsequent repetitions: I(n) = I(n-1) * EF
                new_interval = round(interval * new_ef)

        next_review = now + timedelta(days=new_interval)

        return SM2Result(
            easiness_factor=round(new_ef, 2),
            interval=new_interval,
            repetitions=new_repetitions,
            next_review=next_review,
            was_correct=was_correct,
        )

    @staticmethod
    def update_entry(
        entry: VocabularyEntry,
        rating: int,
        direction: CardDirection = CardDirection.FORWARD,
        review_time: datetime | None = None,
    ) -> VocabularyEntry:
        """
        Update a vocabulary entry after a review.

        Returns a new VocabularyEntry with updated SM-2 fields.
        Does not modify the original entry.

        Args:
            entry: The vocabulary entry to update
            rating: User rating (0-3)
            direction: Which direction was reviewed
            review_time: Time of review (defaults to now)

        Returns:
            New VocabularyEntry with updated SM-2 parameters
        """
        now = review_time or datetime.now(UTC)

        # Get current SM-2 fields for the direction
        sm2_fields = entry.get_sm2_fields(direction)

        # Calculate new SM-2 parameters
        result = SM2Engine.calculate(
            rating=rating,
            easiness_factor=float(sm2_fields["easiness_factor"]),
            interval=int(sm2_fields["interval"]),
            repetitions=int(sm2_fields["repetitions"]),
            review_time=now,
        )

        # Build update dict
        updates: dict[str, float | int | datetime] = {"updated_at": now}

        if direction == CardDirection.FORWARD:
            updates["easiness_factor"] = result.easiness_factor
            updates["interval"] = result.interval
            updates["repetitions"] = result.repetitions
            updates["next_review"] = result.next_review
            updates["total_reviews"] = entry.total_reviews + 1
            if result.was_correct:
                updates["correct_reviews"] = entry.correct_reviews + 1
        else:
            updates["reverse_easiness_factor"] = result.easiness_factor
            updates["reverse_interval"] = result.interval
            updates["reverse_repetitions"] = result.repetitions
            updates["reverse_next_review"] = result.next_review
            updates["reverse_total_reviews"] = entry.reverse_total_reviews + 1
            if result.was_correct:
                updates["reverse_correct_reviews"] = entry.reverse_correct_reviews + 1

        # Create new entry with updates
        return entry.model_copy(update=updates)

    @staticmethod
    def is_learned(entry: VocabularyEntry, direction: CardDirection) -> bool:
        """Check if a word is considered "learned" (interval >= 21 days)."""
        if direction == CardDirection.FORWARD:
            return entry.interval >= LEARNED_THRESHOLD_DAYS
        return entry.reverse_interval >= LEARNED_THRESHOLD_DAYS

    @staticmethod
    def is_learning(entry: VocabularyEntry, direction: CardDirection) -> bool:
        """Check if a word is in "learning" state (0 < interval < 21)."""
        if direction == CardDirection.FORWARD:
            return 0 < entry.interval < LEARNED_THRESHOLD_DAYS
        return 0 < entry.reverse_interval < LEARNED_THRESHOLD_DAYS

    @staticmethod
    def is_new(entry: VocabularyEntry, direction: CardDirection) -> bool:
        """Check if a word is "new" (never reviewed or interval = 0)."""
        if direction == CardDirection.FORWARD:
            return entry.interval == 0
        return entry.reverse_interval == 0
