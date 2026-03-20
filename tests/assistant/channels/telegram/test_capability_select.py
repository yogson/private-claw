"""
Unit tests for CapabilitySelectService and capability selection callbacks.
"""

import hashlib
import hmac as _hmac_module
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.capability_select_callbacks import (
    sign_capability_callback,
    verify_capability_callback,
)
from assistant.channels.telegram.capability_select_service import CapabilitySelectService
from assistant.channels.telegram.models import (
    CallbackQueryMeta,
    EventSource,
    EventType,
    MessageType,
    NormalizedEvent,
)
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.config.schemas import TelegramChannelConfig

_SECRET = "test-hmac-secret"
_CHAT_ID = 123456
_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)

_CAP_DEFS: dict[str, CapabilityDefinition] = {
    "web_search": CapabilityDefinition(capability_id="web_search", prompt="Search the web."),
    "shell_execute": CapabilityDefinition(capability_id="shell_execute", prompt="Run shell."),
}


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


def _forge_capability_callback(
    capability_id: str, chat_id: int, secret: str, ts_offset: int = 0
) -> str:
    ts = int(time.time()) + ts_offset
    ts36 = format(ts, "x")
    msg = f"{chat_id}:{capability_id}:{ts36}"
    sig = _hmac_module.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:12]
    return f"cs:{capability_id}:{ts36}:{sig}"


class TestCapabilitySelectCallbacks:
    def test_roundtrip(self) -> None:
        signed = sign_capability_callback("web_search", _CHAT_ID, _SECRET.encode())
        result = verify_capability_callback(signed, _CHAT_ID, _SECRET.encode())
        assert result == "web_search"

    def test_wrong_chat_id_rejected(self) -> None:
        signed = sign_capability_callback("web_search", _CHAT_ID, _SECRET.encode())
        result = verify_capability_callback(signed, 999999, _SECRET.encode())
        assert result is None

    def test_wrong_secret_rejected(self) -> None:
        signed = sign_capability_callback("web_search", _CHAT_ID, _SECRET.encode())
        result = verify_capability_callback(signed, _CHAT_ID, b"wrong-secret")
        assert result is None

    def test_invalid_format_rejected(self) -> None:
        assert verify_capability_callback("invalid", _CHAT_ID, _SECRET.encode()) is None
        assert verify_capability_callback("cs:cap", _CHAT_ID, _SECRET.encode()) is None

    def test_expired_callback_rejected(self) -> None:
        signed = _forge_capability_callback("web_search", _CHAT_ID, _SECRET, ts_offset=-7200)
        result = verify_capability_callback(signed, _CHAT_ID, _SECRET.encode())
        assert result is None

    def test_future_timestamp_rejected(self) -> None:
        signed = _forge_capability_callback("web_search", _CHAT_ID, _SECRET, ts_offset=9999)
        result = verify_capability_callback(signed, _CHAT_ID, _SECRET.encode())
        assert result is None


class TestCapabilitySelectService:
    def test_build_capabilities_menu_empty_defs(self) -> None:
        svc = CapabilitySelectService(capability_definitions={}, hmac_secret=_SECRET)
        resp = svc.build_capabilities_menu(
            current_session_id="tg:1",
            chat_id=_CHAT_ID,
            trace_id="t",
            enabled_capabilities=[],
        )
        assert resp.message_type == MessageType.TEXT
        assert "No capabilities available" in resp.text

    def test_build_capabilities_menu_returns_interactive(self) -> None:
        svc = CapabilitySelectService(capability_definitions=_CAP_DEFS, hmac_secret=_SECRET)
        resp = svc.build_capabilities_menu(
            current_session_id="tg:1",
            chat_id=_CHAT_ID,
            trace_id="t",
            enabled_capabilities=["web_search"],
        )
        assert resp.message_type == MessageType.INTERACTIVE
        assert resp.ui_kind == "capability_select"
        assert len(resp.actions) == len(_CAP_DEFS)

    def test_enabled_capability_marked_with_checkmark(self) -> None:
        svc = CapabilitySelectService(capability_definitions=_CAP_DEFS, hmac_secret=_SECRET)
        resp = svc.build_capabilities_menu(
            current_session_id="tg:1",
            chat_id=_CHAT_ID,
            trace_id="t",
            enabled_capabilities=["web_search"],
        )
        assert "✓" in resp.text
        marked = [a for a in resp.actions if "✓" in a.label]
        assert len(marked) == 1
        assert "web_search" in marked[0].label

    def test_verify_callback_valid(self) -> None:
        svc = CapabilitySelectService(capability_definitions=_CAP_DEFS, hmac_secret=_SECRET)
        signed = svc.sign_callback("shell_execute", _CHAT_ID)
        result = svc.verify_callback(signed, _CHAT_ID)
        assert result == "shell_execute"

    def test_verify_callback_wrong_chat(self) -> None:
        svc = CapabilitySelectService(capability_definitions=_CAP_DEFS, hmac_secret=_SECRET)
        signed = svc.sign_callback("shell_execute", _CHAT_ID)
        result = svc.verify_callback(signed, 999999)
        assert result is None


class TestAdapterCapabilitySelect:
    def _make_adapter(self) -> TelegramAdapter:
        config = TelegramChannelConfig(
            enabled=True,
            bot_token="12345:token",
            allowlist=[_CHAT_ID],
            session_resume_hmac_secret=_SECRET,
        )
        return TelegramAdapter(
            config,
            session_store=MagicMock(),
            model_allowlist=["claude-sonnet-4-5"],
            default_model_id="claude-sonnet-4-5",
            capability_definitions=_CAP_DEFS,
            default_capabilities=["web_search"],
        )

    def test_is_capabilities_request_true(self) -> None:
        adapter = self._make_adapter()
        event = _make_event(text="/capabilities")
        assert adapter.is_capabilities_request(event) is True

    def test_is_capabilities_request_false_without_defs(self) -> None:
        config = TelegramChannelConfig(
            enabled=True,
            bot_token="12345:token",
            allowlist=[_CHAT_ID],
            session_resume_hmac_secret=_SECRET,
        )
        adapter = TelegramAdapter(config, capability_definitions=None)
        event = _make_event(text="/capabilities")
        assert adapter.is_capabilities_request(event) is False

    @pytest.mark.asyncio
    async def test_build_capabilities_menu_response(self) -> None:
        adapter = self._make_adapter()
        resp = await adapter.build_capabilities_menu_response(_CHAT_ID, "tg:1", "trace-1")
        assert resp.message_type == MessageType.INTERACTIVE
        assert "web_search" in resp.text

    def test_is_capabilities_callback_request_true_for_valid_prefix(self) -> None:
        adapter = self._make_adapter()
        signed = _forge_capability_callback("web_search", _CHAT_ID, _SECRET)
        event = _make_event(callback_data=signed)
        assert adapter.is_capabilities_callback_request(event) is True

    def test_handle_capabilities_callback_toggles_capability(self) -> None:
        adapter = self._make_adapter()
        # Initially web_search is in default_capabilities; toggling off removes it.
        signed = _forge_capability_callback("web_search", _CHAT_ID, _SECRET)
        event = _make_event(callback_data=signed)
        result = adapter.handle_capabilities_callback(event)
        assert result == "web_search"
        override = adapter.get_capabilities_override(_CHAT_ID)
        assert override is not None
        assert "web_search" not in override

    def test_handle_capabilities_callback_invalid_returns_none(self) -> None:
        adapter = self._make_adapter()
        event = _make_event(callback_data="cs:web_search:bad:sig")
        result = adapter.handle_capabilities_callback(event)
        assert result is None

    def test_get_capabilities_override_returns_none_before_any_toggle(self) -> None:
        adapter = self._make_adapter()
        assert adapter.get_capabilities_override(_CHAT_ID) is None

    def test_get_capabilities_override_returns_none_for_zero_chat(self) -> None:
        adapter = self._make_adapter()
        assert adapter.get_capabilities_override(0) is None
