"""Tests for VocabularyStore."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.extensions.language_learning.models import (
    CardDirection,
    CardResult,
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


def make_entry(
    user_id: str = "user-1",
    word: str = "σπίτι",
    translation: str = "дом",
    tags: list[str] | None = None,
    next_review: datetime | None = None,
    reverse_next_review: datetime | None = None,
) -> VocabularyEntry:
    now = datetime.now(UTC)
    return VocabularyEntry(
        user_id=user_id,
        word=word,
        transliteration=f"trans_{word}",
        translation=translation,
        part_of_speech=PartOfSpeech.NOUN,
        article="το",
        tags=tags or [],
        next_review=next_review or now,
        reverse_next_review=reverse_next_review or now,
        created_at=now,
        updated_at=now,
    )


class TestVocabularyStoreCRUD:
    """Tests for basic CRUD operations."""

    @pytest.mark.asyncio
    async def test_add_entry(self, store: VocabularyStore) -> None:
        entry = make_entry()
        created = await store.add(entry)
        assert created.id == entry.id
        assert created.word == "σπίτι"

    @pytest.mark.asyncio
    async def test_add_duplicate_raises(self, store: VocabularyStore) -> None:
        entry = make_entry()
        await store.add(entry)
        with pytest.raises(ValueError, match="already exists"):
            await store.add(entry)

    @pytest.mark.asyncio
    async def test_get_entry(self, store: VocabularyStore) -> None:
        entry = make_entry()
        await store.add(entry)
        retrieved = await store.get("user-1", entry.id)
        assert retrieved is not None
        assert retrieved.word == "σπίτι"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, store: VocabularyStore) -> None:
        result = await store.get("user-1", "nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_entry(self, store: VocabularyStore) -> None:
        entry = make_entry()
        await store.add(entry)
        updated_entry = entry.model_copy(update={"translation": "дом, жилище"})
        result = await store.update(updated_entry)
        assert result.translation == "дом, жилище"

    @pytest.mark.asyncio
    async def test_update_nonexistent_raises(self, store: VocabularyStore) -> None:
        entry = make_entry()
        with pytest.raises(ValueError, match="not found"):
            await store.update(entry)

    @pytest.mark.asyncio
    async def test_delete_entry(self, store: VocabularyStore) -> None:
        entry = make_entry()
        await store.add(entry)
        deleted = await store.delete("user-1", entry.id)
        assert deleted is True
        retrieved = await store.get("user-1", entry.id)
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, store: VocabularyStore) -> None:
        deleted = await store.delete("user-1", "nonexistent")
        assert deleted is False


class TestVocabularyStoreList:
    """Tests for list and search operations."""

    @pytest.mark.asyncio
    async def test_list_empty(self, store: VocabularyStore) -> None:
        result = await store.list_entries("user-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_all(self, store: VocabularyStore) -> None:
        await store.add(make_entry(word="σπίτι"))
        await store.add(make_entry(word="νερό"))
        await store.add(make_entry(word="ψωμί"))

        result = await store.list_entries("user-1")
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_list_with_tags_filter(self, store: VocabularyStore) -> None:
        await store.add(make_entry(word="σπίτι", tags=["home", "basics"]))
        await store.add(make_entry(word="νερό", tags=["food", "basics"]))
        await store.add(make_entry(word="ψωμί", tags=["food"]))

        # Filter by single tag
        result = await store.list_entries("user-1", tags=["home"])
        assert len(result) == 1
        assert result[0].word == "σπίτι"

        # Filter by multiple tags (OR logic)
        result = await store.list_entries("user-1", tags=["home", "food"])
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_list_with_pagination(self, store: VocabularyStore) -> None:
        for i in range(5):
            await store.add(make_entry(word=f"word{i}"))

        result = await store.list_entries("user-1", limit=3)
        assert len(result) == 3

        result = await store.list_entries("user-1", limit=3, offset=3)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_search_by_word(self, store: VocabularyStore) -> None:
        await store.add(make_entry(word="σπίτι", translation="дом"))
        await store.add(make_entry(word="νερό", translation="вода"))

        result = await store.search("user-1", "σπίτι")
        assert len(result) == 1
        assert result[0].word == "σπίτι"

    @pytest.mark.asyncio
    async def test_search_by_translation(self, store: VocabularyStore) -> None:
        await store.add(make_entry(word="σπίτι", translation="дом"))
        await store.add(make_entry(word="νερό", translation="вода"))

        result = await store.search("user-1", "вода")
        assert len(result) == 1
        assert result[0].word == "νερό"

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self, store: VocabularyStore) -> None:
        await store.add(make_entry(word="Σπίτι", translation="Дом"))

        result = await store.search("user-1", "σπίτι")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_find_by_word(self, store: VocabularyStore) -> None:
        await store.add(make_entry(word="σπίτι"))
        await store.add(make_entry(word="νερό"))

        result = await store.find_by_word("user-1", "σπίτι")
        assert result is not None
        assert result.word == "σπίτι"

        result = await store.find_by_word("user-1", "unknown")
        assert result is None


class TestVocabularyStoreDueWords:
    """Tests for due words functionality."""

    @pytest.mark.asyncio
    async def test_get_due_words_empty_store(self, store: VocabularyStore) -> None:
        result = await store.get_due_words("user-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_due_words_all_due(self, store: VocabularyStore, base_time: datetime) -> None:
        past = base_time - timedelta(days=1)
        await store.add(make_entry(word="σπίτι", next_review=past))
        await store.add(make_entry(word="νερό", next_review=past))

        result = await store.get_due_words("user-1", as_of=base_time)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_due_words_none_due(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        future = base_time + timedelta(days=1)
        await store.add(make_entry(word="σπίτι", next_review=future))

        result = await store.get_due_words("user-1", as_of=base_time)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_get_due_words_mixed(self, store: VocabularyStore, base_time: datetime) -> None:
        past = base_time - timedelta(days=1)
        future = base_time + timedelta(days=1)
        await store.add(make_entry(word="σπίτι", next_review=past))
        await store.add(make_entry(word="νερό", next_review=future))

        result = await store.get_due_words("user-1", as_of=base_time)
        assert len(result) == 1
        assert result[0].word == "σπίτι"

    @pytest.mark.asyncio
    async def test_get_due_words_with_limit(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        past = base_time - timedelta(days=1)
        for i in range(5):
            await store.add(make_entry(word=f"word{i}", next_review=past))

        result = await store.get_due_words("user-1", limit=3, as_of=base_time)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_get_due_words_with_tags(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        past = base_time - timedelta(days=1)
        await store.add(make_entry(word="σπίτι", tags=["home"], next_review=past))
        await store.add(make_entry(word="νερό", tags=["food"], next_review=past))

        result = await store.get_due_words("user-1", tags=["home"], as_of=base_time)
        assert len(result) == 1
        assert result[0].word == "σπίτι"

    @pytest.mark.asyncio
    async def test_get_due_words_reverse_direction(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        past = base_time - timedelta(days=1)
        future = base_time + timedelta(days=1)

        # Due for forward, not reverse
        await store.add(make_entry(word="σπίτι", next_review=past, reverse_next_review=future))
        # Due for reverse, not forward
        await store.add(make_entry(word="νερό", next_review=future, reverse_next_review=past))

        forward_due = await store.get_due_words(
            "user-1", direction=CardDirection.FORWARD, as_of=base_time
        )
        assert len(forward_due) == 1
        assert forward_due[0].word == "σπίτι"

        reverse_due = await store.get_due_words(
            "user-1", direction=CardDirection.REVERSE, as_of=base_time
        )
        assert len(reverse_due) == 1
        assert reverse_due[0].word == "νερό"


class TestVocabularyStoreReview:
    """Tests for review update functionality."""

    @pytest.mark.asyncio
    async def test_update_after_review(self, store: VocabularyStore, base_time: datetime) -> None:
        entry = make_entry(next_review=base_time)
        await store.add(entry)

        updated = await store.update_after_review(
            "user-1", entry.id, rating=2, review_time=base_time
        )

        assert updated is not None
        assert updated.interval == 1
        assert updated.repetitions == 1
        assert updated.total_reviews == 1
        assert updated.correct_reviews == 1

    @pytest.mark.asyncio
    async def test_update_after_review_nonexistent(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        result = await store.update_after_review(
            "user-1", "nonexistent", rating=2, review_time=base_time
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_process_exercise_results(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        entry1 = make_entry(word="σπίτι")
        entry2 = make_entry(word="νερό")
        await store.add(entry1)
        await store.add(entry2)

        results = [
            CardResult(word_id=entry1.id, rating=2),
            CardResult(word_id=entry2.id, rating=3),
        ]

        updated = await store.process_exercise_results("user-1", results, review_time=base_time)

        assert len(updated) == 2
        assert updated[entry1.id] is not None
        assert updated[entry1.id].total_reviews == 1
        assert updated[entry2.id] is not None
        assert updated[entry2.id].total_reviews == 1

    @pytest.mark.asyncio
    async def test_process_exercise_results_with_missing(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        entry = make_entry(word="σπίτι")
        await store.add(entry)

        results = [
            CardResult(word_id=entry.id, rating=2),
            CardResult(word_id="nonexistent", rating=3),
        ]

        updated = await store.process_exercise_results("user-1", results, review_time=base_time)

        assert updated[entry.id] is not None
        assert updated["nonexistent"] is None


class TestVocabularyStoreProgress:
    """Tests for progress calculation."""

    @pytest.mark.asyncio
    async def test_get_progress_empty(self, store: VocabularyStore) -> None:
        progress = await store.get_progress("user-1")
        assert progress.total_words == 0
        assert progress.accuracy_percent == 0.0

    @pytest.mark.asyncio
    async def test_get_progress_with_words(
        self, store: VocabularyStore, base_time: datetime
    ) -> None:
        # New word
        await store.add(make_entry(word="new", next_review=base_time))

        # Learning word (interval between 1-20)
        learning = make_entry(word="learning")
        learning = learning.model_copy(
            update={"interval": 6, "total_reviews": 2, "correct_reviews": 2}
        )
        await store.add(learning)

        # Learned word (interval >= 21)
        learned = make_entry(word="learned")
        learned = learned.model_copy(
            update={"interval": 30, "total_reviews": 10, "correct_reviews": 8}
        )
        await store.add(learned)

        progress = await store.get_progress("user-1")

        assert progress.total_words == 3
        assert progress.words_new == 1
        assert progress.words_learning == 1
        assert progress.words_learned == 1
        assert progress.total_reviews == 12
        assert progress.correct_reviews == 10
        assert progress.accuracy_percent == pytest.approx(83.3, 0.1)


class TestVocabularyStoreUtilities:
    """Tests for utility methods."""

    @pytest.mark.asyncio
    async def test_count(self, store: VocabularyStore) -> None:
        assert await store.count("user-1") == 0

        await store.add(make_entry(word="σπίτι"))
        await store.add(make_entry(word="νερό"))

        assert await store.count("user-1") == 2

    @pytest.mark.asyncio
    async def test_exists(self, store: VocabularyStore) -> None:
        entry = make_entry()
        await store.add(entry)

        assert await store.exists("user-1", entry.id) is True
        assert await store.exists("user-1", "nonexistent") is False

    @pytest.mark.asyncio
    async def test_clear_user_vocabulary(self, store: VocabularyStore) -> None:
        await store.add(make_entry(word="σπίτι"))
        await store.add(make_entry(word="νερό"))

        count = await store.clear_user_vocabulary("user-1")
        assert count == 2
        assert await store.count("user-1") == 0

    @pytest.mark.asyncio
    async def test_user_isolation(self, store: VocabularyStore) -> None:
        entry1 = make_entry(user_id="user-1", word="σπίτι")
        entry2 = make_entry(user_id="user-2", word="νερό")
        await store.add(entry1)
        await store.add(entry2)

        user1_words = await store.list_entries("user-1")
        user2_words = await store.list_entries("user-2")

        assert len(user1_words) == 1
        assert len(user2_words) == 1
        assert user1_words[0].word == "σπίτι"
        assert user2_words[0].word == "νερό"
