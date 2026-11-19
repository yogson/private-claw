"""
Component ID: CMP_CORE_CONFIRMATION_SERVICE

In-memory confirmation service: request (with dedupe and one-pending-per-session),
idempotent resolve (per-id lock + compare-and-set), expiry (swept before every
agent turn and on request/resolve), and history recording of every final state.
State is intentionally not persisted; after a restart unknown confirmation ids
resolve as "expired" and an ``expired`` record is written for them: when the channel
can prove the session of a pressed button (record_unknown_expired), and at turn start
for ids that history still shows as pending (reconcile_pending).
"""

import asyncio
from collections.abc import Callable, Iterable
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
    is_confirmation_id,
    new_confirmation_id,
)

logger = structlog.get_logger(__name__)

DEFAULT_CONFIRMATION_TTL_SECONDS = 24 * 3600
# Generic budget for summary_for_user so every kind's prompt fits in one channel
# message (Telegram: 4096) with room for the title and the final status line.
SUMMARY_MAX_CHARS = 3500
_EXPIRED_DETAIL = "Confirmation expired before the user answered."
_REJECTED_DETAIL = "User rejected the proposal."
_UNKNOWN_DETAIL = (
    "The user pressed a button for a confirmation the assistant no longer tracks "
    "(e.g. it was pending when the assistant restarted). Nothing was applied by this press."
)
_ORPHANED_DETAIL = (
    "This confirmation is no longer awaiting an answer (the assistant restarted or the "
    "turn ended before it was resolved), so the user can no longer approve it. Nothing "
    "was applied. Propose it again if it is still needed."
)
UNKNOWN_KIND = "unknown"
# Outcome status of a request discarded because its buttons never (provably) reached the user.
NOT_DELIVERED_OUTCOME_STATUS = "not_delivered"


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

    async def request(
        self, session_id: str, draft: ConfirmationDraft, *, user_id: str | None = None
    ) -> ConfirmationRequestResult:
        """Create a pending confirmation, or return the existing one with the same dedupe_key.

        At most one confirmation may be pending per session: a different proposal
        while one is pending returns ``blocked_by`` and creates nothing. ``user_id`` (the
        turn's user) binds the request: only that user may resolve it.
        """
        await self.expire_due()
        current = self.pending_for_session(session_id)
        if current is not None:
            if current.kind == draft.kind and current.dedupe_key == draft.dedupe_key:
                return ConfirmationRequestResult(request=current, created=False)
            return ConfirmationRequestResult(blocked_by=current)
        now = self._clock()
        request = ConfirmationRequest(
            confirmation_id=new_confirmation_id(),
            kind=draft.kind,
            session_id=session_id,
            user_id=user_id,
            origin_tool_call_id=draft.origin_tool_call_id,
            payload=draft.payload,
            summary_for_user=_cap_summary(draft.summary_for_user),
            dedupe_key=draft.dedupe_key,
            attachments=draft.attachments,
            created_at=now,
            expires_at=now + self._ttl,
        )
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
        """Apply a user decision exactly once; repeated or racing calls are no-ops.

        A decision by another user than the one the request is bound to changes nothing
        (``not_owner``).
        """
        self._prune_resolved(self._clock())
        request = self._requests.get(confirmation_id)
        if request is None:
            return ConfirmationResolveResult()
        if request.user_id is not None and user_id != request.user_id:
            logger.info(
                "confirmation.resolve_not_owner",
                confirmation_id=confirmation_id,
                session_id=request.session_id,
            )
            return ConfirmationResolveResult(request=request, not_owner=True)
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

    async def discard(self, confirmation_id: str, detail: str, *, record: bool = True) -> None:
        """Mark a still-pending request failed (e.g. its buttons could not be delivered).

        ``record=False`` skips the history record: for proposals of failed or cancelled
        turns. Most failures persist no tool call, so the record would relate to nothing
        the agent can see. A ``/stop`` does persist the partial turn, including the
        ``pending_confirmation`` tool result; reconcile_pending then writes the record at
        the start of the next turn, after that cancelled turn.
        """
        request = self._requests.get(confirmation_id)
        if request is None:
            return
        async with self._lock_for(confirmation_id):
            if request.is_pending:
                outcome = ConfirmationOutcome(
                    ok=False, status=NOT_DELIVERED_OUTCOME_STATUS, detail=detail
                )
                await self._finish(request, ConfirmationStatus.FAILED, outcome, record=record)

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
        *,
        record: bool = True,
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
        if record:
            await self._record(request)

    async def _record(self, request: ConfirmationRequest) -> bool:
        """Hand ``request`` to the recorder; True if it was written without error."""
        if self._recorder is None:
            return False
        try:
            await self._recorder.record_resolution(request)
        except Exception as exc:
            logger.warning(
                "confirmation.record_failed",
                confirmation_id=request.confirmation_id,
                error=str(exc),
            )
            return False
        return True

    async def expire_due(self, session_id: str | None = None) -> None:
        """Expire pending requests past their TTL (all sessions, or only ``session_id``).

        Each expiry writes its ``expired`` record, so calling this before a turn
        reads history makes timed-out confirmations visible to that turn.
        """
        now = self._clock()
        self._prune_resolved(now)
        due = [
            r
            for r in self._requests.values()
            if r.is_pending
            and r.expires_at <= now
            and (session_id is None or r.session_id == session_id)
        ]
        for request in due:
            async with self._lock_for(request.confirmation_id):
                if request.is_pending:
                    await self._finish(request, ConfirmationStatus.EXPIRED, _expired_outcome())

    async def record_unknown_expired(
        self, confirmation_id: str, session_id: str, detail: str = _UNKNOWN_DETAIL
    ) -> bool:
        """Write an ``expired`` record for an id this process does not know.

        For ids proven to belong to ``session_id``: a button press whose signature
        proves the session (e.g. a confirmation pending across a restart), or a
        pending id found in that session's own history. The store deduplicates the
        record by its event id, so it is written at most once per id.
        Returns True if the record was written without error.
        """
        if confirmation_id in self._requests or self._recorder is None:
            return False
        now = self._clock()
        stub = ConfirmationRequest(
            confirmation_id=confirmation_id,
            kind=UNKNOWN_KIND,
            session_id=session_id,
            summary_for_user="",
            dedupe_key="",
            status=ConfirmationStatus.EXPIRED,
            created_at=now,
            expires_at=now,
            resolved_at=now,
            result=ConfirmationOutcome(ok=False, status="expired", detail=detail),
        )
        logger.info(
            "confirmation.unknown_expired", confirmation_id=confirmation_id, session_id=session_id
        )
        return await self._record(stub)

    async def reconcile_pending(self, session_id: str, confirmation_ids: Iterable[str]) -> bool:
        """Give every id that ``session_id``'s history shows as pending a final record.

        Called at turn start with the ids whose replayed tool result still says
        ``pending_confirmation`` and that have no resolution record yet
        (unresolved_pending_ids). Ids not in the service's own format are ignored.
        - unknown ids (the process restarted, or the request was pruned): ``expired``
          record;
        - known, already final ids whose record was skipped (``discard(record=False)``)
          or failed to write: their final state is recorded now;
        - still-pending ids of this process: left alone.
        Call ``expire_due(session_id)`` first so timed-out ids are final here.
        Returns True if any record was written, i.e. history should be re-read.
        """
        wrote = False
        for confirmation_id in dict.fromkeys(confirmation_ids):
            if not is_confirmation_id(confirmation_id):
                continue
            request = self._requests.get(confirmation_id)
            if request is None:
                wrote |= await self.record_unknown_expired(
                    confirmation_id, session_id, detail=_ORPHANED_DETAIL
                )
            elif not request.is_pending and request.session_id == session_id:
                wrote |= await self._record(request)
        return wrote

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


def _cap_summary(summary: str) -> str:
    if len(summary) <= SUMMARY_MAX_CHARS:
        return summary
    hidden = len(summary) - SUMMARY_MAX_CHARS
    return summary[:SUMMARY_MAX_CHARS] + f"\n… [truncated: {hidden} more chars not shown]"


def _expired_outcome() -> ConfirmationOutcome:
    return ConfirmationOutcome(ok=False, status="expired", detail=_EXPIRED_DETAIL)
