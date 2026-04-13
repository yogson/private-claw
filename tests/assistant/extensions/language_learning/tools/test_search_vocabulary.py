"""Tests for search_vocabulary tool."""

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
from assistant.extensions.language_learning.tools.search_vocabulary import search_vocabulary


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


def _make_entry(
    word: str = "σπίτι",
    translation: str = "дом",
    tags: list[str] | None = None,
    status: LearningStatus = LearningStatus.NEW,
) -> VocabularyEntry:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return VocabularyEntry(
        user_id="user-1",
        word=word,
        transliteration=f"trans_{word}",
        translation=translation,
        part_of_speech=PartOfSpeech.NOUN,
        article="το",
        tags=tags or [],
        learning_status=status,
        next_review=now,
        reverse_next_review=now,
        created_at=now,
        updated_at=now,
    )


class TestSearchVocabulary:
    @pytest.mark.asyncio
    async def test_search_empty_store(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        result = await search_vocabulary(ctx)
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_search_by_query(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("σπίτι", "дом"))
        await store.add(_make_entry("νερό", "вода"))
        ctx = _make_ctx(store)
        result = await search_vocabulary(ctx, query="σπίτι")
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["entries"][0]["word"] == "σπίτι"

    @pytest.mark.asyncio
    async def test_filter_by_status(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("σπίτι", status=LearningStatus.NEW))
        await store.add(_make_entry("νερό", status=LearningStatus.LEARNING))
        ctx = _make_ctx(store)
        result = await search_vocabulary(ctx, status="new")
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["entries"][0]["status"] == "new"

    @pytest.mark.asyncio
    async def test_filter_invalid_status(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        result = await search_vocabulary(ctx, status="invalid_status")
        assert result["status"] == "rejected_invalid"

    @pytest.mark.asyncio
    async def test_filter_by_tags(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("σπίτι", tags=["home"]))
        await store.add(_make_entry("νερό", tags=["food"]))
        ctx = _make_ctx(store)
        result = await search_vocabulary(ctx, tags=["home"])
        assert result["status"] == "ok"
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_limit_respected(self, store: VocabularyStore) -> None:
        for i in range(10):
            await store.add(_make_entry(f"word{i}"))
        ctx = _make_ctx(store)
        result = await search_vocabulary(ctx, limit=3)
        assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_unavailable_without_store(self) -> None:
        ctx = MagicMock()
        ctx.deps = TurnDeps(writes_approved=[], seen_intent_ids=set())
        result = await search_vocabulary(ctx)
        assert result["status"] == "unavailable"
