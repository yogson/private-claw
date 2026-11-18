"""
Component ID: CMP_CORE_CONFIRMATION_SERVICE

In-memory confirmation service: request (with dedupe and one-pending-per-session),
idempotent resolve (per-id lock + compare-and-set), lazy expiry, and history
recording of every final state. State is intentionally not persisted; after a
restart unknown confirmation ids resolve as "expired".
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog

from assistant.core.confirmation.handlers import ConfirmationHandlerRegistry
from assistant.core.confirmation.history import ConfirmationRecorderInterface
from assistant.core.confirmation.models import (
    ConfirmationDecision,
    ConfirmationDraft,
    ConfirmationOutcome,
    ConfirmationRequest,
    ConfirmationRequestResult,
    ConfirmationResolveResult,
    ConfirmationStatus,
    ConfirmationUiRef,
)

logger = structlog.get_logger(__name__)

DEFAULT_CONFIRMATION_TTL_SECONDS = 24 * 3600
_EXPIRED_DETAIL = "Confirmation expired before the user answered."
_REJECTED_DETAIL = "User rejected the proposal."


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ConfirmationService:
    """Tracks confirmation requests and applies user decisions via per-kind handlers."""

    def __init__(
        self,
        handlers: ConfirmationHandlerRegistry,
        recorder: ConfirmationRecorderInterface | None = None,
        ttl_seconds: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._handlers = handlers
        self._recorder = recorder
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock
        self._requests: dict[str, ConfirmationRequest] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def request(self, session_id: str, draft: ConfirmationDraft) -> ConfirmationRequestResult:
        """Create a pending confirmation, or return the existing one with the same dedupe_key.

        At most one confirmation may be pending per session: a different proposal
        while one is pending returns ``blocked_by`` and creates nothing.
        """
        await self._expire_due()
        current = self.pending_for_session(session_id)
        if current is not None:
            if current.kind == draft.kind and current.dedupe_key == draft.dedupe_key:
                return ConfirmationRequestResult(request=current, created=False)
            return ConfirmationRequestResult(blocked_by=current)
        now = self._clock()
        request = ConfirmationRequest(
            confirmation_id=uuid.uuid4().hex[:16],
            kind=draft.kind,
            session_id=session_id,
            origin_tool_call_id=draft.origin_tool_call_id,
            payload=draft.payload,
            summary_for_user=draft.summary_for_user,
            dedupe_key=draft.dedupe_key,
            created_at=now,
            expires_at=now + self._ttl,
        )
        self._prune_resolved(now)
        self._requests[request.confirmation_id] = request
        logger.info(
            "confirmation.requested",
            confirmation_id=request.confirmation_id,
            kind=request.kind,
            session_id=session_id,
        )
        return ConfirmationRequestResult(request=request, created=True)

    def get(self, confirmation_id: str) -> ConfirmationRequest | None:
        """Return a request by id (any status), or None if unknown."""
        return self._requests.get(confirmation_id)

    def pending_for_session(self, session_id: str) -> ConfirmationRequest | None:
        """Return the pending, non-expired request of a session, if any."""
        now = self._clock()
        for request in self._requests.values():
            if request.session_id == session_id and request.is_pending and request.expires_at > now:
                return request
        return None

    async def resolve(
        self,
        confirmation_id: str,
        decision: ConfirmationDecision,
        *,
        user_id: str | None = None,
        ui_ref: ConfirmationUiRef | None = None,
    ) -> ConfirmationResolveResult:
        """Apply a user decision exactly once; repeated or racing calls are no-ops."""
        request = self._requests.get(confirmation_id)
        if request is None:
            return ConfirmationResolveResult()
        async with self._lock_for(confirmation_id):
            if ui_ref is not None and request.ui_ref is None:
                request.ui_ref = ui_ref
            if not request.is_pending:
                return ConfirmationResolveResult(request=request, changed=False)
            if request.expires_at <= self._clock():
                await self._finish(request, ConfirmationStatus.EXPIRED, _expired_outcome())
                return ConfirmationResolveResult(request=request, changed=False)
            if decision == ConfirmationDecision.REJECT:
                outcome = ConfirmationOutcome(ok=False, status="rejected", detail=_REJECTED_DETAIL)
                await self._finish(request, ConfirmationStatus.REJECTED, outcome)
            else:
                outcome = await self._apply(request, user_id)
                status = ConfirmationStatus.APPROVED if outcome.ok else ConfirmationStatus.FAILED
                await self._finish(request, status, outcome)
            return ConfirmationResolveResult(request=request, changed=True)

    async def discard(self, confirmation_id: str, detail: str) -> None:
        """Mark a still-pending request failed (e.g. its buttons could not be delivered)."""
        request = self._requests.get(confirmation_id)
        if request is None:
            return
        async with self._lock_for(confirmation_id):
            if request.is_pending:
                outcome = ConfirmationOutcome(ok=False, status="not_delivered", detail=detail)
                await self._finish(request, ConfirmationStatus.FAILED, outcome)

    async def _apply(
        self, request: ConfirmationRequest, user_id: str | None
    ) -> ConfirmationOutcome:
        handler = self._handlers.get(request.kind)
        if handler is None:
            return ConfirmationOutcome(
                ok=False, status="no_handler", detail=f"No handler for kind '{request.kind}'."
            )
        try:
            return await handler.apply(request, user_id=user_id)
        except Exception as exc:
            logger.warning(
                "confirmation.apply_failed",
                confirmation_id=request.confirmation_id,
                kind=request.kind,
                error=str(exc),
            )
            return ConfirmationOutcome(ok=False, status="error", detail=f"Apply failed: {exc}")

    async def _finish(
        self,
        request: ConfirmationRequest,
        status: ConfirmationStatus,
        outcome: ConfirmationOutcome,
    ) -> None:
        request.status = status
        request.result = outcome
        request.resolved_at = self._clock()
        logger.info(
            "confirmation.resolved",
            confirmation_id=request.confirmation_id,
            kind=request.kind,
            session_id=request.session_id,
            status=status.value,
            outcome_status=outcome.status,
        )
        if self._recorder is None:
            return
        try:
            await self._recorder.record_resolution(request)
        except Exception as exc:
            logger.warning(
                "confirmation.record_failed",
                confirmation_id=request.confirmation_id,
                error=str(exc),
            )

    async def _expire_due(self) -> None:
        now = self._clock()
        due = [r for r in self._requests.values() if r.is_pending and r.expires_at <= now]
        for request in due:
            async with self._lock_for(request.confirmation_id):
                if request.is_pending:
                    await self._finish(request, ConfirmationStatus.EXPIRED, _expired_outcome())

    def _prune_resolved(self, now: datetime) -> None:
        # Resolved requests are kept for one TTL so repeated taps still get an exact answer.
        stale = [
            cid
            for cid, r in self._requests.items()
            if not r.is_pending and r.resolved_at is not None and r.resolved_at + self._ttl <= now
        ]
        for cid in stale:
            self._requests.pop(cid, None)
            self._locks.pop(cid, None)

    def _lock_for(self, confirmation_id: str) -> asyncio.Lock:
        lock = self._locks.get(confirmation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[confirmation_id] = lock
        return lock


def _expired_outcome() -> ConfirmationOutcome:
    return ConfirmationOutcome(ok=False, status="expired", detail=_EXPIRED_DETAIL)
