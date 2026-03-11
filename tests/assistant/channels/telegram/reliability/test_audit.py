"""
Unit tests for ChannelAuditLogger - verifies structured log emission
without asserting on internal structlog state.
"""

from unittest.mock import MagicMock, patch

import structlog

from assistant.channels.telegram.reliability.audit import ChannelAuditLogger


def _capture_logs() -> list[dict]:
    """Configure structlog to capture log events and return the log list."""
    logs: list[dict] = []
    structlog.configure(
        processors=[structlog.stdlib.add_log_level, structlog.dev.ConsoleRenderer()],
        logger_factory=structlog.PrintLoggerFactory(),
    )
    return logs


class TestChannelAuditLogger:
    def setup_method(self) -> None:
        self.logger = ChannelAuditLogger()

    def test_log_ingress_authorized_does_not_raise(self) -> None:
        self.logger.log_ingress_authorized(
            user_id=123, event_type="user_text_message", trace_id="t1"
        )

    def test_log_ingress_blocked_does_not_raise(self) -> None:
        self.logger.log_ingress_blocked(user_id=123, reason="not_in_allowlist", trace_id="t2")

    def test_log_ingress_throttled_does_not_raise(self) -> None:
        self.logger.log_ingress_throttled(user_id=123, count=20, limit=20, trace_id="t3")

    def test_log_egress_attempt_does_not_raise(self) -> None:
        self.logger.log_egress_attempt(chat_id=456, response_id="r1", attempt=1, trace_id="t4")

    def test_log_egress_success_does_not_raise(self) -> None:
        self.logger.log_egress_success(chat_id=456, response_id="r1", attempts=1, trace_id="t5")

    def test_log_egress_retry_after_does_not_raise(self) -> None:
        self.logger.log_egress_retry_after(
            chat_id=456, response_id="r1", attempt=1, retry_after=5.0, trace_id="t6"
        )

    def test_log_egress_network_error_does_not_raise(self) -> None:
        self.logger.log_egress_network_error(
            chat_id=456, response_id="r1", attempt=2, error="timeout", trace_id="t7"
        )

    def test_log_egress_api_error_does_not_raise(self) -> None:
        self.logger.log_egress_api_error(
            chat_id=456, response_id="r1", attempt=1, error="bad_request", trace_id="t8"
        )

    def test_log_egress_failure_does_not_raise(self) -> None:
        self.logger.log_egress_failure(
            chat_id=456, response_id="r1", attempts=3, error="all_failed", trace_id="t9"
        )

    def test_all_methods_call_structlog(self) -> None:
        mock_logger = MagicMock()
        with patch("assistant.channels.telegram.reliability.audit.logger", mock_logger):
            audit = ChannelAuditLogger()
            audit.log_ingress_authorized(user_id=1, event_type="text", trace_id="x")
            audit.log_ingress_blocked(user_id=2, reason="blocked", trace_id="x")
            audit.log_ingress_throttled(user_id=3, count=5, limit=5, trace_id="x")
            audit.log_egress_attempt(chat_id=4, response_id="r", attempt=1, trace_id="x")
            audit.log_egress_success(chat_id=4, response_id="r", attempts=1, trace_id="x")
            audit.log_egress_retry_after(
                chat_id=4, response_id="r", attempt=1, retry_after=1.0, trace_id="x"
            )
            audit.log_egress_network_error(
                chat_id=4, response_id="r", attempt=1, error="e", trace_id="x"
            )
            audit.log_egress_api_error(
                chat_id=4, response_id="r", attempt=1, error="e", trace_id="x"
            )
            audit.log_egress_failure(
                chat_id=4, response_id="r", attempts=3, error="e", trace_id="x"
            )
        assert mock_logger.info.call_count == 3  # authorized, attempt, success
        assert (
            mock_logger.warning.call_count == 5
        )  # blocked, throttled, retry_after, network_error, api_error
        assert mock_logger.error.call_count == 1
