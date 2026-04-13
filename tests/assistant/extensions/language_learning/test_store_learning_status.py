"""Tests for VocabularyStore with LearningStatus support."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.extensions.language_learning.models import (
    LearningStatus,
    PartOfSpeech,
    VocabularyEntry,
)
from assistant.extensions.language_learning.store import VocabularyStore


@pytest.fixture
def vocabulary_dir(tmp_path: Path) -> Path:
    return tmp_path / "vocabulary"


@pytest.fixture
def store(vocabulary_dir: Path) -> VocabularyStore:
    return VocabularyStore(vocabulary_dir)


@pytest.fixture
def base_time() -> datetime:
    return datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)


def _make_entry(
    word: str = "σπίτι",
    status: LearningStatus = LearningStatus.NEW,
    interval: int = 0,
    next_review: datetime | None = None,
    tags: list[str] | None = None,
) -> VocabularyEntry:
    now = datetime.now(UTC)
    return VocabularyEntry(
        user_id="user-1",
        word=word,
        transliteration=f"trans_{word}",
        translation="тест",
        part_of_speech=PartOfSpeech.NOUN,
        learning_status=status,
        interval=interval,
        easiness_factor=2.5,
        next_review=next_review or now,
        reverse_next_review=next_review or now,
        tags=tags or [],
        created_at=now,
        updated_at=now,
    )


class TestFindByWord:
    @pytest.mark.asyncio
    async def test_find_existing_word(self, store: VocabularyStore) -> None:
        entry = _make_entry("σπίτι")
        await store.add(entry)
        result = await store.find_by_word("user-1", "σπίτι")
        assert result is not None
        assert result.word == "σπίτι"

    @pytest.mark.asyncio
    async def test_find_by_word_case_insensitive(self, store: VocabularyStore) -> None:
        entry = _make_entry("Σπίτι")
        await store.add(entry)
        result = await store.find_by_word("user-1", "σπίτι")
        assert result is not None

    @pytest.mark.asyncio
    async def test_returns_none_for_nonexistent(self, store: VocabularyStore) -> None:
        result = await store.find_by_word("user-1", "unknown")
        assert result is None


class TestGetDueWordsWithLearningStatus:
    @pytest.mark.asyncio
    async def test_new_words_always_included(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        future = base_time + timedelta(days=10)
        await store.add(_make_entry("new_word", status=LearningStatus.NEW, next_review=future))
        result = await store.get_due_words("user-1", as_of=base_time)
        assert len(result) == 1
        assert result[0].word == "new_word"

    @pytest.mark.asyncio
    async def test_suspended_words_never_included(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        past = base_time - timedelta(days=1)
        await store.add(
            _make_entry("suspended_word", status=LearningStatus.SUSPENDED, next_review=past)
        )
        result = await store.get_due_words("user-1", as_of=base_time)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_learning_words_included_when_due(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        past = base_time - timedelta(hours=1)
        await store.add(
            _make_entry("learning_word", status=LearningStatus.LEARNING, next_review=past)
        )
        result = await store.get_due_words("user-1", as_of=base_time)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_learning_words_not_included_when_not_due(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        future = base_time + timedelta(days=3)
        await store.add(
            _make_entry("learning_word", status=LearningStatus.LEARNING, next_review=future)
        )
        result = await store.get_due_words("user-1", as_of=base_time)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_known_words_included_as_refresher_after_60_days(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        # KNOWN word: last reviewed 70 days ago (interval=7, next_review = last_review + 7)
        # last_review = base_time - 70 days
        # next_review = last_review + interval = base_time - 70 + 7 = base_time - 63
        interval = 7
        last_review = base_time - timedelta(days=70)
        next_review = last_review + timedelta(days=interval)
        entry = _make_entry(
            "known_word", status=LearningStatus.KNOWN, interval=interval, next_review=next_review
        )
        await store.add(entry)
        result = await store.get_due_words("user-1", as_of=base_time)
        assert len(result) == 1
        assert result[0].word == "known_word"

    @pytest.mark.asyncio
    async def test_known_words_not_included_before_60_days(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        # KNOWN word: last reviewed 30 days ago
        interval = 7
        last_review = base_time - timedelta(days=30)
        next_review = last_review + timedelta(days=interval)
        entry = _make_entry(
            "known_word", status=LearningStatus.KNOWN, interval=interval, next_review=next_review
        )
        await store.add(entry)
        result = await store.get_due_words("user-1", as_of=base_time)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_new_words_come_before_learning_words(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        past = base_time - timedelta(days=1)
        await store.add(
            _make_entry("learning_word", status=LearningStatus.LEARNING, next_review=past)
        )
        await store.add(_make_entry("new_word", status=LearningStatus.NEW))
        result = await store.get_due_words("user-1", as_of=base_time)
        assert len(result) == 2
        assert result[0].word == "new_word"

    @pytest.mark.asyncio
    async def test_list_entries_filter_by_status(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("new_word", status=LearningStatus.NEW))
        await store.add(_make_entry("learning_word", status=LearningStatus.LEARNING))
        await store.add(_make_entry("suspended_word", status=LearningStatus.SUSPENDED))

        new_entries = await store.list_entries("user-1", status=LearningStatus.NEW)
        assert len(new_entries) == 1
        assert new_entries[0].word == "new_word"

        suspended = await store.list_entries("user-1", status=LearningStatus.SUSPENDED)
        assert len(suspended) == 1
        assert suspended[0].word == "suspended_word"
