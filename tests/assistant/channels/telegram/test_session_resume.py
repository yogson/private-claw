"""
Unit tests for SessionResumeService and TelegramAdapter session-resume integration.
"""

import hashlib
import hmac as _hmac_module
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.channels.telegram.models import EventSource, EventType, MessageType, NormalizedEvent
from assistant.channels.telegram.session_resume import (
    _CALLBACK_TTL_SECONDS,
    SessionEntry,
    SessionResumeService,
    _extract_label,
    _extract_preview,
)
from assistant.store.models import SessionRecord, SessionRecordType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET = "test-hmac-secret"
_CHAT_ID = 123456
_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)


def _make_record(
    session_id: str,
    record_type: SessionRecordType,
    payload: dict,
    seq: int = 0,
    ts: datetime | None = None,
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        sequence=seq,
        event_id=str(uuid.uuid4()),
        turn_id="turn-1",
        timestamp=ts or _NOW,
        record_type=record_type,
        payload=payload,
    )


def _make_store(sessions: dict[str, list[SessionRecord]]) -> MagicMock:
    store = MagicMock()
    store.list_sessions = AsyncMock(return_value=list(sessions.keys()))
    store.read_session = AsyncMock(side_effect=lambda sid: sessions.get(sid, []))
    store.clear_session = AsyncMock(return_value=True)
    return store


def _make_event(text: str | None = None, callback_data: str | None = None) -> NormalizedEvent:
    from assistant.channels.telegram.models import CallbackQueryMeta

    cq = None
    if callback_data is not None:
        cq = CallbackQueryMeta(
            callback_id="cq-1",
            callback_data=callback_data,
            origin_message_id=1,
            ui_version="1",
        )
    return NormalizedEvent(
        event_id=str(uuid.uuid4()),
        event_type=EventType.USER_TEXT_MESSAGE if cq is None else EventType.USER_CALLBACK_QUERY,
        source=EventSource.TELEGRAM,
        session_id=f"tg:{_CHAT_ID}",
        user_id=str(_CHAT_ID),
        created_at=_NOW,
        trace_id="trace-test",
        text=text,
        callback_query=cq,
        metadata={"chat_id": _CHAT_ID},
    )


def _forge_callback(session_id: str, chat_id: int, secret: str, ts_offset: int = 0) -> str:
    """Build a correctly-signed payload, optionally with a shifted timestamp."""
    ts = int(time.time()) + ts_offset
    ts36 = format(ts, "x")
    msg = f"{chat_id}:{session_id}:{ts36}"
    sig = _hmac_module.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:12]
    return f"rs:{session_id}:{ts36}:{sig}"


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


class TestSignAndVerify:
    def test_roundtrip(self) -> None:
        svc = SessionResumeService(MagicMock(), _SECRET)
        signed = svc.sign_callback(f"tg:{_CHAT_ID}", _CHAT_ID)
        assert svc.verify_callback(signed, _CHAT_ID) == f"tg:{_CHAT_ID}"

    def test_wrong_chat_id_rejected(self) -> None:
        svc = SessionResumeService(MagicMock(), _SECRET)
        signed = svc.sign_callback(f"tg:{_CHAT_ID}", _CHAT_ID)
        assert svc.verify_callback(signed, expected_chat_id=999999) is None

    def test_tampered_session_id_rejected(self) -> None:
        svc = SessionResumeService(MagicMock(), _SECRET)
        signed = svc.sign_callback(f"tg:{_CHAT_ID}", _CHAT_ID)
        tampered = f"{signed[:-1]}0" if signed[-1] != "0" else f"{signed[:-1]}1"
        assert svc.verify_callback(tampered, _CHAT_ID) is None

    def test_wrong_action_prefix_rejected(self) -> None:
        svc = SessionResumeService(MagicMock(), _SECRET)
        bad = f"bad_action:{_CHAT_ID}:tg:{_CHAT_ID}:9999999999:abcdef12"
        assert svc.verify_callback(bad, _CHAT_ID) is None

    def test_too_few_parts_rejected(self) -> None:
        svc = SessionResumeService(MagicMock(), _SECRET)
        assert svc.verify_callback(f"resume_session:tg:{_CHAT_ID}", _CHAT_ID) is None

    def test_different_secrets_incompatible(self) -> None:
        svc1 = SessionResumeService(MagicMock(), "secret-a")
        svc2 = SessionResumeService(MagicMock(), "secret-b")
        signed = svc1.sign_callback(f"tg:{_CHAT_ID}", _CHAT_ID)
        assert svc2.verify_callback(signed, _CHAT_ID) is None

    def test_expired_callback_rejected(self) -> None:
        svc = SessionResumeService(MagicMock(), _SECRET)
        stale = _forge_callback(
            f"tg:{_CHAT_ID}", _CHAT_ID, _SECRET, ts_offset=-(_CALLBACK_TTL_SECONDS + 60)
        )
        assert svc.verify_callback(stale, _CHAT_ID) is None

    def test_future_timestamp_rejected(self) -> None:
        svc = SessionResumeService(MagicMock(), _SECRET)
        future = _forge_callback(f"tg:{_CHAT_ID}", _CHAT_ID, _SECRET, ts_offset=9999)
        assert svc.verify_callback(future, _CHAT_ID) is None

    def test_legacy_payload_rejected(self) -> None:
        svc = SessionResumeService(MagicMock(), _SECRET)
        legacy = "resume_session:123456:tg:123456:1700000000:deadbeefdeadbeef"
        assert svc.verify_callback(legacy, _CHAT_ID) is None


# ---------------------------------------------------------------------------
# list_recent_sessions
# ---------------------------------------------------------------------------


class TestListRecentSessions:
    @pytest.mark.asyncio
    async def test_scoped_to_requesting_chat_only(self) -> None:
        """Sessions for other chats must not appear in the listing."""
        own_sid = f"tg:{_CHAT_ID}"
        other_sid = "tg:999999"
        store = _make_store(
            {
                own_sid: [_make_record(own_sid, SessionRecordType.USER_MESSAGE, {"content": "hi"})],
                other_sid: [
                    _make_record(other_sid, SessionRecordType.USER_MESSAGE, {"content": "x"})
                ],
            }
        )
        svc = SessionResumeService(store, _SECRET)
        entries = await svc.list_recent_sessions(_CHAT_ID)
        assert len(entries) == 1
        assert entries[0].session_id == own_sid

    @pytest.mark.asyncio
    async def test_includes_chat_variant_sessions(self) -> None:
        """Sessions like tg:{chat_id}:{variant} are included."""
        base = f"tg:{_CHAT_ID}"
        variant = f"tg:{_CHAT_ID}:alt"
        store = _make_store(
            {
                base: [_make_record(base, SessionRecordType.USER_MESSAGE, {"content": "main"})],
                variant: [
                    _make_record(variant, SessionRecordType.USER_MESSAGE, {"content": "alt"})
                ],
            }
        )
        svc = SessionResumeService(store, _SECRET)
        entries = await svc.list_recent_sessions(_CHAT_ID)
        assert {e.session_id for e in entries} == {base, variant}

    @pytest.mark.asyncio
    async def test_sorted_by_last_activity_desc(self) -> None:
        ts1 = datetime(2026, 1, 1, tzinfo=UTC)
        ts2 = datetime(2026, 1, 10, tzinfo=UTC)
        sid_a = f"tg:{_CHAT_ID}:a"
        sid_b = f"tg:{_CHAT_ID}:b"
        store = _make_store(
            {
                sid_a: [
                    _make_record(sid_a, SessionRecordType.USER_MESSAGE, {"content": "a"}, ts=ts1)
                ],
                sid_b: [
                    _make_record(sid_b, SessionRecordType.USER_MESSAGE, {"content": "b"}, ts=ts2)
                ],
            }
        )
        svc = SessionResumeService(store, _SECRET)
        entries = await svc.list_recent_sessions(_CHAT_ID)
        assert entries[0].session_id == sid_b
        assert entries[1].session_id == sid_a

    @pytest.mark.asyncio
    async def test_max_sessions_respected(self) -> None:
        sessions = {
            f"tg:{_CHAT_ID}:{i}": [
                _make_record(
                    f"tg:{_CHAT_ID}:{i}", SessionRecordType.USER_MESSAGE, {"content": f"msg{i}"}
                )
            ]
            for i in range(10)
        }
        store = _make_store(sessions)
        svc = SessionResumeService(store, _SECRET, max_sessions=3)
        entries = await svc.list_recent_sessions(_CHAT_ID)
        assert len(entries) == 3

    @pytest.mark.asyncio
    async def test_empty_sessions_excluded(self) -> None:
        store = _make_store({f"tg:{_CHAT_ID}": []})
        svc = SessionResumeService(store, _SECRET)
        entries = await svc.list_recent_sessions(_CHAT_ID)
        assert entries == []


# ---------------------------------------------------------------------------
# build_session_menu
# ---------------------------------------------------------------------------


class TestBuildSessionMenu:
    def test_empty_entries_returns_text_response(self) -> None:
        svc = SessionResumeService(MagicMock(), _SECRET)
        resp = svc.build_session_menu([], f"tg:{_CHAT_ID}", _CHAT_ID, "trace-x")
        assert resp.message_type == MessageType.TEXT
        assert "No previous sessions" in resp.text

    def test_entries_returns_interactive_response(self) -> None:
        entries = [
            SessionEntry(
                session_id=f"tg:{_CHAT_ID}",
                label="Chat about Python",
                last_activity=_NOW,
                preview_snippet="Tell me about decorators",
            )
        ]
        svc = SessionResumeService(MagicMock(), _SECRET)
        resp = svc.build_session_menu(entries, f"tg:{_CHAT_ID}", _CHAT_ID, "trace-x")
        assert resp.message_type == MessageType.INTERACTIVE
        assert len(resp.actions) == 1
        assert resp.ui_kind == "session_resume"
        # Callback must be verifiable for the same chat_id
        verified = svc.verify_callback(resp.actions[0].callback_data, _CHAT_ID)
        assert verified == f"tg:{_CHAT_ID}"
        assert len(resp.actions[0].callback_data.encode("utf-8")) <= 64

    def test_long_session_id_keeps_callback_under_telegram_limit(self) -> None:
        entries = [
            SessionEntry(
                session_id=f"tg:{_CHAT_ID}:3eeada40275f",
                label="Long Session",
                last_activity=_NOW,
                preview_snippet="",
            )
        ]
        svc = SessionResumeService(MagicMock(), _SECRET)
        resp = svc.build_session_menu(entries, f"tg:{_CHAT_ID}", _CHAT_ID, "trace-x")
        assert len(resp.actions) == 1
        assert len(resp.actions[0].callback_data.encode("utf-8")) <= 64
        assert (
            svc.verify_callback(resp.actions[0].callback_data, _CHAT_ID)
            == f"tg:{_CHAT_ID}:3eeada40275f"
        )

    def test_callback_rejects_different_chat(self) -> None:
        """Buttons signed for chat A must not verify for chat B."""
        entries = [
            SessionEntry(
                session_id=f"tg:{_CHAT_ID}",
                label="Test",
                last_activity=_NOW,
                preview_snippet="",
            )
        ]
        svc = SessionResumeService(MagicMock(), _SECRET)
        resp = svc.build_session_menu(entries, f"tg:{_CHAT_ID}", _CHAT_ID, "trace-x")
        assert svc.verify_callback(resp.actions[0].callback_data, expected_chat_id=999999) is None


# ---------------------------------------------------------------------------
# _extract_label / _extract_preview helpers
# ---------------------------------------------------------------------------


class TestExtractHelpers:
    def test_label_prefers_turn_summary(self) -> None:
        records = [
            _make_record("s", SessionRecordType.USER_MESSAGE, {"content": "hello"}),
            _make_record("s", SessionRecordType.TURN_SUMMARY, {"summary_text": "Summary title"}),
        ]
        assert _extract_label(records) == "Summary title"

    def test_label_falls_back_to_user_message(self) -> None:
        records = [_make_record("s", SessionRecordType.USER_MESSAGE, {"content": "hello world"})]
        assert _extract_label(records) == "hello world"

    def test_label_falls_back_to_session_id(self) -> None:
        records = [
            _make_record("my-session", SessionRecordType.TURN_TERMINAL, {"status": "completed"})
        ]
        assert _extract_label(records) == "my-session"

    def test_label_truncated_to_max(self) -> None:
        long_text = "x" * 100
        records = [_make_record("s", SessionRecordType.USER_MESSAGE, {"content": long_text})]
        assert len(_extract_label(records)) == 40

    def test_preview_last_assistant_message(self) -> None:
        records = [
            _make_record("s", SessionRecordType.USER_MESSAGE, {"content": "Q"}),
            _make_record("s", SessionRecordType.ASSISTANT_MESSAGE, {"content": "Answer here"}),
        ]
        assert _extract_preview(records) == "Answer here"

    def test_preview_empty_when_no_messages(self) -> None:
        records = [_make_record("s", SessionRecordType.TURN_TERMINAL, {"status": "completed"})]
        assert _extract_preview(records) == ""

    def test_preview_truncated_to_max(self) -> None:
        records = [_make_record("s", SessionRecordType.USER_MESSAGE, {"content": "x" * 200})]
        assert len(_extract_preview(records)) == 100


# ---------------------------------------------------------------------------
# TelegramAdapter session-resume integration
# ---------------------------------------------------------------------------


class TestAdapterSessionResume:
    def _make_adapter(self, with_store: bool = True) -> tuple:  # type: ignore[type-arg]
        from assistant.channels.telegram.adapter import TelegramAdapter
        from assistant.core.config.schemas import TelegramChannelConfig

        config = TelegramChannelConfig(
            enabled=True,
            bot_token="12345:token",
            allowlist=[_CHAT_ID],
            session_resume_hmac_secret=_SECRET,
        )
        store = None
        if with_store:
            store = _make_store(
                {
                    f"tg:{_CHAT_ID}": [
                        _make_record(
                            f"tg:{_CHAT_ID}", SessionRecordType.USER_MESSAGE, {"content": "hello"}
                        )
                    ]
                }
            )
        return TelegramAdapter(config, session_store=store), store

    def test_is_session_resume_request_true(self) -> None:
        adapter, _ = self._make_adapter()
        event = _make_event(text="/sessions")
        assert adapter.is_session_resume_request(event) is True

    def test_is_session_reset_available_true_with_store(self) -> None:
        adapter, _ = self._make_adapter(with_store=True)
        assert adapter.is_session_reset_available() is True

    @pytest.mark.parametrize("text", ["/new", " /NEW ", "/new@mybot"])
    def test_is_session_new_request_true(self, text: str) -> None:
        adapter, _ = self._make_adapter()
        event = _make_event(text=text)
        assert adapter.is_session_new_request(event) is True

    @pytest.mark.parametrize("text", ["new", "/news", "/reset", "/sessions", "hello", None, ""])
    def test_is_session_new_request_false(self, text: str | None) -> None:
        adapter, _ = self._make_adapter()
        event = _make_event(text=text)
        assert adapter.is_session_new_request(event) is False

    def test_is_session_reset_available_false_without_store(self) -> None:
        adapter, _ = self._make_adapter(with_store=False)
        assert adapter.is_session_reset_available() is False

    @pytest.mark.parametrize("text", ["/reset", " /RESET ", "/reset@mybot"])
    def test_is_session_reset_request_true(self, text: str) -> None:
        adapter, _ = self._make_adapter()
        event = _make_event(text=text)
        assert adapter.is_session_reset_request(event) is True

    @pytest.mark.parametrize("text", ["reset", "/resets", "/sessions", "hello", None, ""])
    def test_is_session_reset_request_false(self, text: str | None) -> None:
        adapter, _ = self._make_adapter()
        event = _make_event(text=text)
        assert adapter.is_session_reset_request(event) is False

    def test_is_session_resume_request_false_without_store(self) -> None:
        adapter, _ = self._make_adapter(with_store=False)
        event = _make_event(text="/sessions")
        assert adapter.is_session_resume_request(event) is False

    def test_is_session_resume_callback_true(self) -> None:
        adapter, _ = self._make_adapter()
        svc = SessionResumeService(MagicMock(), _SECRET)
        signed = svc.sign_callback(f"tg:{_CHAT_ID}", _CHAT_ID)
        event = _make_event(callback_data=signed)
        assert adapter.is_session_resume_callback(event) is True

    def test_is_session_resume_callback_false_invalid(self) -> None:
        adapter, _ = self._make_adapter()
        event = _make_event(callback_data="not_valid:data")
        assert adapter.is_session_resume_callback(event) is False

    def test_handle_session_resume_callback_sets_active_session(self) -> None:
        adapter, _ = self._make_adapter()
        svc = SessionResumeService(MagicMock(), _SECRET)
        signed = svc.sign_callback(f"tg:{_CHAT_ID}", _CHAT_ID)
        event = _make_event(callback_data=signed)
        result = adapter.handle_session_resume_callback(event)
        assert result == f"tg:{_CHAT_ID}"
        assert adapter.get_active_session(_CHAT_ID) == f"tg:{_CHAT_ID}"

    def test_handle_session_resume_callback_wrong_chat_rejected(self) -> None:
        """A callback signed for a different chat must be rejected."""
        adapter, _ = self._make_adapter()
        svc = SessionResumeService(MagicMock(), _SECRET)
        # Sign for chat 999999, but event comes from _CHAT_ID
        signed = svc.sign_callback("tg:999999", 999999)
        event = _make_event(callback_data=signed)
        result = adapter.handle_session_resume_callback(event)
        assert result is None

    def test_handle_session_resume_callback_invalid_returns_none(self) -> None:
        adapter, _ = self._make_adapter()
        event = _make_event(callback_data="bad:data:here")
        result = adapter.handle_session_resume_callback(event)
        assert result is None

    def test_clear_active_session(self) -> None:
        adapter, _ = self._make_adapter()
        adapter._active_sessions[_CHAT_ID] = f"tg:{_CHAT_ID}"
        adapter.clear_active_session(_CHAT_ID)
        assert adapter.get_active_session(_CHAT_ID) is None

    def test_apply_session_context_overrides_session_id(self) -> None:
        adapter, _ = self._make_adapter()
        adapter._active_sessions[_CHAT_ID] = "tg:999"
        event = _make_event(text="hello")
        result = adapter._apply_session_context(event)
        assert result is not None
        assert result.session_id == "tg:999"

    def test_apply_session_context_no_override(self) -> None:
        adapter, _ = self._make_adapter()
        event = _make_event(text="hello")
        result = adapter._apply_session_context(event)
        assert result is not None
        assert result.session_id == f"tg:{_CHAT_ID}"

    def test_apply_session_context_none_passthrough(self) -> None:
        adapter, _ = self._make_adapter()
        assert adapter._apply_session_context(None) is None

    @pytest.mark.asyncio
    async def test_build_session_menu_response_no_store(self) -> None:
        adapter, _ = self._make_adapter(with_store=False)
        resp = await adapter.build_session_menu_response(_CHAT_ID, f"tg:{_CHAT_ID}", "trace-x")
        assert resp.message_type == MessageType.TEXT
        assert "not available" in resp.text

    @pytest.mark.asyncio
    async def test_build_session_menu_response_with_store(self) -> None:
        adapter, _ = self._make_adapter(with_store=True)
        resp = await adapter.build_session_menu_response(_CHAT_ID, f"tg:{_CHAT_ID}", "trace-x")
        assert resp.message_type == MessageType.INTERACTIVE
        assert len(resp.actions) == 1
        # Verify returned callbacks are scoped to this chat
        svc = SessionResumeService(MagicMock(), _SECRET)
        assert svc.verify_callback(resp.actions[0].callback_data, _CHAT_ID) is not None
        assert svc.verify_callback(resp.actions[0].callback_data, 999999) is None

    @pytest.mark.asyncio
    async def test_reset_session_context_delegates_to_store(self) -> None:
        adapter, store = self._make_adapter(with_store=True)
        event = _make_event(text="/reset")
        cleared = await adapter.reset_session_context(event)
        assert cleared is True
        assert store is not None
        store.clear_session.assert_awaited_once_with(f"tg:{_CHAT_ID}")

    def test_start_new_session_activates_override(self) -> None:
        adapter, _ = self._make_adapter(with_store=True)
        event = _make_event(text="/new")
        session_id = adapter.start_new_session(event)
        assert session_id is not None
        assert session_id.startswith(f"tg:{_CHAT_ID}:")
        assert adapter.get_active_session(_CHAT_ID) == session_id

    def test_start_new_session_missing_chat_returns_none(self) -> None:
        adapter, _ = self._make_adapter(with_store=True)
        event = _make_event(text="/new")
        event = event.model_copy(update={"metadata": {}})
        assert adapter.start_new_session(event) is None

    def test_start_new_session_non_numeric_chat_returns_none(self) -> None:
        adapter, _ = self._make_adapter(with_store=True)
        event = _make_event(text="/new")
        event = event.model_copy(update={"metadata": {"chat_id": "not-a-number"}})
        assert adapter.start_new_session(event) is None
