"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Telegram user allowlist enforcement.
Blocks unauthorized users before any business logic executes.
"""

import structlog

logger = structlog.get_logger(__name__)


class UnauthorizedUserError(Exception):
    """Raised when a Telegram user is not in the configured allowlist."""

    def __init__(self, user_id: int) -> None:
        super().__init__(f"Unauthorized Telegram user: {user_id}")
        self.user_id = user_id


class AllowlistGuard:
    """
    Enforces the Telegram user allowlist policy (CMP_ERROR_CHANNEL_UNAUTHORIZED).

    Rejects and logs any interaction from users not in the configured allowlist.
    """

    def __init__(self, allowed_user_ids: list[int]) -> None:
        self._allowed: frozenset[int] = frozenset(allowed_user_ids)

    def is_allowed(self, user_id: int) -> bool:
        """Return True if user_id is in the allowlist."""
        return user_id in self._allowed

    def require_allowed(self, user_id: int) -> None:
        """
        Assert that user_id is in the allowlist.

        Raises UnauthorizedUserError and emits an audit warning if not.
        """
        if not self.is_allowed(user_id):
            logger.warning(
                "telegram.allowlist.rejected",
                user_id=user_id,
                error_code="CMP_ERROR_CHANNEL_UNAUTHORIZED",
            )
            raise UnauthorizedUserError(user_id)
