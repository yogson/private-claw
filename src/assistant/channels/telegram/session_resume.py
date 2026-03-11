"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Session resume flow for Telegram: listing recent resumable sessions and
switching active session via signed inline keyboard callback selection.
"""

import hashlib
import hmac
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from assistant.channels.telegram.models import ActionButton, ChannelResponse, MessageType
from assistant.store.interfaces import SessionStoreInterface
from assistant.store.models import SessionRecord, SessionRecordType

_RESUME_CALLBACK_ACTION = "resume_session"
_MAX_PREVIEW_LENGTH = 100
_MAX_LABEL_LENGTH = 40
_HMAC_SIG_LENGTH = 16
_BUTTON_LABEL_MAX = 64
# Callbacks older than this are rejected to prevent replay.
_CALLBACK_TTL_SECONDS = 3600

_SESSIONS_COMMAND = "/sessions"


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

        Format: ``resume_session:{chat_id}:{session_id}:{ts}:{sig}``
        where ``sig = HMAC("{chat_id}:{session_id}:{ts}")``.
        The timestamp enables TTL enforcement; the chat_id prevents cross-chat
        replay even if a payload is intercepted.
        """
        ts = int(datetime.now(UTC).timestamp())
        msg = f"{chat_id}:{session_id}:{ts}"
        sig = hmac.new(self._secret, msg.encode(), hashlib.sha256).hexdigest()[:_HMAC_SIG_LENGTH]
        return f"{_RESUME_CALLBACK_ACTION}:{chat_id}:{session_id}:{ts}:{sig}"

    def verify_callback(self, callback_data: str, expected_chat_id: int) -> str | None:
        """
        Verify a signed session-resume callback and return session_id if valid.

        Rejects the payload when:
        - format is unrecognised or the action prefix is wrong,
        - the embedded chat_id does not match ``expected_chat_id``,
        - the payload is older than ``_CALLBACK_TTL_SECONDS`` (replay protection),
        - the HMAC signature does not match.

        Parsing is right-anchored so that session_ids containing colons
        (e.g. ``tg:123456``) are handled correctly.
        """
        # Strip sig (always last segment)
        without_sig, _, sig = callback_data.rpartition(":")
        if not without_sig or not sig:
            return None
        # Strip ts (now last segment)
        without_ts, _, ts_str = without_sig.rpartition(":")
        if not without_ts or not ts_str:
            return None
        # Parse "resume_session:{chat_id}:{session_id}" — session_id may contain colons
        parts = without_ts.split(":", 2)
        if len(parts) != 3 or parts[0] != _RESUME_CALLBACK_ACTION:
            return None
        _, chat_id_str, session_id = parts

        # Chat binding check
        try:
            if int(chat_id_str) != expected_chat_id:
                return None
        except ValueError:
            return None

        # TTL / replay check
        try:
            ts = int(ts_str)
        except ValueError:
            return None
        age = int(datetime.now(UTC).timestamp()) - ts
        if age < 0 or age > _CALLBACK_TTL_SECONDS:
            return None

        # Signature check
        msg = f"{chat_id_str}:{session_id}:{ts_str}"
        expected = hmac.new(self._secret, msg.encode(), hashlib.sha256).hexdigest()[
            :_HMAC_SIG_LENGTH
        ]
        if not hmac.compare_digest(sig, expected):
            return None
        return session_id

    @staticmethod
    def is_resume_request(text: str | None) -> bool:
        """Return True if the message text is the /sessions bot command."""
        if not text:
            return False
        # Match /sessions or /sessions@botname (Telegram appends bot username in groups)
        normalized = text.strip().lower()
        return normalized == _SESSIONS_COMMAND or normalized.startswith(_SESSIONS_COMMAND + "@")

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


def _extract_label(records: list[SessionRecord]) -> str:
    for record in records:
        if record.record_type == SessionRecordType.TURN_SUMMARY:
            text = str(record.payload.get("summary_text", ""))
            if text:
                return text[:_MAX_LABEL_LENGTH]
    for record in records:
        if record.record_type == SessionRecordType.USER_MESSAGE:
            content = str(record.payload.get("content", ""))
            if content:
                return content[:_MAX_LABEL_LENGTH]
    return str(records[0].session_id)[:_MAX_LABEL_LENGTH]


def _extract_preview(records: list[SessionRecord]) -> str:
    for record in reversed(records):
        if record.record_type in (
            SessionRecordType.USER_MESSAGE,
            SessionRecordType.ASSISTANT_MESSAGE,
        ):
            content = str(record.payload.get("content", ""))
            if content:
                return content[:_MAX_PREVIEW_LENGTH]
    return ""
