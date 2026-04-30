"""Tests for get_words tool."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from assistant.agent.tools.deps import TurnDeps
from assistant.extensions.language_learning.models import (
    LearningStatus,
    PartOfSpeech,
    VerbForms,
    VocabularyEntry,
)
from assistant.extensions.language_learning.store import VocabularyStore
from assistant.extensions.language_learning.tools.get_words import get_words


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
    word: str,
    pos: PartOfSpeech = PartOfSpeech.NOUN,
    tags: list[str] | None = None,
    stability: float | None = None,
    created_offset_seconds: int = 0,
) -> VocabularyEntry:
    now = datetime.now(UTC)
    from datetime import timedelta

    created_at = now - timedelta(seconds=created_offset_seconds)
    fsrs_card = None
    if stability is not None:
        fsrs_card = {"stability": stability, "difficulty": 5.0, "state": 0}
    verb_forms = None
    if pos == PartOfSpeech.VERB:
        verb_forms = VerbForms(
            present="γράφω",
            present_tr="gráfo",
            aorist="έγραψα",
            aorist_tr="égrapsa",
            future="θα γράψω",
            future_tr="tha grápso",
        )
    return VocabularyEntry(
        user_id="user-1",
        word=word,
        transliteration=f"trans_{word}",
        translation="тест",
        part_of_speech=pos,
        tags=tags or [],
        learning_status=LearningStatus.NEW,
        fsrs_card=fsrs_card,
        next_review=now,
        reverse_next_review=now,
        created_at=created_at,
        updated_at=now,
        verb_forms=verb_forms,
    )


class TestGetWordsFiltering:
    @pytest.mark.asyncio
    async def test_empty_store_returns_empty(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        result = await get_words(ctx)
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["words"] == []

    @pytest.mark.asyncio
    async def test_returns_all_words_up_to_limit(self, store: VocabularyStore) -> None:
        for i in range(8):
            await store.add(_make_entry(f"word{i}"))
        ctx = _make_ctx(store)
        result = await get_words(ctx, limit=5)
        assert result["status"] == "ok"
        assert result["count"] == 5

    @pytest.mark.asyncio
    async def test_filter_by_tags(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("food1", tags=["food"]))
        await store.add(_make_entry("food2", tags=["food", "travel"]))
        await store.add(_make_entry("other", tags=["nature"]))
        ctx = _make_ctx(store)
        result = await get_words(ctx, tags=["food"])
        assert result["status"] == "ok"
        assert result["count"] == 2
        words = {w["word"] for w in result["words"]}
        assert words == {"food1", "food2"}

    @pytest.mark.asyncio
    async def test_filter_by_pos_noun(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("noun1", pos=PartOfSpeech.NOUN))
        await store.add(_make_entry("noun2", pos=PartOfSpeech.NOUN))
        await store.add(_make_entry("γράφω", pos=PartOfSpeech.VERB))
        ctx = _make_ctx(store)
        result = await get_words(ctx, pos=["noun"])
        assert result["status"] == "ok"
        assert result["count"] == 2
        assert all(w["part_of_speech"] == "noun" for w in result["words"])

    @pytest.mark.asyncio
    async def test_filter_by_pos_verb(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("noun1", pos=PartOfSpeech.NOUN))
        await store.add(_make_entry("γράφω", pos=PartOfSpeech.VERB))
        ctx = _make_ctx(store)
        result = await get_words(ctx, pos=["verb"])
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["words"][0]["part_of_speech"] == "verb"

    @pytest.mark.asyncio
    async def test_filter_by_min_stability(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("easy", stability=10.0))
        await store.add(_make_entry("hard", stability=2.0))
        await store.add(_make_entry("new_word"))  # no stability
        ctx = _make_ctx(store)
        result = await get_words(ctx, min_ease_factor=5.0)
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["words"][0]["word"] == "easy"

    @pytest.mark.asyncio
    async def test_filter_by_max_stability(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("easy", stability=10.0))
        await store.add(_make_entry("hard", stability=2.0))
        await store.add(_make_entry("new_word"))  # stability=0.0
        ctx = _make_ctx(store)
        result = await get_words(ctx, max_ease_factor=3.0)
        assert result["status"] == "ok"
        assert result["count"] == 2  # hard + new_word (stability=0)

    @pytest.mark.asyncio
    async def test_combined_tag_and_pos_filter(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("food_noun", pos=PartOfSpeech.NOUN, tags=["food"]))
        await store.add(_make_entry("γράφω", pos=PartOfSpeech.VERB, tags=["food"]))
        await store.add(_make_entry("other_noun", pos=PartOfSpeech.NOUN, tags=["nature"]))
        ctx = _make_ctx(store)
        result = await get_words(ctx, tags=["food"], pos=["noun"])
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["words"][0]["word"] == "food_noun"


class TestGetWordsOrdering:
    @pytest.mark.asyncio
    async def test_order_newest(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("oldest", created_offset_seconds=3600))
        await store.add(_make_entry("middle", created_offset_seconds=1800))
        await store.add(_make_entry("newest", created_offset_seconds=0))
        ctx = _make_ctx(store)
        result = await get_words(ctx, order_by="newest", limit=3)
        assert result["status"] == "ok"
        assert result["words"][0]["word"] == "newest"
        assert result["words"][-1]["word"] == "oldest"

    @pytest.mark.asyncio
    async def test_order_oldest(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("oldest", created_offset_seconds=3600))
        await store.add(_make_entry("middle", created_offset_seconds=1800))
        await store.add(_make_entry("newest", created_offset_seconds=0))
        ctx = _make_ctx(store)
        result = await get_words(ctx, order_by="oldest", limit=3)
        assert result["status"] == "ok"
        assert result["words"][0]["word"] == "oldest"
        assert result["words"][-1]["word"] == "newest"

    @pytest.mark.asyncio
    async def test_order_least_learned(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("easy", stability=15.0))
        await store.add(_make_entry("hard", stability=1.5))
        await store.add(_make_entry("new_word"))  # no stability = 0.0
        ctx = _make_ctx(store)
        result = await get_words(ctx, order_by="least_learned", limit=3)
        assert result["status"] == "ok"
        assert result["words"][0]["word"] == "new_word"
        assert result["words"][-1]["word"] == "easy"

    @pytest.mark.asyncio
    async def test_order_most_learned(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("easy", stability=15.0))
        await store.add(_make_entry("hard", stability=1.5))
        await store.add(_make_entry("new_word"))  # no stability = 0.0
        ctx = _make_ctx(store)
        result = await get_words(ctx, order_by="most_learned", limit=3)
        assert result["status"] == "ok"
        assert result["words"][0]["word"] == "easy"
        assert result["words"][-1]["word"] == "new_word"

    @pytest.mark.asyncio
    async def test_order_random_returns_correct_count(self, store: VocabularyStore) -> None:
        for i in range(10):
            await store.add(_make_entry(f"word{i}"))
        ctx = _make_ctx(store)
        result = await get_words(ctx, order_by="random", limit=5)
        assert result["status"] == "ok"
        assert result["count"] == 5


class TestGetWordsResponse:
    @pytest.mark.asyncio
    async def test_response_includes_word_fields(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("σπίτι", tags=["home"], stability=3.0))
        ctx = _make_ctx(store)
        result = await get_words(ctx, limit=1)
        assert result["status"] == "ok"
        word = result["words"][0]
        assert word["word"] == "σπίτι"
        assert "translation" in word
        assert "transliteration" in word
        assert "part_of_speech" in word
        assert "tags" in word
        assert "learning_status" in word
        assert "id" in word

    @pytest.mark.asyncio
    async def test_verb_includes_verb_forms(self, store: VocabularyStore) -> None:
        await store.add(_make_entry("γράφω", pos=PartOfSpeech.VERB))
        ctx = _make_ctx(store)
        result = await get_words(ctx, pos=["verb"])
        assert result["status"] == "ok"
        word = result["words"][0]
        assert "verb_forms" in word
        assert "present" in word["verb_forms"]

    @pytest.mark.asyncio
    async def test_unavailable_without_store(self) -> None:
        ctx = MagicMock()
        ctx.deps = TurnDeps(writes_approved=[], seen_intent_ids=set())
        result = await get_words(ctx)
        assert result["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_limit_capped_at_max(self, store: VocabularyStore) -> None:
        for i in range(5):
            await store.add(_make_entry(f"word{i}"))
        ctx = _make_ctx(store)
        result = await get_words(ctx, limit=100)
        assert result["count"] == 5  # only 5 words exist
