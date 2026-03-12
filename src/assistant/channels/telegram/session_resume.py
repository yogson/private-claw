"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Session resume flow for Telegram: listing recent resumable sessions and
switching active session via signed inline keyboard callback selection.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel

from assistant.channels.telegram.models import ActionButton, ChannelResponse, MessageType
from assistant.channels.telegram.session_resume_callbacks import (
    CALLBACK_TTL_SECONDS,
    sign_resume_callback,
    verify_resume_callback,
)
from assistant.channels.telegram.session_resume_labels import (
    extract_label as _extract_label,
)
from assistant.channels.telegram.session_resume_labels import (
    extract_preview as _extract_preview,
)
from assistant.store.interfaces import SessionStoreInterface

_BUTTON_LABEL_MAX = 64

_CALLBACK_TTL_SECONDS = CALLBACK_TTL_SECONDS


class SessionEntry(BaseModel):
    """Compact user-facing session metadata for resume selection."""

    session_id: str
    label: str
    last_activity: datetime
    preview_snippet: str


class SessionResumeService:
    """
    Builds Telegram session resume selection flows.

    Lists recent resumable sessions for a specific chat and produces
    ChannelResponse objects with signed inline keyboard callbacks.
    Callbacks are bound to a chat_id and carry a timestamp so that
    cross-chat replay is impossible and stale payloads are rejected.
    """

    def __init__(
        self,
        session_store: SessionStoreInterface,
        hmac_secret: str,
        max_sessions: int = 5,
    ) -> None:
        self._store = session_store
        self._secret = hmac_secret.encode()
        self._max_sessions = max_sessions

    async def list_recent_sessions(self, chat_id: int) -> list[SessionEntry]:
        """
        List recent resumable sessions scoped to the given chat_id.

        Only sessions whose session_id equals ``tg:{chat_id}`` or starts with
        ``tg:{chat_id}:`` are returned, preventing cross-chat discovery.
        Results are sorted by last activity descending and capped at max_sessions.
        """
        all_sessions = await self._store.list_sessions()
        chat_prefix = f"tg:{chat_id}"
        scoped = [s for s in all_sessions if s == chat_prefix or s.startswith(chat_prefix + ":")]

        entries: list[SessionEntry] = []
        for session_id in scoped:
            entry = await self._build_session_entry(session_id)
            if entry is not None:
                entries.append(entry)

        entries.sort(key=lambda e: e.last_activity, reverse=True)
        return entries[: self._max_sessions]

    def build_session_menu(
        self,
        entries: list[SessionEntry],
        current_session_id: str,
        chat_id: int,
        trace_id: str,
    ) -> ChannelResponse:
        """
        Build an interactive ChannelResponse with inline buttons for session selection.

        Each button carries a chat-scoped, timestamped signed payload.
        Returns a plain-text response if no sessions are available.
        """
        if not entries:
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=current_session_id,
                trace_id=trace_id,
                message_type=MessageType.TEXT,
                text="No previous sessions found.",
            )

        lines = ["*Recent sessions — tap to resume:*\n"]
        actions: list[ActionButton] = []
        for i, entry in enumerate(entries, 1):
            ts = entry.last_activity.strftime("%Y-%m-%d %H:%M")
            preview = f" — _{entry.preview_snippet}_" if entry.preview_snippet else ""
            lines.append(f"{i}. *{entry.label}* ({ts}){preview}")
            button_label = f"{i}. {entry.label} ({entry.last_activity.strftime('%m/%d %H:%M')})"
            actions.append(
                ActionButton(
                    label=button_label[:_BUTTON_LABEL_MAX],
                    callback_id=f"resume:{entry.session_id}",
                    callback_data=self.sign_callback(entry.session_id, chat_id),
                )
            )

        return ChannelResponse(
            response_id=str(uuid.uuid4()),
            channel="telegram",
            session_id=current_session_id,
            trace_id=trace_id,
            message_type=MessageType.INTERACTIVE,
            text="\n".join(lines),
            parse_mode="Markdown",
            ui_kind="session_resume",
            actions=actions,
        )

    def sign_callback(self, session_id: str, chat_id: int) -> str:
        """
        Generate a chat-scoped, timestamped signed callback payload.

        Compact format: ``rs:{session_id}:{ts36}:{sig}``
        where ``sig = HMAC("{chat_id}:{session_id}:{ts36}")``.
        The payload intentionally omits chat_id to stay under Telegram's
        callback_data size limit, while the signature still binds the payload
        to the originating chat via the signed message.
        """
        return sign_resume_callback(session_id=session_id, chat_id=chat_id, secret=self._secret)

    def verify_callback(self, callback_data: str, expected_chat_id: int) -> str | None:
        """
        Verify a signed session-resume callback and return session_id if valid.

        Rejects the payload when:
        - format is unrecognised or the action prefix is wrong,
        - the payload is older than ``_CALLBACK_TTL_SECONDS`` (replay protection),
        - chat binding fails for compact format:
          ``sig = HMAC("{expected_chat_id}:{session_id}:{ts36}")``.
        - the HMAC signature does not match for the parsed format.

        Parsing is right-anchored so that session_ids containing colons
        (e.g. ``tg:123456``) are handled correctly.
        """
        return verify_resume_callback(
            callback_data=callback_data,
            expected_chat_id=expected_chat_id,
            secret=self._secret,
        )

    async def _build_session_entry(self, session_id: str) -> SessionEntry | None:
        records = await self._store.read_session(session_id)
        if not records:
            return None
        label = _extract_label(records)
        last_activity = records[-1].timestamp
        preview = _extract_preview(records)
        return SessionEntry(
            session_id=session_id,
            label=label,
            last_activity=last_activity,
            preview_snippet=preview,
        )
