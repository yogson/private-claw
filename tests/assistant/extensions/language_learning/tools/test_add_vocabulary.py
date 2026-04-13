"""Tests for add_vocabulary tool."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from assistant.agent.tools.deps import TurnDeps
from assistant.extensions.language_learning.models import (
    LearningStatus,
    PartOfSpeech,
)
from assistant.extensions.language_learning.store import VocabularyStore
from assistant.extensions.language_learning.tools.add_vocabulary import (
    VerbFormsInput,
    WordInput,
    add_vocabulary,
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


class TestAddVocabulary:
    @pytest.mark.asyncio
    async def test_add_single_noun(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        words = [
            WordInput(
                word="σπίτι",
                transliteration="spíti",
                translation="дом",
                part_of_speech=PartOfSpeech.NOUN,
                article="το",
                gender="n",
            )
        ]
        result = await add_vocabulary(ctx, words)
        assert result["status"] == "ok"
        assert "σπίτι" in result["added"]
        assert len(result["skipped_duplicates"]) == 0

    @pytest.mark.asyncio
    async def test_add_verb(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        words = [
            WordInput(
                word="γράφω",
                transliteration="gráfo",
                translation="писать",
                part_of_speech=PartOfSpeech.VERB,
                verb_forms=VerbFormsInput(
                    present="γράφω",
                    present_tr="gráfo",
                    aorist="έγραψα",
                    aorist_tr="égrapsa",
                    future="θα γράψω",
                    future_tr="tha grápso",
                ),
            )
        ]
        result = await add_vocabulary(ctx, words)
        assert result["status"] == "ok"
        assert "γράφω" in result["added"]

    @pytest.mark.asyncio
    async def test_skip_duplicate(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        word = WordInput(
            word="σπίτι",
            transliteration="spíti",
            translation="дом",
            part_of_speech=PartOfSpeech.NOUN,
        )
        # Add first time
        await add_vocabulary(ctx, [word])
        # Add again (should be skipped)
        result = await add_vocabulary(ctx, [word])
        assert result["status"] == "ok"
        assert "σπίτι" in result["skipped_duplicates"]
        assert len(result["added"]) == 0

    @pytest.mark.asyncio
    async def test_batch_add(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        words = [
            WordInput(
                word=f"word{i}",
                transliteration=f"word{i}",
                translation=f"слово{i}",
                part_of_speech=PartOfSpeech.OTHER,
            )
            for i in range(5)
        ]
        result = await add_vocabulary(ctx, words)
        assert result["status"] == "ok"
        assert len(result["added"]) == 5

    @pytest.mark.asyncio
    async def test_max_words_truncated(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        words = [
            WordInput(
                word=f"word{i}",
                transliteration=f"word{i}",
                translation=f"слово{i}",
                part_of_speech=PartOfSpeech.OTHER,
            )
            for i in range(15)
        ]
        result = await add_vocabulary(ctx, words)
        assert result["status"] == "ok"
        assert len(result["added"]) == 10  # Max 10
        assert result["truncated"] == 5
        assert "5 word(s) were over the limit" in result["summary"]

    @pytest.mark.asyncio
    async def test_noun_without_gender_article_has_warning(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        words = [
            WordInput(
                word="νερό",
                transliteration="neró",
                translation="вода",
                part_of_speech=PartOfSpeech.NOUN,
                # No gender or article
            )
        ]
        result = await add_vocabulary(ctx, words)
        assert result["status"] == "ok"
        assert "νερό" in result["added"]
        assert any("νερό" in w and "noun without gender/article" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_verb_without_verb_forms_has_warning_and_error(
        self, store: VocabularyStore
    ) -> None:
        ctx = _make_ctx(store)
        words = [
            WordInput(
                word="τρέχω",
                transliteration="trécho",
                translation="бежать",
                part_of_speech=PartOfSpeech.VERB,
                # No verb_forms — warning is emitted, but the model also rejects the entry
            )
        ]
        result = await add_vocabulary(ctx, words)
        assert result["status"] == "ok"
        # The model enforces verb_forms for verbs, so the entry fails to create
        assert "τρέχω" not in result["added"]
        assert any("τρέχω" in w and "verb without verb_forms" in w for w in result["warnings"])
        assert len(result["errors"]) == 1

    @pytest.mark.asyncio
    async def test_unavailable_without_store(self) -> None:
        ctx = MagicMock()
        ctx.deps = TurnDeps(writes_approved=[], seen_intent_ids=set())
        result = await add_vocabulary(ctx, [])
        assert result["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_new_words_have_new_status(self, store: VocabularyStore) -> None:
        ctx = _make_ctx(store)
        words = [
            WordInput(
                word="νερό",
                transliteration="neró",
                translation="вода",
                part_of_speech=PartOfSpeech.NOUN,
            )
        ]
        await add_vocabulary(ctx, words)
        entries = await store.list_entries("user-1")
        assert len(entries) == 1
        assert entries[0].learning_status == LearningStatus.NEW
