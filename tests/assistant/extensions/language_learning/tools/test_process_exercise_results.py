"""Tests for process_exercise_results tool."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.agent.tools.deps import TurnDeps
from assistant.extensions.language_learning.models import (
    LearningStatus,
    PartOfSpeech,
    VocabularyEntry,
)
from assistant.extensions.language_learning.store import VocabularyStore
from assistant.extensions.language_learning.tools.process_exercise_results import (
    process_exercise_results,
)


@pytest.fixture
def vocabulary_dir(tmp_path: Path) -> Path:
    return tmp_path / "vocabulary"


@pytest.fixture
def store(vocabulary_dir: Path) -> VocabularyStore:
    return VocabularyStore(vocabulary_dir)


def _make_ctx(store: VocabularyStore | None, user_id: str = "user-1") -> MagicMock:
    ctx = MagicMock()
    ctx.deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        user_id=user_id,
        vocabulary_store=store,
    )
    return ctx


def _make_new_entry(word: str, user_id: str = "user-1") -> VocabularyEntry:
    now = datetime.now(UTC)
    return VocabularyEntry(
        user_id=user_id,
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


def _make_valid_payload(word_ids: list[str]) -> str:
    results = [
        {"word_id": wid, "rating": 3, "time_ms": 1200, "direction": "forward"} for wid in word_ids
    ]
    return json.dumps({"type": "exercise_results", "results": results})


class TestProcessExerciseResults:
    @pytest.mark.asyncio
    async def test_valid_payload_updates_words(self, store: VocabularyStore) -> None:
        """Valid payload with known word IDs updates and returns ok status."""
        entry = _make_new_entry("σπίτι")
        await store.add(entry)
        # Fetch the saved entry to get the real UUID
        all_entries = await store.list_entries("user-1")
        word_id = all_entries[0].id

        payload = _make_valid_payload([str(word_id)])
        ctx = _make_ctx(store)
        result = await process_exercise_results(ctx, payload)

        assert result["status"] == "ok"
        assert result["updated_count"] == 1
        assert result["not_found_count"] == 0
        assert "summary" in result
        assert "status_breakdown" in result
        assert "direction_summary" in result

    @pytest.mark.asyncio
    async def test_valid_payload_counts_not_found(self, store: VocabularyStore) -> None:
        """Word IDs that do not exist are counted as not_found."""
        payload = _make_valid_payload(["00000000-0000-0000-0000-000000000000"])
        ctx = _make_ctx(store)
        result = await process_exercise_results(ctx, payload)

        assert result["status"] == "ok"
        assert result["updated_count"] == 0
        assert result["not_found_count"] == 1

    @pytest.mark.asyncio
    async def test_invalid_json_returns_rejected(self, store: VocabularyStore) -> None:
        """Non-JSON input returns rejected_invalid."""
        ctx = _make_ctx(store)
        result = await process_exercise_results(ctx, "not-valid-json{{")
        assert result["status"] == "rejected_invalid"
        assert "Invalid JSON" in result["reason"]

    @pytest.mark.asyncio
    async def test_schema_mismatch_returns_rejected(self, store: VocabularyStore) -> None:
        """Valid JSON that does not match ExerciseResultPayload schema is rejected."""
        ctx = _make_ctx(store)
        bad_payload = json.dumps({"type": "exercise_results", "results": "not-a-list"})
        result = await process_exercise_results(ctx, bad_payload)
        assert result["status"] == "rejected_invalid"
        assert "schema" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_empty_results_list_returns_rejected(self, store: VocabularyStore) -> None:
        """Payload with an empty results list is rejected."""
        ctx = _make_ctx(store)
        payload = json.dumps({"type": "exercise_results", "results": []})
        result = await process_exercise_results(ctx, payload)
        assert result["status"] == "rejected_invalid"

    @pytest.mark.asyncio
    async def test_store_exception_returns_error(self) -> None:
        """When the store raises, the tool returns error status."""
        mock_store = MagicMock()
        mock_store.process_exercise_results = AsyncMock(side_effect=RuntimeError("disk full"))

        ctx = MagicMock()
        ctx.deps = TurnDeps(
            writes_approved=[],
            seen_intent_ids=set(),
            user_id="user-1",
            vocabulary_store=mock_store,
        )
        payload = _make_valid_payload(["00000000-0000-0000-0000-000000000001"])
        result = await process_exercise_results(ctx, payload)
        assert result["status"] == "error"
        assert "disk full" in result["reason"]

    @pytest.mark.asyncio
    async def test_unavailable_without_store(self) -> None:
        """Returns unavailable when vocabulary_store is None."""
        ctx = MagicMock()
        ctx.deps = TurnDeps(writes_approved=[], seen_intent_ids=set())
        result = await process_exercise_results(ctx, "{}")
        assert result["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_unavailable_without_user_id(self, store: VocabularyStore) -> None:
        """Returns unavailable when user_id is None."""
        ctx = MagicMock()
        ctx.deps = TurnDeps(
            writes_approved=[],
            seen_intent_ids=set(),
            user_id=None,
            vocabulary_store=store,
        )
        result = await process_exercise_results(ctx, "{}")
        assert result["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_direction_summary_populated(self, store: VocabularyStore) -> None:
        """direction_summary contains a count per direction used in results."""
        entry = _make_new_entry("νερό")
        await store.add(entry)
        all_entries = await store.list_entries("user-1")
        word_id = str(all_entries[0].id)

        payload = json.dumps(
            {
                "type": "exercise_results",
                "results": [
                    {"word_id": word_id, "rating": 2, "direction": "forward"},
                ],
            }
        )
        ctx = _make_ctx(store)
        result = await process_exercise_results(ctx, payload)
        assert result["status"] == "ok"
        assert result["direction_summary"].get("forward", 0) >= 1
