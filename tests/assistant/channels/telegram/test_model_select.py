"""
Unit tests for ModelSelectService and model selection callbacks.
"""

import hashlib
import hmac as _hmac_module
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.model_select import ModelSelectService
from assistant.channels.telegram.model_select_callbacks import (
    sign_model_callback,
    verify_model_callback,
)
from assistant.channels.telegram.models import (
    CallbackQueryMeta,
    EventSource,
    EventType,
    MessageType,
    NormalizedEvent,
)
from assistant.core.config.schemas import TelegramChannelConfig
from assistant.core.session_context import SessionModelContextService

_SECRET = "test-hmac-secret"
_CHAT_ID = 123456
_MODEL_ALLOWLIST = ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"]
_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)


def _make_event(text: str | None = None, callback_data: str | None = None) -> NormalizedEvent:
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


def _forge_model_callback(model_id: str, chat_id: int, secret: str, ts_offset: int = 0) -> str:
    ts = int(time.time()) + ts_offset
    ts36 = format(ts, "x")
    msg = f"{chat_id}:{model_id}:{ts36}"
    sig = _hmac_module.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:12]
    return f"ms:{model_id}:{ts36}:{sig}"


class TestModelSelectCallbacks:
    def test_roundtrip(self) -> None:
        signed = sign_model_callback("claude-sonnet-4-5", _CHAT_ID, _SECRET.encode())
        result = verify_model_callback(signed, _CHAT_ID, _SECRET.encode())
        assert result == "claude-sonnet-4-5"

    def test_wrong_chat_id_rejected(self) -> None:
        signed = sign_model_callback("claude-sonnet-4-5", _CHAT_ID, _SECRET.encode())
        result = verify_model_callback(signed, 999999, _SECRET.encode())
        assert result is None

    def test_invalid_format_rejected(self) -> None:
        assert verify_model_callback("invalid", _CHAT_ID, _SECRET.encode()) is None
        assert verify_model_callback("ms:model", _CHAT_ID, _SECRET.encode()) is None


class TestModelSelectService:
    def test_build_model_menu_empty_allowlist(self) -> None:
        svc = ModelSelectService(model_allowlist=[], hmac_secret=_SECRET)
        resp = svc.build_model_menu(
            current_session_id="tg:1",
            chat_id=_CHAT_ID,
            trace_id="t",
        )
        assert resp.message_type == MessageType.TEXT
        assert "No models available" in resp.text

    def test_build_model_menu_returns_interactive(self) -> None:
        svc = ModelSelectService(model_allowlist=_MODEL_ALLOWLIST, hmac_secret=_SECRET)
        resp = svc.build_model_menu(
            current_session_id="tg:1",
            chat_id=_CHAT_ID,
            trace_id="t",
            current_model_id="claude-sonnet-4-5",
        )
        assert resp.message_type == MessageType.INTERACTIVE
        assert resp.ui_kind == "model_select"
        assert len(resp.actions) == 3
        assert "claude-sonnet-4-5" in resp.text
        assert "✓" in resp.text

    def test_verify_callback_valid(self) -> None:
        svc = ModelSelectService(model_allowlist=_MODEL_ALLOWLIST, hmac_secret=_SECRET)
        signed = svc.sign_callback("claude-haiku-4-5", _CHAT_ID)
        result = svc.verify_callback(signed, _CHAT_ID)
        assert result == "claude-haiku-4-5"


class TestAdapterModelSelect:
    def _make_adapter(self) -> TelegramAdapter:
        config = TelegramChannelConfig(
            enabled=True,
            bot_token="12345:token",
            allowlist=[_CHAT_ID],
            session_resume_hmac_secret=_SECRET,
        )
        model_context = SessionModelContextService(storage_path=None)
        return TelegramAdapter(
            config,
            session_store=MagicMock(),
            model_context=model_context,
            model_allowlist=_MODEL_ALLOWLIST,
            default_model_id="claude-sonnet-4-5",
        )

    def test_is_model_request_true(self) -> None:
        adapter = self._make_adapter()
        event = _make_event(text="/model")
        assert adapter.is_model_request(event) is True

    def test_is_model_request_false_without_allowlist(self) -> None:
        config = TelegramChannelConfig(
            enabled=True,
            bot_token="12345:token",
            allowlist=[_CHAT_ID],
            session_resume_hmac_secret=_SECRET,
        )
        adapter = TelegramAdapter(config, model_allowlist=[])
        event = _make_event(text="/model")
        assert adapter.is_model_request(event) is False

    @pytest.mark.asyncio
    async def test_build_model_menu_response(self) -> None:
        adapter = self._make_adapter()
        resp = await adapter.build_model_menu_response(_CHAT_ID, "tg:1", "trace-1")
        assert resp.message_type == MessageType.INTERACTIVE
        assert "claude" in resp.text

    def test_is_model_callback_request_true_for_valid(self) -> None:
        adapter = self._make_adapter()
        signed = _forge_model_callback("claude-haiku-4-5", _CHAT_ID, _SECRET)
        event = _make_event(callback_data=signed)
        assert adapter.is_model_callback_request(event) is True

    def test_is_model_callback_request_true_for_invalid_prefix(self) -> None:
        """Invalid/expired ms: callback is still routed by prefix."""
        adapter = self._make_adapter()
        event = _make_event(callback_data="ms:claude-haiku-4-5:bad:sig")
        assert adapter.is_model_callback_request(event) is True

    def test_handle_model_callback_sets_override(self) -> None:
        adapter = self._make_adapter()
        signed = _forge_model_callback("claude-haiku-4-5", _CHAT_ID, _SECRET)
        event = _make_event(callback_data=signed)
        result = adapter.handle_model_callback(event)
        assert result == "claude-haiku-4-5"
        assert adapter.get_model_override(_CHAT_ID) == "claude-haiku-4-5"

    def test_handle_model_callback_invalid_returns_none(self) -> None:
        """Tampered/expired ms: callback returns None."""
        adapter = self._make_adapter()
        event = _make_event(callback_data="ms:claude-haiku-4-5:bad:sig")
        result = adapter.handle_model_callback(event)
        assert result is None

    def test_get_model_override_supports_negative_chat_id(self) -> None:
        """Group/supergroup chats (negative chat_id) get model override."""
        adapter = self._make_adapter()
        group_chat_id = -1001234567890
        signed = _forge_model_callback("claude-haiku-4-5", group_chat_id, _SECRET)
        event = _make_event(callback_data=signed)
        event.metadata = {"chat_id": group_chat_id}
        result = adapter.handle_model_callback(event)
        assert result == "claude-haiku-4-5"
        assert adapter.get_model_override(group_chat_id) == "claude-haiku-4-5"

    def test_get_model_override_returns_none_for_chat_id_zero(self) -> None:
        adapter = self._make_adapter()
        assert adapter.get_model_override(0) is None
