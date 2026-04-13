"""Tests for set_word_status tool."""

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
from assistant.extensions.language_learning.tools.set_word_status import (
    StatusUpdate,
    set_word_status,
)


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


def _make_entry(word: str = "σπίτι") -> VocabularyEntry:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return VocabularyEntry(
        user_id="user-1",
        word=word,
        transliteration=f"trans_{word}",
        translation="дом",
        part_of_speech=PartOfSpeech.NOUN,
        learning_status=LearningStatus.LEARNING,
        next_review=now,
        reverse_next_review=now,
        created_at=now,
        updated_at=now,
    )


class TestSetWordStatus:
    @pytest.mark.asyncio
    async def test_suspend_word(self, store: VocabularyStore) -> None:
        entry = _make_entry("σπίτι")
        await store.add(entry)
        ctx = _make_ctx(store)
        result = await set_word_status(ctx, [StatusUpdate(word_id=entry.id, status="suspended")])
        assert result["status"] == "ok"
        assert len(result["updated"]) == 1
        updated = await store.get("user-1", entry.id)
        assert updated is not None
        assert updated.learning_status == LearningStatus.SUSPENDED

    @pytest.mark.asyncio
    async def test_set_learning_status(self, store: VocabularyStore) -> None:
        entry = _make_entry("σπίτι")
        entry = entry.model_copy(update={"learning_status": LearningStatus.SUSPENDED})
        await store.add(entry)
        ctx = _make_ctx(store)
        result = await set_word_status(ctx, [StatusUpdate(word_id=entry.id, status="learning")])
        assert result["status"] == "ok"
        updated = await store.get("user-1", entry.id)
        assert updated is not None
        assert updated.learning_status == LearningStatus.LEARNING

    @pytest.mark.asyncio
    async def test_reject_invalid_status(self, store: VocabularyStore) -> None:
        entry = _make_entry("σπίτι")
        await store.add(entry)
        ctx = _make_ctx(store)
        result = await set_word_status(ctx, [StatusUpdate(word_id=entry.id, status="new")])
        assert result["status"] == "ok"
        assert len(result["errors"]) == 1
        assert "new" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_word_not_found(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        result = await set_word_status(
            ctx, [StatusUpdate(word_id="nonexistent", status="suspended")]
        )
        assert result["status"] == "ok"
        assert len(result["errors"]) == 1
        assert "not found" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_unavailable_without_store(self) -> None:
        ctx = MagicMock()
        ctx.deps = TurnDeps(writes_approved=[], seen_intent_ids=set())
        result = await set_word_status(ctx, [])
        assert result["status"] == "unavailable"
