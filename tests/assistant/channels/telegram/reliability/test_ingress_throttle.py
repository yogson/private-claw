"""
Integration tests verifying throttle guard and audit logger wiring in TelegramIngress.
"""

from unittest.mock import MagicMock

import pytest

from assistant.channels.telegram.allowlist import AllowlistGuard, UnauthorizedUserError
from assistant.channels.telegram.ingress import TelegramIngress
from assistant.channels.telegram.reliability.audit import ChannelAuditLogger
from assistant.channels.telegram.reliability.throttle import ChannelThrottleGuard, ThrottledError


def _text_update(user_id: int = 100, text: str = "hi", message_id: int = 1) -> dict:
    return {
        "message": {
            "message_id": message_id,
            "from": {"id": user_id},
            "chat": {"id": user_id},
            "date": 1700000000,
            "text": text,
        }
    }


def _callback_update(user_id: int = 100, cq_id: str = "cq1") -> dict:
    return {
        "callback_query": {
            "id": cq_id,
            "from": {"id": user_id},
            "data": "action:test",
            "message": {"message_id": 5, "chat": {"id": user_id}},
        }
    }


class TestIngressThrottleIntegration:
    def _make_ingress(
        self, max_per_window: int = 5
    ) -> tuple[TelegramIngress, ChannelThrottleGuard, MagicMock]:
        guard = AllowlistGuard([100])
        throttle = ChannelThrottleGuard(max_per_window=max_per_window)
        audit = MagicMock(spec=ChannelAuditLogger)
        ingress = TelegramIngress(guard, throttle_guard=throttle, audit_logger=audit)
        return ingress, throttle, audit

    def test_authorized_event_emits_audit_log(self) -> None:
        ingress, _, audit = self._make_ingress()
        event = ingress.normalize(_text_update())
        assert event is not None
        audit.log_ingress_authorized.assert_called_once_with(
            user_id=100, event_type="user_text_message", trace_id=event.trace_id
        )

    def test_throttled_event_raises_and_emits_throttle_audit(self) -> None:
        ingress, _, audit = self._make_ingress(max_per_window=1)
        ingress.normalize(_text_update(message_id=1))
        with pytest.raises(ThrottledError):
            ingress.normalize(_text_update(message_id=2))
        audit.log_ingress_throttled.assert_called_once()
        call_kwargs = audit.log_ingress_throttled.call_args.kwargs
        assert call_kwargs["user_id"] == 100
        assert call_kwargs["limit"] == 1

    def test_callback_authorized_emits_audit_log(self) -> None:
        ingress, _, audit = self._make_ingress()
        event = ingress.normalize(_callback_update())
        assert event is not None
        audit.log_ingress_authorized.assert_called_once_with(
            user_id=100, event_type="user_callback_query", trace_id=event.trace_id
        )

    def test_callback_throttled_raises_and_emits_throttle_audit(self) -> None:
        ingress, _, audit = self._make_ingress(max_per_window=1)
        ingress.normalize(_callback_update(cq_id="cq1"))
        with pytest.raises(ThrottledError):
            ingress.normalize(_callback_update(cq_id="cq2"))
        audit.log_ingress_throttled.assert_called_once()

    def test_no_throttle_guard_still_normalizes(self) -> None:
        guard = AllowlistGuard([100])
        ingress = TelegramIngress(guard)
        event = ingress.normalize(_text_update())
        assert event is not None

    def test_throttle_not_triggered_under_limit(self) -> None:
        ingress, _, audit = self._make_ingress(max_per_window=3)
        for i in range(3):
            ingress.normalize(_text_update(message_id=i))
        assert audit.log_ingress_throttled.call_count == 0
        assert audit.log_ingress_authorized.call_count == 3

    def test_blocked_user_emits_blocked_audit_and_reraises(self) -> None:
        guard = AllowlistGuard([100])
        audit = MagicMock(spec=ChannelAuditLogger)
        ingress = TelegramIngress(guard, audit_logger=audit)
        with pytest.raises(UnauthorizedUserError):
            ingress.normalize(_text_update(user_id=999))
        audit.log_ingress_blocked.assert_called_once()
        call_kwargs = audit.log_ingress_blocked.call_args.kwargs
        assert call_kwargs["user_id"] == 999
        assert call_kwargs["reason"] == "not_in_allowlist"
        assert call_kwargs["trace_id"] != ""

    def test_blocked_callback_emits_blocked_audit(self) -> None:
        guard = AllowlistGuard([100])
        audit = MagicMock(spec=ChannelAuditLogger)
        ingress = TelegramIngress(guard, audit_logger=audit)
        with pytest.raises(UnauthorizedUserError):
            ingress.normalize(_callback_update(user_id=999))
        audit.log_ingress_blocked.assert_called_once()
        assert audit.log_ingress_blocked.call_args.kwargs["user_id"] == 999

    def test_pre_trace_id_is_non_empty_even_without_correlation_context(self) -> None:
        guard = AllowlistGuard([100])
        audit = MagicMock(spec=ChannelAuditLogger)
        ingress = TelegramIngress(guard, audit_logger=audit)
        event = ingress.normalize(_text_update())
        assert event is not None
        assert event.trace_id != ""

    def test_throttled_trace_id_is_non_empty(self) -> None:
        guard = AllowlistGuard([100])
        audit = MagicMock(spec=ChannelAuditLogger)
        throttle = ChannelThrottleGuard(max_per_window=1)
        ingress = TelegramIngress(guard, throttle_guard=throttle, audit_logger=audit)
        ingress.normalize(_text_update(message_id=1))
        with pytest.raises(ThrottledError):
            ingress.normalize(_text_update(message_id=2))
        call_kwargs = audit.log_ingress_throttled.call_args.kwargs
        assert call_kwargs["trace_id"] != ""
