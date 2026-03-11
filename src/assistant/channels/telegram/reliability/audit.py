"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Structured channel audit logger for Telegram ingress and egress events.
Emits consistent log records for authorization, throttling, and delivery telemetry.
"""

import structlog

logger = structlog.get_logger(__name__)


class ChannelAuditLogger:
    """
    Emits structured audit events for Telegram channel operations.

    All methods emit log entries with a stable "telegram.audit.*" event key
    and consistent fields for security review, incident reconstruction, and
    retry telemetry.
    """

    def log_ingress_authorized(self, user_id: int, event_type: str, trace_id: str) -> None:
        logger.info(
            "telegram.audit.ingress.authorized",
            user_id=user_id,
            event_type=event_type,
            trace_id=trace_id,
        )

    def log_ingress_blocked(self, user_id: int, reason: str, trace_id: str) -> None:
        logger.warning(
            "telegram.audit.ingress.blocked",
            user_id=user_id,
            reason=reason,
            trace_id=trace_id,
        )

    def log_ingress_throttled(self, user_id: int, count: int, limit: int, trace_id: str) -> None:
        logger.warning(
            "telegram.audit.ingress.throttled",
            user_id=user_id,
            count=count,
            limit=limit,
            trace_id=trace_id,
        )

    def log_egress_attempt(
        self, chat_id: int, response_id: str, attempt: int, trace_id: str
    ) -> None:
        logger.info(
            "telegram.audit.egress.attempt",
            chat_id=chat_id,
            response_id=response_id,
            attempt=attempt,
            trace_id=trace_id,
        )

    def log_egress_success(
        self, chat_id: int, response_id: str, attempts: int, trace_id: str
    ) -> None:
        logger.info(
            "telegram.audit.egress.success",
            chat_id=chat_id,
            response_id=response_id,
            attempts=attempts,
            trace_id=trace_id,
        )

    def log_egress_retry_after(
        self,
        chat_id: int,
        response_id: str,
        attempt: int,
        retry_after: float,
        trace_id: str,
    ) -> None:
        logger.warning(
            "telegram.audit.egress.retry_after",
            chat_id=chat_id,
            response_id=response_id,
            attempt=attempt,
            retry_after=retry_after,
            trace_id=trace_id,
        )

    def log_egress_network_error(
        self, chat_id: int, response_id: str, attempt: int, error: str, trace_id: str
    ) -> None:
        logger.warning(
            "telegram.audit.egress.network_error",
            chat_id=chat_id,
            response_id=response_id,
            attempt=attempt,
            error=error,
            trace_id=trace_id,
        )

    def log_egress_api_error(
        self, chat_id: int, response_id: str, attempt: int, error: str, trace_id: str
    ) -> None:
        logger.warning(
            "telegram.audit.egress.api_error",
            chat_id=chat_id,
            response_id=response_id,
            attempt=attempt,
            error=error,
            trace_id=trace_id,
        )

    def log_egress_failure(
        self,
        chat_id: int,
        response_id: str,
        attempts: int,
        error: str,
        trace_id: str,
    ) -> None:
        logger.error(
            "telegram.audit.egress.failure",
            chat_id=chat_id,
            response_id=response_id,
            attempts=attempts,
            error=error,
            trace_id=trace_id,
        )
