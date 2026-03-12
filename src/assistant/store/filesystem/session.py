"""
Component ID: CMP_STORE_SESSION_PERSISTENCE

Filesystem-based session persistence using append-only JSONL files.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from assistant.store.filesystem.atomic import ensure_directory, file_append_lines
from assistant.store.filesystem.replay import build_replay
from assistant.store.interfaces import SessionStoreInterface
from assistant.store.models import SessionRecord


class FilesystemSessionStore(SessionStoreInterface):
    """Filesystem-based implementation of session persistence using JSONL."""

    def __init__(self, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        self._locks: dict[str, asyncio.Lock] = {}
        ensure_directory(self._sessions_dir)

    def _session_path(self, session_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        return self._sessions_dir / f"{safe_id}.jsonl"

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    def _serialize_record(self, record: SessionRecord) -> str:
        data = {
            "session_id": record.session_id,
            "sequence": record.sequence,
            "event_id": record.event_id,
            "turn_id": record.turn_id,
            "timestamp": record.timestamp.isoformat(),
            "record_type": record.record_type,
            "payload": record.payload,
        }
        return json.dumps(data, separators=(",", ":"))

    def _deserialize_record(self, line: str) -> SessionRecord | None:
        try:
            data = json.loads(line)
            return SessionRecord(
                session_id=data["session_id"],
                sequence=data["sequence"],
                event_id=data["event_id"],
                turn_id=data["turn_id"],
                timestamp=datetime.fromisoformat(data["timestamp"]),
                record_type=data["record_type"],
                payload=data["payload"],
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    async def _read_all_records(self, session_id: str) -> list[SessionRecord]:
        path = self._session_path(session_id)
        if not path.exists():
            return []

        records: list[SessionRecord] = []
        content = path.read_text()
        for line in content.strip().split("\n"):
            if not line.strip():
                continue
            record = self._deserialize_record(line)
            if record is not None:
                records.append(record)
        return sorted(records, key=lambda r: r.sequence)

    async def append(self, records: list[SessionRecord]) -> None:
        """
        Append records to a session log.

        Idempotency: Records with event_ids already present in the session
        are silently skipped (not duplicated).
        """
        if not records:
            return

        session_ids = {r.session_id for r in records}
        if len(session_ids) != 1:
            raise ValueError("All records must have the same session_id")

        session_id = records[0].session_id
        lock = self._get_lock(session_id)

        async with lock:
            existing = await self._read_all_records(session_id)
            existing_event_ids = {r.event_id for r in existing}
            next_seq = existing[-1].sequence + 1 if existing else 0

            new_records: list[SessionRecord] = []
            for record in records:
                if record.event_id in existing_event_ids:
                    continue
                new_record = SessionRecord(
                    session_id=record.session_id,
                    sequence=next_seq,
                    event_id=record.event_id,
                    turn_id=record.turn_id,
                    timestamp=record.timestamp,
                    record_type=record.record_type,
                    payload=record.payload,
                )
                new_records.append(new_record)
                existing_event_ids.add(record.event_id)
                next_seq += 1

            if not new_records:
                return

            path = self._session_path(session_id)
            lines = [self._serialize_record(r) for r in new_records]
            await file_append_lines(path, lines)

    async def append_raw(self, records: list[SessionRecord]) -> None:
        """
        Append pre-sequenced records directly (for recovery repairs).

        Unlike append(), this does not check idempotency or assign sequences.
        Caller must ensure records have valid sequence numbers.
        """
        if not records:
            return

        session_ids = {r.session_id for r in records}
        if len(session_ids) != 1:
            raise ValueError("All records must have the same session_id")

        session_id = records[0].session_id
        lock = self._get_lock(session_id)

        async with lock:
            path = self._session_path(session_id)
            lines = [self._serialize_record(r) for r in records]
            await file_append_lines(path, lines)

    async def read_session(self, session_id: str) -> list[SessionRecord]:
        lock = self._get_lock(session_id)
        async with lock:
            return await self._read_all_records(session_id)

    async def read_window(self, session_id: str, max_records: int) -> list[SessionRecord]:
        records = await self.read_session(session_id)
        if max_records >= len(records):
            return records
        return records[-max_records:]

    async def get_next_sequence(self, session_id: str) -> int:
        records = await self.read_session(session_id)
        if not records:
            return 0
        return records[-1].sequence + 1

    async def session_exists(self, session_id: str) -> bool:
        path = self._session_path(session_id)
        return path.exists()

    async def list_sessions(self) -> list[str]:
        sessions = []
        for path in self._sessions_dir.glob("*.jsonl"):
            content = path.read_text().strip()
            if not content:
                continue
            first_line = content.split("\n")[0]
            record = self._deserialize_record(first_line)
            if record is not None:
                sessions.append(record.session_id)
        return sessions

    async def clear_session(self, session_id: str) -> bool:
        lock = self._get_lock(session_id)
        async with lock:
            path = self._session_path(session_id)
            if not path.exists():
                return False
            path.unlink()
            return True

    async def replay_for_turn(self, session_id: str, budget: int) -> list[SessionRecord]:
        """
        Reconstruct model-facing history for the given session.

        Delegates to build_replay() with all persisted records.
        """
        records = await self.read_session(session_id)
        return build_replay(records, budget)

    async def get_session_metadata(self, session_id: str) -> dict[str, Any] | None:
        """Get metadata about a session (record count, last activity, etc)."""
        records = await self.read_session(session_id)
        if not records:
            return None

        return {
            "session_id": session_id,
            "record_count": len(records),
            "first_timestamp": records[0].timestamp.isoformat(),
            "last_timestamp": records[-1].timestamp.isoformat(),
            "last_sequence": records[-1].sequence,
        }
