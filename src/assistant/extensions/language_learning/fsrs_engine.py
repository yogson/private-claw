"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

FSRS Spaced Repetition engine.

Uses the py-fsrs library (https://github.com/open-spaced-repetition/py-fsrs)
to schedule vocabulary reviews.

Rating mapping:
- 0 = Again (complete blackout, FSRS Rating.Again)
- 1 = Hard (remembered with difficulty, FSRS Rating.Hard)
- 2 = Good (remembered with some effort, FSRS Rating.Good)
- 3 = Easy (remembered effortlessly, FSRS Rating.Easy)
"""

from datetime import UTC, datetime
from typing import Any

from fsrs import Card, Rating, Scheduler, State

from assistant.extensions.language_learning.models import (
    CardDirection,
    LearningStatus,
    VocabularyEntry,
)

# Map our 0-3 ratings to FSRS Rating enum
RATING_MAP: dict[int, Rating] = {
    0: Rating.Again,
    1: Rating.Hard,
    2: Rating.Good,
    3: Rating.Easy,
}

# FSRS scheduler (stateless; shared across all calls)
_scheduler = Scheduler()


class FSRSEngine:
    """FSRS Spaced Repetition engine."""

    @staticmethod
    def get_card(entry: VocabularyEntry, direction: CardDirection) -> Card:
        """Return the FSRS Card for the given direction.

        If the entry has no stored card state yet (word never reviewed in that
        direction), a fresh Card is created so the algorithm can initialise it.
        """
        card_data: dict[str, Any] | None = (
            entry.fsrs_card if direction == CardDirection.FORWARD else entry.fsrs_card_reverse
        )
        if card_data is None:
            return Card()
        return Card.from_dict(card_data)  # type: ignore[arg-type]

    @staticmethod
    def update_entry(
        entry: VocabularyEntry,
        rating: int,
        direction: CardDirection = CardDirection.FORWARD,
        review_time: datetime | None = None,
    ) -> VocabularyEntry:
        """Update a vocabulary entry after a review using FSRS.

        Returns a new VocabularyEntry with updated FSRS card state.
        Does not modify the original entry.

        Args:
            entry: The vocabulary entry to update.
            rating: User rating (0=Again, 1=Hard, 2=Good, 3=Easy).
            direction: Which direction was reviewed.
            review_time: Time of review (defaults to now UTC).

        Returns:
            New VocabularyEntry with updated FSRS parameters.
        """
        if rating < 0 or rating > 3:
            raise ValueError(f"Rating must be 0-3, got {rating}")

        now = review_time or datetime.now(UTC)
        fsrs_rating = RATING_MAP[rating]

        card = FSRSEngine.get_card(entry, direction)
        new_card, _ = _scheduler.review_card(card, fsrs_rating, review_datetime=now)

        was_correct = rating >= 1  # Again (0) counts as incorrect

        updates: dict[str, Any] = {"updated_at": now}

        if direction == CardDirection.FORWARD:
            updates["fsrs_card"] = new_card.to_dict()
            updates["next_review"] = new_card.due
            updates["total_reviews"] = entry.total_reviews + 1
            if was_correct:
                updates["correct_reviews"] = entry.correct_reviews + 1
        else:
            updates["fsrs_card_reverse"] = new_card.to_dict()
            updates["reverse_next_review"] = new_card.due
            updates["reverse_total_reviews"] = entry.reverse_total_reviews + 1
            if was_correct:
                updates["reverse_correct_reviews"] = entry.reverse_correct_reviews + 1

        # Compute current card states for both directions (after this review)
        if direction == CardDirection.FORWARD:
            fwd_card = new_card
            rev_card = FSRSEngine.get_card(entry, CardDirection.REVERSE)
        else:
            fwd_card = FSRSEngine.get_card(entry, CardDirection.FORWARD)
            rev_card = new_card

        new_status = FSRSEngine.apply_status_transition(entry, rating, fwd_card, rev_card)
        updates["learning_status"] = new_status

        return entry.model_copy(update=updates)

    @staticmethod
    def apply_status_transition(
        entry: VocabularyEntry,
        rating: int,
        fwd_card: Card,
        rev_card: Card,
    ) -> LearningStatus:
        """Compute the new LearningStatus after a review.

        - SUSPENDED: never changes.
        - NEW: transitions to LEARNING on first review (regardless of rating).
        - LEARNING: transitions to KNOWN only when BOTH directions have graduated
          to FSRS State.Review.
        - KNOWN: demotes to LEARNING on ratings 0 (Again) or 1 (Hard).
        """
        current = entry.learning_status

        if current == LearningStatus.SUSPENDED:
            return current

        if current == LearningStatus.NEW:
            return LearningStatus.LEARNING

        if current == LearningStatus.LEARNING:
            both_graduated = fwd_card.state == State.Review and rev_card.state == State.Review
            return LearningStatus.KNOWN if both_graduated else LearningStatus.LEARNING

        if current == LearningStatus.KNOWN:
            return LearningStatus.LEARNING if rating <= 1 else LearningStatus.KNOWN

        return current

    @staticmethod
    def is_learned(entry: VocabularyEntry, direction: CardDirection) -> bool:
        """Return True if the card has graduated to FSRS Review state."""
        card_data = (
            entry.fsrs_card if direction == CardDirection.FORWARD else entry.fsrs_card_reverse
        )
        if card_data is None:
            return False
        return Card.from_dict(card_data).state == State.Review  # type: ignore[arg-type]

    @staticmethod
    def is_learning(entry: VocabularyEntry, direction: CardDirection) -> bool:
        """Return True if the card is in FSRS Learning or Relearning state."""
        card_data = (
            entry.fsrs_card if direction == CardDirection.FORWARD else entry.fsrs_card_reverse
        )
        if card_data is None:
            return False
        state = Card.from_dict(card_data).state  # type: ignore[arg-type]
        return state in (State.Learning, State.Relearning)

    @staticmethod
    def is_new(entry: VocabularyEntry, direction: CardDirection) -> bool:
        """Return True if the word has never been reviewed in this direction."""
        if direction == CardDirection.FORWARD:
            return entry.fsrs_card is None
        return entry.fsrs_card_reverse is None
