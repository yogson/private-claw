"""Tests for start_exercise tool."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from assistant.agent.tools.deps import TurnDeps
from assistant.extensions.language_learning.models import (
    LearningStatus,
    PartOfSpeech,
    VocabularyEntry,
)
from assistant.extensions.language_learning.store import VocabularyStore
from assistant.extensions.language_learning.tools.start_exercise import start_exercise


@pytest.fixture
def vocabulary_dir(tmp_path: Path) -> Path:
    return tmp_path / "vocabulary"


@pytest.fixture
def store(vocabulary_dir: Path) -> VocabularyStore:
    return VocabularyStore(vocabulary_dir)


def _make_ctx(store: VocabularyStore, user_id: str = "user-1") -> MagicMock:
    ctx = MagicMock()
    ctx.deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        user_id=user_id,
        vocabulary_store=store,
    )
    return ctx


def _make_new_entry(word: str) -> VocabularyEntry:
    now = datetime.now(UTC)
    return VocabularyEntry(
        user_id="user-1",
        word=word,
        transliteration=f"trans_{word}",
        translation="тест",
        part_of_speech=PartOfSpeech.NOUN,
        learning_status=LearningStatus.NEW,
        next_review=now,
        reverse_next_review=now,
        created_at=now,
        updated_at=now,
    )


def _make_learning_entry(word: str, due: bool = True) -> VocabularyEntry:
    now = datetime.now(UTC)
    next_review = now - timedelta(hours=1) if due else now + timedelta(days=7)
    return VocabularyEntry(
        user_id="user-1",
        word=word,
        transliteration=f"trans_{word}",
        translation="тест",
        part_of_speech=PartOfSpeech.NOUN,
        learning_status=LearningStatus.LEARNING,
        interval=6,
        next_review=next_review,
        reverse_next_review=next_review,
        created_at=now,
        updated_at=now,
    )


def _make_suspended_entry(word: str) -> VocabularyEntry:
    now = datetime.now(UTC)
    return VocabularyEntry(
        user_id="user-1",
        word=word,
        transliteration=f"trans_{word}",
        translation="тест",
        part_of_speech=PartOfSpeech.NOUN,
        learning_status=LearningStatus.SUSPENDED,
        next_review=now,
        reverse_next_review=now,
        created_at=now,
        updated_at=now,
    )


class TestStartExercise:
    @pytest.fixture(autouse=True)
    def set_webapp_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOCABULARY_WEBAPP_URL", "https://test.example.com/exercise")

    @pytest.mark.asyncio
    async def test_no_words_due(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        result = await start_exercise(ctx)
        assert result["status"] == "no_words_due"

    @pytest.mark.asyncio
    async def test_exercise_with_new_words(self, store: VocabularyStore) -> None:
        await store.add(_make_new_entry("σπίτι"))
        await store.add(_make_new_entry("νερό"))
        ctx = _make_ctx(store)
        result = await start_exercise(ctx)
        assert result["status"] == "exercise_ready"
        assert result["word_count"] == 2
        assert result["breakdown"]["new"] == 2
        assert "webapp_url" in result
        assert "message" in result

    @pytest.mark.asyncio
    async def test_exercise_skips_suspended(self, store: VocabularyStore) -> None:
        await store.add(_make_new_entry("σπίτι"))
        await store.add(_make_suspended_entry("νερό"))
        ctx = _make_ctx(store)
        result = await start_exercise(ctx)
        assert result["status"] == "exercise_ready"
        assert result["word_count"] == 1
        assert result["breakdown"]["new"] == 1

    @pytest.mark.asyncio
    async def test_exercise_with_due_learning_words(self, store: VocabularyStore) -> None:
        await store.add(_make_learning_entry("σπίτι", due=True))
        await store.add(_make_learning_entry("νερό", due=False))
        ctx = _make_ctx(store)
        result = await start_exercise(ctx)
        assert result["status"] == "exercise_ready"
        assert result["word_count"] == 1
        assert result["breakdown"]["due"] == 1

    @pytest.mark.asyncio
    async def test_limit_respected(self, store: VocabularyStore) -> None:
        for i in range(10):
            await store.add(_make_new_entry(f"word{i}"))
        ctx = _make_ctx(store)
        result = await start_exercise(ctx, limit=5)
        assert result["word_count"] == 5

    @pytest.mark.asyncio
    async def test_invalid_direction(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        result = await start_exercise(ctx, direction="invalid")
        assert result["status"] == "rejected_invalid"

    @pytest.mark.asyncio
    async def test_webapp_url_contains_encoded_words(self, store: VocabularyStore) -> None:
        await store.add(_make_new_entry("σπίτι"))
        ctx = _make_ctx(store)
        result = await start_exercise(ctx)
        assert result["status"] == "exercise_ready"
        url = result["webapp_url"]
        assert "words=" in url
        assert "dir=forward" in url

    @pytest.mark.asyncio
    async def test_unavailable_without_store(self) -> None:
        ctx = MagicMock()
        ctx.deps = TurnDeps(writes_approved=[], seen_intent_ids=set())
        result = await start_exercise(ctx)
        assert result["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_error_when_webapp_url_not_configured(
        self, store: VocabularyStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VOCABULARY_WEBAPP_URL", raising=False)
        await store.add(_make_new_entry("σπίτι"))
        ctx = _make_ctx(store)
        result = await start_exercise(ctx)
        assert result["status"] == "error"
        assert "WebApp URL is not configured" in result["reason"]
