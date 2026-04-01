"""
Component ID: CMP_STORE_SESSION_PERSISTENCE

Filesystem-backed implementation of session metadata storage.
"""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from assistant.core.session.interfaces import SessionMetadataStoreInterface
from assistant.core.session.metadata import SessionMetadata, SessionState, SessionStatus
from assistant.store.filesystem.atomic import atomic_write_text, ensure_directory

logger = structlog.get_logger(__name__)

_STORAGE_VERSION = 1

# Pattern for allowed characters in session IDs: alphanumeric, underscore, colon, hyphen
_SAFE_SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_:\-]+$")


class FilesystemSessionMetadataStore(SessionMetadataStoreInterface):
    """
    Filesystem-backed session metadata storage.

    Stores session metadata as JSON files in the configured directory.
    Each session has its own file: <session_id>.meta.json
    """

    def __init__(self, storage_dir: Path) -> None:
        self._storage_dir = storage_dir
        ensure_directory(storage_dir)
        # Per-session locks to prevent lost-update races during concurrent read-modify-write
        self._session_locks: dict[str, asyncio.Lock] = {}

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create a lock for a specific session."""
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    async def save(self, metadata: SessionMetadata, state: SessionState) -> None:
        """Save session metadata and state to persistent storage."""
        path = self._get_path(metadata.session_id)
        data = {
            "__version": _STORAGE_VERSION,
            "metadata": metadata.to_dict(),
            "state": state.to_dict(),
        }
        await atomic_write_text(path, json.dumps(data, indent=2))
        logger.debug(
            "session_metadata.saved",
            session_id=metadata.session_id,
            context_id=metadata.context_id,
        )

    async def load(self, session_id: str) -> tuple[SessionMetadata, SessionState] | None:
        """Load session metadata and state from storage."""
        path = self._get_path(session_id)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())

            # Validate storage version for future-proofing schema migrations
            stored_version = data.get("__version", 1)
            if stored_version > _STORAGE_VERSION:
                logger.warning(
                    "session_metadata.version_mismatch",
                    session_id=session_id,
                    stored_version=stored_version,
                    current_version=_STORAGE_VERSION,
                )
                return None

            metadata = SessionMetadata.from_dict(data["metadata"])
            state = SessionState.from_dict(data["state"])
            return metadata, state
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "session_metadata.load_failed",
                session_id=session_id,
                error=str(e),
            )
            return None

    async def update_state(self, session_id: str, state: SessionState) -> bool:
        """Update session state with concurrency protection."""
        path = self._get_path(session_id)
        if not path.exists():
            return False

        # Use per-session lock to prevent lost-update races in read-modify-write
        async with self._get_session_lock(session_id):
            try:
                data = json.loads(path.read_text())
                data["state"] = state.to_dict()
                await atomic_write_text(path, json.dumps(data, indent=2))
                logger.debug(
                    "session_metadata.state_updated",
                    session_id=session_id,
                    status=state.status,
                )
                return True
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                logger.warning(
                    "session_metadata.update_failed",
                    session_id=session_id,
                    error=str(e),
                )
                return False

    async def delete(self, session_id: str) -> bool:
        """Delete session metadata."""
        path = self._get_path(session_id)
        if not path.exists():
            return False

        try:
            path.unlink()
            logger.info("session_metadata.deleted", session_id=session_id)
            return True
        except OSError as e:
            logger.warning(
                "session_metadata.delete_failed",
                session_id=session_id,
                error=str(e),
            )
            return False

    async def list_by_context(self, context_id: str) -> list[SessionMetadata]:
        """List all sessions for a given context."""
        results: list[SessionMetadata] = []

        for path in self._storage_dir.glob("*.meta.json"):
            try:
                data = json.loads(path.read_text())
                metadata = SessionMetadata.from_dict(data["metadata"])
                if metadata.context_id == context_id:
                    results.append(metadata)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError):
                continue

        # Sort by creation time, newest first
        results.sort(key=lambda m: m.created_at, reverse=True)
        return results

    async def list_by_status(
        self, status: str, context_id: str | None = None
    ) -> list[SessionMetadata]:
        """List sessions by status, optionally filtered by context."""
        results: list[SessionMetadata] = []
        target_status = SessionStatus(status)

        for path in self._storage_dir.glob("*.meta.json"):
            try:
                data = json.loads(path.read_text())
                metadata = SessionMetadata.from_dict(data["metadata"])
                state = SessionState.from_dict(data["state"])

                if state.status != target_status:
                    continue
                if context_id is not None and metadata.context_id != context_id:
                    continue

                results.append(metadata)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError):
                continue

        results.sort(key=lambda m: m.created_at, reverse=True)
        return results

    async def cleanup_archived(self, max_age_days: int) -> int:
        """Remove archived sessions older than max_age_days."""
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        removed_count = 0

        for path in self._storage_dir.glob("*.meta.json"):
            try:
                data = json.loads(path.read_text())
                state = SessionState.from_dict(data["state"])

                if state.status != SessionStatus.ARCHIVED:
                    continue

                if state.last_activity_at < cutoff:
                    session_id = data["metadata"]["session_id"]
                    path.unlink()
                    removed_count += 1
                    logger.info(
                        "session_metadata.cleanup_removed",
                        session_id=session_id,
                        last_activity=state.last_activity_at.isoformat(),
                    )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError):
                continue

        if removed_count > 0:
            logger.info(
                "session_metadata.cleanup_completed",
                removed_count=removed_count,
                max_age_days=max_age_days,
            )

        return removed_count

    def _get_path(self, session_id: str) -> Path:
        """Get the file path for a session's metadata."""
        safe_id = self._sanitize_session_id(session_id)
        return self._storage_dir / f"{safe_id}.meta.json"

    def _sanitize_session_id(self, session_id: str) -> str:
        """
        Sanitize session_id for use in filename.

        Only allows [a-zA-Z0-9_:-]. Any session_id containing other characters
        is hashed to prevent path traversal and filesystem compatibility issues.
        """
        if _SAFE_SESSION_ID_PATTERN.match(session_id):
            return session_id
        # Hash session IDs with unsafe characters to ensure filesystem safety
        hashed = hashlib.sha256(session_id.encode()).hexdigest()[:32]
        logger.debug(
            "session_metadata.id_sanitized",
            original_id=session_id,
            sanitized_id=hashed,
        )
        return f"hashed_{hashed}"
