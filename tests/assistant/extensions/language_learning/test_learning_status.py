"""Tests for LearningStatus transitions in FSRSEngine."""

from datetime import UTC, datetime

from fsrs import State

from assistant.extensions.language_learning.fsrs_engine import FSRSEngine
from assistant.extensions.language_learning.models import (
    CardDirection,
    LearningStatus,
    PartOfSpeech,
    VocabularyEntry,
)


def _make_entry(
    status: LearningStatus = LearningStatus.NEW,
    fsrs_card: dict | None = None,  # type: ignore[type-arg]
    fsrs_card_reverse: dict | None = None,  # type: ignore[type-arg]
) -> VocabularyEntry:
    now = datetime.now(UTC)
    return VocabularyEntry(
        user_id="user-1",
        word="σπίτι",
        transliteration="spíti",
        translation="дом",
        part_of_speech=PartOfSpeech.NOUN,
        learning_status=status,
        fsrs_card=fsrs_card,
        fsrs_card_reverse=fsrs_card_reverse,
        next_review=now,
        reverse_next_review=now,
        created_at=now,
        updated_at=now,
    )


def _review_card_dict(state: int, stability: float = 5.0, difficulty: float = 5.0) -> dict:  # type: ignore[type-arg]
    """Build a minimal fsrs_card dict for testing."""
    now = datetime.now(UTC)
    return {
        "card_id": 1,
        "state": state,
        "step": None if state == State.Review else 0,
        "stability": stability,
        "difficulty": difficulty,
        "due": now.isoformat(),
        "last_review": now.isoformat(),
    }


BASE_TIME = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)


class TestLearningStatusTransitions:
    def test_new_to_learning_after_first_review(self) -> None:
        entry = _make_entry(LearningStatus.NEW)
        updated = FSRSEngine.update_entry(entry, rating=2, review_time=BASE_TIME)
        assert updated.learning_status == LearningStatus.LEARNING

    def test_new_to_learning_even_with_poor_rating(self) -> None:
        entry = _make_entry(LearningStatus.NEW)
        updated = FSRSEngine.update_entry(entry, rating=0, review_time=BASE_TIME)
        assert updated.learning_status == LearningStatus.LEARNING

    def test_learning_stays_learning_when_only_one_direction_graduated(self) -> None:
        # Forward card in Review state, reverse card not yet reviewed
        fwd_card = _review_card_dict(State.Review)
        entry = _make_entry(LearningStatus.LEARNING, fsrs_card=fwd_card)
        # Review forward again — but reverse is still not in Review state
        updated = FSRSEngine.update_entry(
            entry, rating=2, direction=CardDirection.FORWARD, review_time=BASE_TIME
        )
        assert updated.learning_status == LearningStatus.LEARNING

    def test_learning_to_known_when_both_directions_graduated(self) -> None:
        # Both directions already in Review state
        fwd_card = _review_card_dict(State.Review, stability=10.0)
        rev_card = _review_card_dict(State.Review, stability=10.0)
        entry = _make_entry(LearningStatus.LEARNING, fsrs_card=fwd_card, fsrs_card_reverse=rev_card)
        # Reviewing forward — after this review forward is still Review,
        # reverse is already Review → should promote to KNOWN
        updated = FSRSEngine.update_entry(
            entry, rating=2, direction=CardDirection.FORWARD, review_time=BASE_TIME
        )
        assert updated.learning_status == LearningStatus.KNOWN

    def test_known_to_learning_on_again_rating(self) -> None:
        fwd_card = _review_card_dict(State.Review)
        rev_card = _review_card_dict(State.Review)
        entry = _make_entry(LearningStatus.KNOWN, fsrs_card=fwd_card, fsrs_card_reverse=rev_card)
        updated = FSRSEngine.update_entry(entry, rating=0, review_time=BASE_TIME)
        assert updated.learning_status == LearningStatus.LEARNING

    def test_known_to_learning_on_hard_rating(self) -> None:
        fwd_card = _review_card_dict(State.Review)
        rev_card = _review_card_dict(State.Review)
        entry = _make_entry(LearningStatus.KNOWN, fsrs_card=fwd_card, fsrs_card_reverse=rev_card)
        updated = FSRSEngine.update_entry(entry, rating=1, review_time=BASE_TIME)
        assert updated.learning_status == LearningStatus.LEARNING

    def test_known_stays_known_on_good_rating(self) -> None:
        fwd_card = _review_card_dict(State.Review)
        rev_card = _review_card_dict(State.Review)
        entry = _make_entry(LearningStatus.KNOWN, fsrs_card=fwd_card, fsrs_card_reverse=rev_card)
        updated = FSRSEngine.update_entry(entry, rating=2, review_time=BASE_TIME)
        assert updated.learning_status == LearningStatus.KNOWN

    def test_known_stays_known_on_easy_rating(self) -> None:
        fwd_card = _review_card_dict(State.Review)
        rev_card = _review_card_dict(State.Review)
        entry = _make_entry(LearningStatus.KNOWN, fsrs_card=fwd_card, fsrs_card_reverse=rev_card)
        updated = FSRSEngine.update_entry(entry, rating=3, review_time=BASE_TIME)
        assert updated.learning_status == LearningStatus.KNOWN

    def test_suspended_never_changes(self) -> None:
        entry = _make_entry(LearningStatus.SUSPENDED)
        updated = FSRSEngine.update_entry(entry, rating=2, review_time=BASE_TIME)
        assert updated.learning_status == LearningStatus.SUSPENDED

    def test_suspended_stays_suspended_on_poor_rating(self) -> None:
        entry = _make_entry(LearningStatus.SUSPENDED)
        updated = FSRSEngine.update_entry(entry, rating=0, review_time=BASE_TIME)
        assert updated.learning_status == LearningStatus.SUSPENDED
