"""Tests for LearningStatus transitions in SM2Engine."""

from assistant.extensions.language_learning.models import (
    LearningStatus,
    PartOfSpeech,
    VocabularyEntry,
)
from assistant.extensions.language_learning.sm2 import SM2Engine


def _make_entry(
    status: LearningStatus = LearningStatus.NEW, interval: int = 0, ef: float = 2.5
) -> VocabularyEntry:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return VocabularyEntry(
        user_id="user-1",
        word="σπίτι",
        transliteration="spíti",
        translation="дом",
        part_of_speech=PartOfSpeech.NOUN,
        learning_status=status,
        easiness_factor=ef,
        interval=interval,
        next_review=now,
        reverse_next_review=now,
        created_at=now,
        updated_at=now,
    )


class TestLearningStatusTransitions:
    def test_new_to_learning_after_first_review(self) -> None:
        entry = _make_entry(LearningStatus.NEW)
        updated = SM2Engine.update_entry(entry, rating=2)
        assert updated.learning_status == LearningStatus.LEARNING

    def test_new_to_learning_even_with_poor_rating(self) -> None:
        entry = _make_entry(LearningStatus.NEW)
        updated = SM2Engine.update_entry(entry, rating=0)
        assert updated.learning_status == LearningStatus.LEARNING

    def test_learning_stays_learning_for_short_interval(self) -> None:
        entry = _make_entry(LearningStatus.LEARNING, interval=6, ef=2.5)
        updated = SM2Engine.update_entry(entry, rating=2)
        # Interval 6 * 2.5 = 15, still < 21 days
        assert updated.learning_status == LearningStatus.LEARNING

    def test_learning_to_known_when_interval_and_ef_high(self) -> None:
        # Set up an entry where BOTH directions will meet the KNOWN threshold after review.
        # Forward: interval=20 and EF=2.5, next interval will be round(20 * 2.5) = 50 >= 21
        # Reverse: already at threshold (reverse_interval=21, reverse_easiness_factor=2.5)
        entry = _make_entry(LearningStatus.LEARNING, interval=20, ef=2.5)
        entry = entry.model_copy(
            update={
                "repetitions": 3,
                "reverse_interval": 21,
                "reverse_easiness_factor": 2.5,
            }
        )
        updated = SM2Engine.update_entry(entry, rating=3)
        assert updated.learning_status == LearningStatus.KNOWN

    def test_learning_stays_learning_when_only_one_direction_strong(self) -> None:
        # Forward interval is high but reverse is still 0 — should NOT promote to KNOWN
        entry = _make_entry(LearningStatus.LEARNING, interval=20, ef=2.5)
        entry = entry.model_copy(update={"repetitions": 3})
        # reverse_interval remains 0
        updated = SM2Engine.update_entry(entry, rating=3)
        assert updated.learning_status == LearningStatus.LEARNING

    def test_known_to_learning_on_poor_rating(self) -> None:
        entry = _make_entry(LearningStatus.KNOWN, interval=30, ef=2.5)
        updated = SM2Engine.update_entry(entry, rating=0)
        assert updated.learning_status == LearningStatus.LEARNING

    def test_known_to_learning_on_hard_rating(self) -> None:
        entry = _make_entry(LearningStatus.KNOWN, interval=30, ef=2.5)
        updated = SM2Engine.update_entry(entry, rating=1)
        assert updated.learning_status == LearningStatus.LEARNING

    def test_known_stays_known_on_good_rating(self) -> None:
        entry = _make_entry(LearningStatus.KNOWN, interval=30, ef=2.5)
        updated = SM2Engine.update_entry(entry, rating=2)
        assert updated.learning_status == LearningStatus.KNOWN

    def test_suspended_never_changes(self) -> None:
        entry = _make_entry(LearningStatus.SUSPENDED)
        updated = SM2Engine.update_entry(entry, rating=2)
        assert updated.learning_status == LearningStatus.SUSPENDED

    def test_suspended_stays_suspended_on_poor_rating(self) -> None:
        entry = _make_entry(LearningStatus.SUSPENDED, interval=30, ef=2.5)
        updated = SM2Engine.update_entry(entry, rating=0)
        assert updated.learning_status == LearningStatus.SUSPENDED
