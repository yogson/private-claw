"""Tests for the FSRS spaced repetition engine."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fsrs import Card, State

from assistant.extensions.language_learning.fsrs_engine import FSRSEngine
from assistant.extensions.language_learning.models import (
    CardDirection,
    PartOfSpeech,
    VocabularyEntry,
)


@pytest.fixture
def base_time() -> datetime:
    return datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def new_entry(base_time: datetime) -> VocabularyEntry:
    return VocabularyEntry(
        user_id="user-1",
        word="σπίτι",
        transliteration="spíti",
        translation="дом",
        part_of_speech=PartOfSpeech.NOUN,
        article="το",
        gender="n",
        created_at=base_time,
        updated_at=base_time,
        next_review=base_time,
        reverse_next_review=base_time,
    )


def _review_card_state(
    state: int, stability: float, difficulty: float, last_review: datetime
) -> dict:  # type: ignore[type-arg]
    """Helper to build an fsrs_card dict for a card in a specific state."""
    return {
        "card_id": 123456,
        "state": state,
        "step": None if state == State.Review else 0,
        "stability": stability,
        "difficulty": difficulty,
        "due": last_review.isoformat(),
        "last_review": last_review.isoformat(),
    }


class TestIsDue:
    """Tests for VocabularyEntry.is_due() — verifies due / not-due logic."""

    def test_card_not_due_when_future_review_date(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        """A card with a due date in the future must NOT be considered due."""
        future = base_time + timedelta(days=7)
        entry = new_entry.model_copy(update={"next_review": future, "reverse_next_review": future})
        assert not entry.is_due(CardDirection.FORWARD, as_of=base_time)
        assert not entry.is_due(CardDirection.REVERSE, as_of=base_time)

    def test_card_is_due_when_past_review_date(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        """A card whose due date has passed must be considered due."""
        past = base_time - timedelta(hours=1)
        entry = new_entry.model_copy(update={"next_review": past, "reverse_next_review": past})
        assert entry.is_due(CardDirection.FORWARD, as_of=base_time)
        assert entry.is_due(CardDirection.REVERSE, as_of=base_time)

    def test_not_due_card_excluded_from_due_words(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        """A card whose due date is in the future should be treated as not-due."""
        future = base_time + timedelta(days=3)
        entry = new_entry.model_copy(update={"next_review": future, "reverse_next_review": future})
        # Simulate the check performed by get_due_words: the card must NOT appear due
        check_time = base_time
        assert not entry.is_due(CardDirection.FORWARD, as_of=check_time)
        # Once time has moved past the due date it becomes due
        assert entry.is_due(CardDirection.FORWARD, as_of=future)


class TestFSRSEngineGetCard:
    """Tests for FSRSEngine.get_card()."""

    def test_get_card_returns_fresh_card_when_no_data(self, new_entry: VocabularyEntry) -> None:
        card = FSRSEngine.get_card(new_entry, CardDirection.FORWARD)
        assert isinstance(card, Card)
        assert card.state == State.Learning

    def test_get_card_restores_stored_state(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        card_data = _review_card_state(State.Review, 10.0, 5.0, base_time)
        entry = new_entry.model_copy(update={"fsrs_card": card_data})
        card = FSRSEngine.get_card(entry, CardDirection.FORWARD)
        assert card.state == State.Review
        assert card.stability == 10.0

    def test_get_card_reverse_uses_reverse_field(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        fwd_data = _review_card_state(State.Review, 5.0, 4.0, base_time)
        rev_data = _review_card_state(State.Learning, 2.0, 3.0, base_time)
        entry = new_entry.model_copy(update={"fsrs_card": fwd_data, "fsrs_card_reverse": rev_data})
        fwd_card = FSRSEngine.get_card(entry, CardDirection.FORWARD)
        rev_card = FSRSEngine.get_card(entry, CardDirection.REVERSE)
        assert fwd_card.state == State.Review
        assert rev_card.state == State.Learning


class TestFSRSEngineUpdateEntry:
    """Tests for FSRSEngine.update_entry()."""

    def test_invalid_rating_raises(self, new_entry: VocabularyEntry) -> None:
        with pytest.raises(ValueError, match="Rating must be 0-3"):
            FSRSEngine.update_entry(new_entry, rating=-1)
        with pytest.raises(ValueError, match="Rating must be 0-3"):
            FSRSEngine.update_entry(new_entry, rating=4)

    def test_update_forward_direction_stores_fsrs_card(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        updated = FSRSEngine.update_entry(
            new_entry, rating=2, direction=CardDirection.FORWARD, review_time=base_time
        )
        assert updated.fsrs_card is not None
        assert updated.fsrs_card_reverse is None  # untouched
        assert updated.next_review > base_time
        assert updated.total_reviews == 1
        assert updated.correct_reviews == 1
        assert updated.updated_at == base_time

    def test_update_reverse_direction_stores_fsrs_card_reverse(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        updated = FSRSEngine.update_entry(
            new_entry, rating=2, direction=CardDirection.REVERSE, review_time=base_time
        )
        assert updated.fsrs_card_reverse is not None
        assert updated.fsrs_card is None  # untouched
        assert updated.reverse_next_review > base_time
        assert updated.reverse_total_reviews == 1
        assert updated.reverse_correct_reviews == 1

    def test_again_rating_does_not_increment_correct_reviews(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        updated = FSRSEngine.update_entry(new_entry, rating=0, review_time=base_time)
        assert updated.total_reviews == 1
        assert updated.correct_reviews == 0

    def test_original_entry_unchanged(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        FSRSEngine.update_entry(new_entry, rating=2, review_time=base_time)
        assert new_entry.fsrs_card is None
        assert new_entry.total_reviews == 0

    def test_next_review_synced_with_fsrs_due(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        updated = FSRSEngine.update_entry(new_entry, rating=2, review_time=base_time)
        card = Card.from_dict(updated.fsrs_card)  # type: ignore[arg-type]
        assert updated.next_review == card.due

    def test_all_ratings_accepted(self, new_entry: VocabularyEntry, base_time: datetime) -> None:
        for rating in range(4):
            result = FSRSEngine.update_entry(new_entry, rating=rating, review_time=base_time)
            assert result.total_reviews == 1

    def test_json_serialization_roundtrip(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        """Verify no data loss through to_dict → JSON → from_dict roundtrip."""
        # Review a card to get non-trivial FSRS state
        updated = FSRSEngine.update_entry(new_entry, rating=2, review_time=base_time)
        assert updated.fsrs_card is not None

        # Serialize the FSRS card dict to JSON and back
        json_str = json.dumps(updated.fsrs_card)
        restored_dict = json.loads(json_str)
        restored_card = Card.from_dict(restored_dict)  # type: ignore[arg-type]
        original_card = Card.from_dict(updated.fsrs_card)  # type: ignore[arg-type]

        # Verify all card state is preserved through the roundtrip
        assert restored_card.state == original_card.state
        assert restored_card.stability == original_card.stability
        assert restored_card.difficulty == original_card.difficulty
        assert restored_card.due == original_card.due

        # Re-review the card using the restored state to verify no data loss
        entry_with_restored = updated.model_copy(update={"fsrs_card": restored_dict})
        t2 = base_time + timedelta(minutes=10)
        re_reviewed = FSRSEngine.update_entry(entry_with_restored, rating=2, review_time=t2)
        assert re_reviewed.total_reviews == 2
        assert re_reviewed.correct_reviews == 2


class TestFSRSEngineClassification:
    """Tests for is_new, is_learning, is_learned."""

    def test_is_new_when_no_card_data(self, new_entry: VocabularyEntry) -> None:
        assert FSRSEngine.is_new(new_entry, CardDirection.FORWARD) is True
        assert FSRSEngine.is_new(new_entry, CardDirection.REVERSE) is True

    def test_is_not_new_after_review(self, new_entry: VocabularyEntry, base_time: datetime) -> None:
        updated = FSRSEngine.update_entry(new_entry, rating=2, review_time=base_time)
        assert FSRSEngine.is_new(updated, CardDirection.FORWARD) is False
        # Reverse not reviewed yet
        assert FSRSEngine.is_new(updated, CardDirection.REVERSE) is True

    def test_is_learning_after_first_good_review(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        updated = FSRSEngine.update_entry(new_entry, rating=2, review_time=base_time)
        # After one review the card is still in Learning state (not yet graduated)
        assert FSRSEngine.is_learning(updated, CardDirection.FORWARD) is True
        assert FSRSEngine.is_learned(updated, CardDirection.FORWARD) is False

    def test_is_learned_when_card_in_review_state(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        card_data = _review_card_state(State.Review, 10.0, 5.0, base_time)
        entry = new_entry.model_copy(update={"fsrs_card": card_data})
        assert FSRSEngine.is_learned(entry, CardDirection.FORWARD) is True
        assert FSRSEngine.is_learning(entry, CardDirection.FORWARD) is False

    def test_is_learning_for_relearning_state(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        card_data = {
            "card_id": 123,
            "state": State.Relearning,
            "step": 0,
            "stability": 2.0,
            "difficulty": 5.0,
            "due": base_time.isoformat(),
            "last_review": base_time.isoformat(),
        }
        entry = new_entry.model_copy(update={"fsrs_card": card_data})
        assert FSRSEngine.is_learning(entry, CardDirection.FORWARD) is True
        assert FSRSEngine.is_learned(entry, CardDirection.FORWARD) is False


class TestFSRSEngineGraduation:
    """Test that a card graduates through learning steps to Review state."""

    def test_two_good_reviews_graduate_card(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        # First Good review — card stays in Learning (step 0 → 1)
        after_first = FSRSEngine.update_entry(
            new_entry, rating=2, direction=CardDirection.FORWARD, review_time=base_time
        )
        card_after_first = Card.from_dict(after_first.fsrs_card)  # type: ignore[arg-type]
        assert card_after_first.state == State.Learning

        # Second Good review — card graduates to Review
        t2 = base_time + timedelta(minutes=10)
        after_second = FSRSEngine.update_entry(
            after_first, rating=2, direction=CardDirection.FORWARD, review_time=t2
        )
        card_after_second = Card.from_dict(after_second.fsrs_card)  # type: ignore[arg-type]
        assert card_after_second.state == State.Review
        assert FSRSEngine.is_learned(after_second, CardDirection.FORWARD) is True

    def test_again_on_review_card_moves_to_relearning(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        card_data = _review_card_state(State.Review, 10.0, 5.0, base_time)
        entry = new_entry.model_copy(update={"fsrs_card": card_data})
        updated = FSRSEngine.update_entry(entry, rating=0, review_time=base_time)
        card = Card.from_dict(updated.fsrs_card)  # type: ignore[arg-type]
        assert card.state == State.Relearning
