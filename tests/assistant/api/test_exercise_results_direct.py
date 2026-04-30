"""Tests for direct (LLM-bypassing) exercise results processing in orchestrator_handler."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.api.orchestrator_handler import _build_orchestrator_handler
from assistant.channels.telegram.models import EventSource, EventType, NormalizedEvent
from assistant.extensions.language_learning.models import (
    LearningStatus,
    PartOfSpeech,
    VocabularyEntry,
)
from assistant.extensions.language_learning.store import VocabularyStore


def _make_event(
    event_type: EventType = EventType.EXERCISE_RESULTS,
    text: str | None = None,
    user_id: str = "123456",
    session_id: str = "tg:123456",
    trace_id: str = "trace-1",
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id="evt-1",
        event_type=event_type,
        source=EventSource.TELEGRAM,
        session_id=session_id,
        user_id=user_id,
        created_at=datetime.now(UTC),
        trace_id=trace_id,
        text=text,
        metadata={"chat_id": 123456},
    )


def _valid_payload(word_ids: list[str]) -> str:
    results = [
        {"word_id": wid, "rating": 3, "time_ms": 1000, "direction": "forward"} for wid in word_ids
    ]
    return json.dumps({"type": "exercise_results", "results": results})


def _make_handler(vocabulary_store=None):
    adapter = MagicMock()
    for method in (
        "is_stop_request",
        "is_verbose_request",
        "is_session_new_request",
        "is_session_reset_request",
        "is_session_resume_request",
        "is_session_resume_callback",
        "is_model_request",
        "is_model_callback_request",
        "is_capabilities_request",
        "is_capabilities_callback_request",
        "is_memory_confirmation_callback",
        "is_task_callback",
        "is_usage_request",
    ):
        getattr(adapter, method).return_value = False

    orchestrator = MagicMock()
    orchestrator.execute_turn = AsyncMock()

    handler = _build_orchestrator_handler(
        adapter=adapter,
        orchestrator=orchestrator,
        vocabulary_store=vocabulary_store,
    )
    return handler, orchestrator


def _make_entry(user_id: str = "123456") -> VocabularyEntry:
    now = datetime.now(UTC)
    return VocabularyEntry(
        user_id=user_id,
        word="σπίτι",
        transliteration="spiti",
        translation="дом",
        part_of_speech=PartOfSpeech.NOUN,
        learning_status=LearningStatus.NEW,
        next_review=now,
        reverse_next_review=now,
        created_at=now,
        updated_at=now,
    )


class TestExerciseResultsDirectProcessing:
    @pytest.mark.asyncio
    async def test_valid_results_bypass_llm(self, tmp_path: Path) -> None:
        """EXERCISE_RESULTS event calls store directly; orchestrator is never invoked."""
        store = VocabularyStore(tmp_path / "vocab")
        handler, orchestrator = _make_handler(store)

        event = _make_event(text=_valid_payload([]))
        response = await handler(event)

        assert response is not None
        orchestrator.execute_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_response_includes_updated_count(self, tmp_path: Path) -> None:
        """Response text reports how many words were updated."""
        store = VocabularyStore(tmp_path / "vocab")
        entry = _make_entry()
        await store.add(entry)
        entries = await store.list_entries("123456")
        word_id = str(entries[0].id)

        handler, _ = _make_handler(store)
        event = _make_event(text=_valid_payload([word_id]))
        response = await handler(event)

        assert response is not None
        assert "1" in response.text

    @pytest.mark.asyncio
    async def test_zero_updated_when_word_not_found(self, tmp_path: Path) -> None:
        """Unknown word IDs count as not-found; response still includes a count."""
        store = VocabularyStore(tmp_path / "vocab")
        handler, _ = _make_handler(store)

        event = _make_event(text=_valid_payload(["00000000-0000-0000-0000-000000000000"]))
        response = await handler(event)

        assert response is not None
        assert "0" in response.text

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error_without_llm(self) -> None:
        """Invalid JSON in EXERCISE_RESULTS event returns error; LLM not invoked."""
        handler, orchestrator = _make_handler()
        event = _make_event(text="not-valid-json{{")
        response = await handler(event)

        assert response is not None
        assert response.text
        orchestrator.execute_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_schema_mismatch_returns_error_without_llm(self) -> None:
        """JSON with wrong structure returns error; LLM not invoked."""
        handler, orchestrator = _make_handler()
        event = _make_event(text=json.dumps({"type": "exercise_results", "results": "not-a-list"}))
        response = await handler(event)

        assert response is not None
        assert response.text
        orchestrator.execute_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_vocabulary_store_returns_error(self) -> None:
        """When vocabulary_store is None, return error without calling LLM."""
        handler, orchestrator = _make_handler(vocabulary_store=None)
        event = _make_event(text=_valid_payload(["00000000-0000-0000-0000-000000000001"]))
        response = await handler(event)

        assert response is not None
        assert response.text
        orchestrator.execute_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_exception_returns_error(self) -> None:
        """When store raises, return error response without calling LLM."""
        mock_store = MagicMock()
        mock_store.process_exercise_results = AsyncMock(side_effect=RuntimeError("disk full"))
        handler, orchestrator = _make_handler(mock_store)
        event = _make_event(text=_valid_payload(["00000000-0000-0000-0000-000000000001"]))
        response = await handler(event)

        assert response is not None
        assert response.text
        orchestrator.execute_turn.assert_not_called()
