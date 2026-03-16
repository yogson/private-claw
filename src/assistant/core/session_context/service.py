"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Active session context service with optional filesystem persistence.
"""

import json
from pathlib import Path
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)


class ActiveSessionContextInterface(Protocol):
    """Interface for managing active session routing context."""

    def get_active_session(self, context_id: str) -> str | None:
        """Return active session id for the given context."""

    def set_active_session(self, context_id: str, session_id: str) -> None:
        """Set active session id for the given context."""

    def clear_active_session(self, context_id: str) -> None:
        """Clear active session id for the given context."""


class ActiveSessionContextService(ActiveSessionContextInterface):
    """In-memory active session context with optional persisted backing file."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self._storage_path = storage_path
        self._active_sessions = self._load()

    def get_active_session(self, context_id: str) -> str | None:
        value = self._active_sessions.get(context_id)
        if value is None:
            return None
        normalized = value.strip()
        return normalized if normalized else None

    def set_active_session(self, context_id: str, session_id: str) -> None:
        normalized_context = context_id.strip()
        normalized_session = session_id.strip()
        if not normalized_context or not normalized_session:
            return
        self._active_sessions[normalized_context] = normalized_session
        self._save()

    def clear_active_session(self, context_id: str) -> None:
        normalized_context = context_id.strip()
        if not normalized_context:
            return
        removed = self._active_sessions.pop(normalized_context, None)
        if removed is not None:
            self._save()

    def _load(self) -> dict[str, str]:
        path = self._storage_path
        if path is None or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("session_context.load_failed", path=str(path))
            return {}
        if not isinstance(raw, dict):
            return {}
        loaded: dict[str, str] = {}
        for context_id, session_id in raw.items():
            if not isinstance(context_id, str) or not isinstance(session_id, str):
                continue
            context_clean = context_id.strip()
            session_clean = session_id.strip()
            if context_clean and session_clean:
                loaded[context_clean] = session_clean
        return loaded

    def _save(self) -> None:
        path = self._storage_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(self._active_sessions)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(encoded)
            tmp_path.replace(path)
        except OSError:
            logger.warning("session_context.save_failed", path=str(path))
