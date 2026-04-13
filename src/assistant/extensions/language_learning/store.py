"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

JSON file-based vocabulary store with SM-2 spaced repetition support.

Storage pattern: one JSON file per user containing all vocabulary entries.
File location: data/vocabulary/{user_id}.json
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assistant.extensions.language_learning.models import (
    CardDirection,
    CardResult,
    VocabularyEntry,
    VocabularyProgress,
)
from assistant.extensions.language_learning.sm2 import SM2Engine
from assistant.store.filesystem.atomic import atomic_write_text, ensure_directory


class VocabularyStore:
    """JSON file-based vocabulary store with SM-2 spaced repetition."""

    def __init__(self, vocabulary_dir: Path) -> None:
        self._vocabulary_dir = vocabulary_dir
        self._locks: dict[str, asyncio.Lock] = {}
        ensure_directory(self._vocabulary_dir)

    def _user_file_path(self, user_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return self._vocabulary_dir / f"{safe_id}.json"

    def _get_lock(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    def _serialize_entry(self, entry: VocabularyEntry) -> dict[str, Any]:
        data = entry.model_dump()
        # Convert datetime fields to ISO format
        for key in [
            "next_review",
            "reverse_next_review",
            "created_at",
            "updated_at",
        ]:
            if data[key] is not None:
                data[key] = data[key].isoformat()
        return data

    def _deserialize_entry(self, data: dict[str, Any]) -> VocabularyEntry | None:
        try:
            # Convert ISO strings back to datetime
            for key in [
                "next_review",
                "reverse_next_review",
                "created_at",
                "updated_at",
            ]:
                if data.get(key) is not None and isinstance(data[key], str):
                    data[key] = datetime.fromisoformat(data[key])
            return VocabularyEntry.model_validate(data)
        except (ValueError, KeyError):
            return None

    async def _read_user_vocabulary(self, user_id: str) -> dict[str, VocabularyEntry]:
        path = self._user_file_path(user_id)
        if not path.exists():
            return {}

        try:
            content = path.read_text(encoding="utf-8")
            data = json.loads(content)
            entries: dict[str, VocabularyEntry] = {}
            for entry_data in data.get("entries", []):
                entry = self._deserialize_entry(entry_data)
                if entry is not None:
                    entries[entry.id] = entry
            return entries
        except (json.JSONDecodeError, KeyError):
            return {}

    async def _write_user_vocabulary(
        self, user_id: str, entries: dict[str, VocabularyEntry]
    ) -> None:
        path = self._user_file_path(user_id)
        data = {
            "user_id": user_id,
            "updated_at": datetime.now(UTC).isoformat(),
            "entries": [self._serialize_entry(e) for e in entries.values()],
        }
        content = json.dumps(data, ensure_ascii=False, indent=2)
        await atomic_write_text(path, content)

    async def add(self, entry: VocabularyEntry) -> VocabularyEntry:
        """Add a new vocabulary entry."""
        lock = self._get_lock(entry.user_id)
        async with lock:
            entries = await self._read_user_vocabulary(entry.user_id)
            if entry.id in entries:
                raise ValueError(f"Entry already exists: {entry.id}")
            entries[entry.id] = entry
            await self._write_user_vocabulary(entry.user_id, entries)
            return entry

    async def get(self, user_id: str, word_id: str) -> VocabularyEntry | None:
        """Get a vocabulary entry by ID."""
        entries = await self._read_user_vocabulary(user_id)
        return entries.get(word_id)

    async def update(self, entry: VocabularyEntry) -> VocabularyEntry:
        """Update an existing vocabulary entry."""
        lock = self._get_lock(entry.user_id)
        async with lock:
            entries = await self._read_user_vocabulary(entry.user_id)
            if entry.id not in entries:
                raise ValueError(f"Entry not found: {entry.id}")
            updated_entry = entry.model_copy(update={"updated_at": datetime.now(UTC)})
            entries[entry.id] = updated_entry
            await self._write_user_vocabulary(entry.user_id, entries)
            return updated_entry

    async def delete(self, user_id: str, word_id: str) -> bool:
        """Delete a vocabulary entry. Returns True if deleted, False if not found."""
        lock = self._get_lock(user_id)
        async with lock:
            entries = await self._read_user_vocabulary(user_id)
            if word_id not in entries:
                return False
            del entries[word_id]
            await self._write_user_vocabulary(user_id, entries)
            return True

    async def list_entries(
        self,
        user_id: str,
        tags: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[VocabularyEntry]:
        """List vocabulary entries with optional tag filtering."""
        entries = await self._read_user_vocabulary(user_id)

        result = [e for e in entries.values()]

        # Filter by tags if specified
        if tags:
            tag_set = set(tags)
            result = [e for e in result if tag_set.intersection(e.tags)]

        # Sort by created_at descending (newest first)
        result.sort(key=lambda e: e.created_at, reverse=True)

        # Apply pagination
        if offset > 0:
            result = result[offset:]
        if limit is not None and limit > 0:
            result = result[:limit]

        return result

    async def search(
        self,
        user_id: str,
        query: str,
        limit: int = 20,
    ) -> list[VocabularyEntry]:
        """Search vocabulary entries by word, translation, or transliteration."""
        entries = await self._read_user_vocabulary(user_id)
        query_lower = query.lower()

        matches: list[VocabularyEntry] = []
        for entry in entries.values():
            if (
                query_lower in entry.word.lower()
                or query_lower in entry.translation.lower()
                or query_lower in entry.transliteration.lower()
            ):
                matches.append(entry)

        # Sort by relevance (exact matches first, then by created_at)
        def relevance_key(e: VocabularyEntry) -> tuple[int, datetime]:
            exact = (
                e.word.lower() == query_lower
                or e.translation.lower() == query_lower
                or e.transliteration.lower() == query_lower
            )
            return (0 if exact else 1, e.created_at)

        matches.sort(key=relevance_key)

        return matches[:limit]

    async def get_due_words(
        self,
        user_id: str,
        limit: int = 20,
        tags: list[str] | None = None,
        direction: CardDirection = CardDirection.FORWARD,
        as_of: datetime | None = None,
    ) -> list[VocabularyEntry]:
        """Get words due for review."""
        entries = await self._read_user_vocabulary(user_id)
        check_time = as_of or datetime.now(UTC)

        due: list[VocabularyEntry] = []
        for entry in entries.values():
            # Filter by tags if specified
            if tags:
                tag_set = set(tags)
                if not tag_set.intersection(entry.tags):
                    continue

            # Check if due for the specified direction
            if entry.is_due(direction, check_time):
                due.append(entry)

        # Sort by next_review (oldest first) to prioritize overdue words
        if direction == CardDirection.FORWARD:
            due.sort(key=lambda e: e.next_review)
        else:
            due.sort(key=lambda e: e.reverse_next_review)

        return due[:limit]

    async def update_after_review(
        self,
        user_id: str,
        word_id: str,
        rating: int,
        direction: CardDirection = CardDirection.FORWARD,
        review_time: datetime | None = None,
    ) -> VocabularyEntry | None:
        """Update a word's SM-2 parameters after review."""
        lock = self._get_lock(user_id)
        async with lock:
            entries = await self._read_user_vocabulary(user_id)
            entry = entries.get(word_id)
            if entry is None:
                return None

            # Apply SM-2 update
            updated_entry = SM2Engine.update_entry(entry, rating, direction, review_time)
            entries[word_id] = updated_entry
            await self._write_user_vocabulary(user_id, entries)
            return updated_entry

    async def process_exercise_results(
        self,
        user_id: str,
        results: list[CardResult],
        review_time: datetime | None = None,
    ) -> dict[str, VocabularyEntry | None]:
        """Process multiple card results from an exercise session."""
        lock = self._get_lock(user_id)
        now = review_time or datetime.now(UTC)

        async with lock:
            entries = await self._read_user_vocabulary(user_id)
            updated: dict[str, VocabularyEntry | None] = {}

            for result in results:
                entry = entries.get(result.word_id)
                if entry is None:
                    updated[result.word_id] = None
                    continue

                # Apply SM-2 update
                updated_entry = SM2Engine.update_entry(entry, result.rating, result.direction, now)
                entries[result.word_id] = updated_entry
                updated[result.word_id] = updated_entry

            await self._write_user_vocabulary(user_id, entries)
            return updated

    async def get_progress(
        self,
        user_id: str,
        direction: CardDirection = CardDirection.FORWARD,
    ) -> VocabularyProgress:
        """Get learning progress statistics for a user."""
        entries = await self._read_user_vocabulary(user_id)
        now = datetime.now(UTC)

        total = len(entries)
        learned = 0
        learning = 0
        new = 0
        total_reviews = 0
        correct_reviews = 0
        due_today = 0
        due_today_reverse = 0
        last_review: datetime | None = None

        for entry in entries.values():
            # Count by learning stage
            if SM2Engine.is_learned(entry, direction):
                learned += 1
            elif SM2Engine.is_learning(entry, direction):
                learning += 1
            else:
                new += 1

            # Sum review stats
            if direction == CardDirection.FORWARD:
                total_reviews += entry.total_reviews
                correct_reviews += entry.correct_reviews
            else:
                total_reviews += entry.reverse_total_reviews
                correct_reviews += entry.reverse_correct_reviews

            # Count due words
            if entry.is_due(CardDirection.FORWARD, now):
                due_today += 1
            if entry.is_due(CardDirection.REVERSE, now):
                due_today_reverse += 1

            # Track last review
            if last_review is None or entry.updated_at > last_review:
                last_review = entry.updated_at

        accuracy = (correct_reviews / total_reviews * 100) if total_reviews > 0 else 0.0

        return VocabularyProgress(
            user_id=user_id,
            total_words=total,
            words_learned=learned,
            words_learning=learning,
            words_new=new,
            total_reviews=total_reviews,
            correct_reviews=correct_reviews,
            accuracy_percent=round(accuracy, 1),
            due_today=due_today,
            due_today_reverse=due_today_reverse,
            streak_days=0,  # TODO: Calculate from review history
            last_review_date=last_review,
        )

    async def count(self, user_id: str) -> int:
        """Get total word count for a user."""
        entries = await self._read_user_vocabulary(user_id)
        return len(entries)

    async def exists(self, user_id: str, word_id: str) -> bool:
        """Check if a vocabulary entry exists."""
        entries = await self._read_user_vocabulary(user_id)
        return word_id in entries

    async def find_by_word(
        self,
        user_id: str,
        word: str,
    ) -> VocabularyEntry | None:
        """Find an entry by exact word match."""
        entries = await self._read_user_vocabulary(user_id)
        word_lower = word.lower()
        for entry in entries.values():
            if entry.word.lower() == word_lower:
                return entry
        return None

    async def clear_user_vocabulary(self, user_id: str) -> int:
        """Delete all vocabulary for a user. Returns count of deleted entries."""
        lock = self._get_lock(user_id)
        async with lock:
            entries = await self._read_user_vocabulary(user_id)
            count = len(entries)
            path = self._user_file_path(user_id)
            if path.exists():
                path.unlink()
            return count
