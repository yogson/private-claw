"""Tests for SM-2 spaced repetition algorithm."""

from datetime import UTC, datetime, timedelta

import pytest

from assistant.extensions.language_learning.models import (
    CardDirection,
    PartOfSpeech,
    VocabularyEntry,
)
from assistant.extensions.language_learning.sm2 import (
    DEFAULT_EASINESS_FACTOR,
    IMMEDIATE_RETRY_RATINGS,
    LEARNED_THRESHOLD_DAYS,
    MIN_EASINESS_FACTOR,
    SM2Engine,
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


class TestSM2Calculate:
    """Tests for SM2Engine.calculate()."""

    def test_invalid_rating_raises(self) -> None:
        with pytest.raises(ValueError, match="Rating must be 0-3"):
            SM2Engine.calculate(rating=-1)
        with pytest.raises(ValueError, match="Rating must be 0-3"):
            SM2Engine.calculate(rating=4)

    def test_first_correct_review_sets_interval_1(self, base_time: datetime) -> None:
        result = SM2Engine.calculate(rating=2, review_time=base_time)
        assert result.interval == 1
        assert result.repetitions == 1
        assert result.was_correct is True
        assert result.next_review == base_time + timedelta(days=1)

    def test_second_correct_review_sets_interval_6(self, base_time: datetime) -> None:
        result = SM2Engine.calculate(rating=2, repetitions=1, interval=1, review_time=base_time)
        assert result.interval == 6
        assert result.repetitions == 2
        assert result.was_correct is True
        assert result.next_review == base_time + timedelta(days=6)

    def test_third_correct_review_uses_ef(self, base_time: datetime) -> None:
        ef = 2.5
        result = SM2Engine.calculate(
            rating=2, repetitions=2, interval=6, easiness_factor=ef, review_time=base_time
        )
        expected_interval = round(6 * result.easiness_factor)
        assert result.interval == expected_interval
        assert result.repetitions == 3

    def test_failed_review_resets_progress(self, base_time: datetime) -> None:
        result = SM2Engine.calculate(rating=0, repetitions=5, interval=30, review_time=base_time)
        assert result.interval == 1
        assert result.repetitions == 0
        assert result.was_correct is False

    def test_hard_rating_still_progresses(self, base_time: datetime) -> None:
        result = SM2Engine.calculate(rating=1, review_time=base_time)
        assert result.interval == 1
        assert result.repetitions == 1
        assert result.was_correct is True

    def test_easy_rating_increases_ef(self, base_time: datetime) -> None:
        result = SM2Engine.calculate(rating=3, easiness_factor=2.5, review_time=base_time)
        assert result.easiness_factor > 2.5

    def test_again_rating_decreases_ef(self, base_time: datetime) -> None:
        result = SM2Engine.calculate(rating=0, easiness_factor=2.5, review_time=base_time)
        assert result.easiness_factor < 2.5

    def test_ef_never_below_minimum(self, base_time: datetime) -> None:
        result = SM2Engine.calculate(
            rating=0, easiness_factor=MIN_EASINESS_FACTOR, review_time=base_time
        )
        assert result.easiness_factor >= MIN_EASINESS_FACTOR

    def test_default_ef(self) -> None:
        result = SM2Engine.calculate(rating=2)
        assert result.easiness_factor >= MIN_EASINESS_FACTOR

    def test_again_rating_sets_next_review_to_now(self, base_time: datetime) -> None:
        """Rating 0 (Again) should schedule immediate re-review."""
        result = SM2Engine.calculate(rating=0, review_time=base_time)
        assert result.next_review == base_time
        assert 0 in IMMEDIATE_RETRY_RATINGS

    def test_hard_rating_sets_next_review_to_now(self, base_time: datetime) -> None:
        """Rating 1 (Hard) should schedule immediate re-review."""
        result = SM2Engine.calculate(rating=1, review_time=base_time)
        assert result.next_review == base_time
        assert 1 in IMMEDIATE_RETRY_RATINGS

    def test_good_rating_schedules_next_day(self, base_time: datetime) -> None:
        """Rating 2 (Good) should NOT use immediate retry — normal SM-2 schedule."""
        result = SM2Engine.calculate(rating=2, review_time=base_time)
        assert result.next_review > base_time

    def test_easy_rating_schedules_next_day(self, base_time: datetime) -> None:
        """Rating 3 (Easy) should NOT use immediate retry — normal SM-2 schedule."""
        result = SM2Engine.calculate(rating=3, review_time=base_time)
        assert result.next_review > base_time

    def test_immediate_retry_interval_unchanged(self, base_time: datetime) -> None:
        """SM-2 interval value is preserved even when next_review is set to now."""
        result = SM2Engine.calculate(rating=0, repetitions=5, interval=30, review_time=base_time)
        # Interval resets per SM-2, but next_review is immediate
        assert result.interval == 1
        assert result.next_review == base_time


class TestSM2UpdateEntry:
    """Tests for SM2Engine.update_entry()."""

    def test_update_forward_direction(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        review_time = base_time + timedelta(hours=1)
        updated = SM2Engine.update_entry(
            new_entry, rating=2, direction=CardDirection.FORWARD, review_time=review_time
        )

        assert updated.id == new_entry.id
        assert updated.interval == 1
        assert updated.repetitions == 1
        assert updated.total_reviews == 1
        assert updated.correct_reviews == 1
        assert updated.next_review == review_time + timedelta(days=1)
        assert updated.updated_at == review_time

        # Reverse direction should be unchanged
        assert updated.reverse_interval == 0
        assert updated.reverse_repetitions == 0
        assert updated.reverse_total_reviews == 0

    def test_update_reverse_direction(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        review_time = base_time + timedelta(hours=1)
        updated = SM2Engine.update_entry(
            new_entry, rating=3, direction=CardDirection.REVERSE, review_time=review_time
        )

        assert updated.reverse_interval == 1
        assert updated.reverse_repetitions == 1
        assert updated.reverse_total_reviews == 1
        assert updated.reverse_correct_reviews == 1
        assert updated.reverse_next_review == review_time + timedelta(days=1)

        # Forward direction should be unchanged
        assert updated.interval == 0
        assert updated.repetitions == 0

    def test_failed_review_increments_total_only(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        updated = SM2Engine.update_entry(new_entry, rating=0, review_time=base_time)
        assert updated.total_reviews == 1
        assert updated.correct_reviews == 0

    def test_original_entry_unchanged(
        self, new_entry: VocabularyEntry, base_time: datetime
    ) -> None:
        original_interval = new_entry.interval
        SM2Engine.update_entry(new_entry, rating=2, review_time=base_time)
        assert new_entry.interval == original_interval


class TestSM2LearningStages:
    """Tests for learning stage classification."""

    def test_is_new(self, new_entry: VocabularyEntry) -> None:
        assert SM2Engine.is_new(new_entry, CardDirection.FORWARD) is True
        assert SM2Engine.is_new(new_entry, CardDirection.REVERSE) is True

    def test_is_learning(self, new_entry: VocabularyEntry) -> None:
        learning_entry = new_entry.model_copy(update={"interval": 6})
        assert SM2Engine.is_learning(learning_entry, CardDirection.FORWARD) is True
        assert SM2Engine.is_new(learning_entry, CardDirection.FORWARD) is False

    def test_is_learned(self, new_entry: VocabularyEntry) -> None:
        learned_entry = new_entry.model_copy(update={"interval": LEARNED_THRESHOLD_DAYS})
        assert SM2Engine.is_learned(learned_entry, CardDirection.FORWARD) is True
        assert SM2Engine.is_learning(learned_entry, CardDirection.FORWARD) is False

    def test_reverse_learning_stages(self, new_entry: VocabularyEntry) -> None:
        mixed_entry = new_entry.model_copy(
            update={
                "interval": LEARNED_THRESHOLD_DAYS,  # learned forward
                "reverse_interval": 5,  # learning reverse
            }
        )
        assert SM2Engine.is_learned(mixed_entry, CardDirection.FORWARD) is True
        assert SM2Engine.is_learning(mixed_entry, CardDirection.REVERSE) is True


class TestSM2RatingSequences:
    """Tests for sequences of reviews."""

    def test_consecutive_good_ratings(self, base_time: datetime) -> None:
        ef = DEFAULT_EASINESS_FACTOR
        interval = 0
        repetitions = 0
        time = base_time

        # Simulate 5 consecutive "Good" ratings
        intervals = []
        for _ in range(5):
            result = SM2Engine.calculate(
                rating=2,
                easiness_factor=ef,
                interval=interval,
                repetitions=repetitions,
                review_time=time,
            )
            ef = result.easiness_factor
            interval = result.interval
            repetitions = result.repetitions
            intervals.append(interval)
            time = result.next_review

        # Intervals should be: 1, 6, then increasing
        assert intervals[0] == 1
        assert intervals[1] == 6
        assert all(intervals[i] < intervals[i + 1] for i in range(2, len(intervals) - 1))

    def test_mixed_ratings_sequence(self, base_time: datetime) -> None:
        ef = DEFAULT_EASINESS_FACTOR
        interval = 0
        repetitions = 0
        time = base_time

        # Good, Good, Again, Good, Good
        ratings = [2, 2, 0, 2, 2]
        results = []

        for rating in ratings:
            result = SM2Engine.calculate(
                rating=rating,
                easiness_factor=ef,
                interval=interval,
                repetitions=repetitions,
                review_time=time,
            )
            ef = result.easiness_factor
            interval = result.interval
            repetitions = result.repetitions
            results.append(result)
            time = result.next_review

        # After "Again" (index 2), should reset
        assert results[2].repetitions == 0
        assert results[2].interval == 1

        # Should start building up again
        assert results[3].repetitions == 1
        assert results[4].repetitions == 2
