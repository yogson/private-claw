"""Tests for CMP_CORE_CONFIRMATION_SERVICE ConfirmationService."""

import asyncio
from datetime import UTC, datetime, timedelta

from assistant.core.confirmation import (
    ConfirmationDecision,
    ConfirmationDraft,
    ConfirmationHandlerRegistry,
    ConfirmationOutcome,
    ConfirmationRequest,
    ConfirmationService,
    ConfirmationStatus,
)

SESSION = "tg:1"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class _Handler:
    kind = "memory_update"

    def __init__(self, outcome: ConfirmationOutcome | None = None, delay: float = 0.0) -> None:
        self.calls = 0
        self._outcome = outcome or ConfirmationOutcome(ok=True, status="written", detail="ok")
        self._delay = delay

    async def apply(
        self, request: ConfirmationRequest, *, user_id: str | None
    ) -> ConfirmationOutcome:
        self.calls += 1
        await asyncio.sleep(self._delay)
        return self._outcome


class _Recorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, ConfirmationStatus]] = []

    async def record_resolution(self, request: ConfirmationRequest) -> None:
        self.records.append((request.confirmation_id, request.status))


def _draft(key: str = "k1") -> ConfirmationDraft:
    return ConfirmationDraft(
        kind="memory_update", payload={}, summary_for_user="summary", dedupe_key=key
    )


def _service(
    handler: _Handler | None = None, recorder: _Recorder | None = None, clock: _Clock | None = None
) -> ConfirmationService:
    return ConfirmationService(
        ConfirmationHandlerRegistry([handler or _Handler()]),
        recorder=recorder,
        ttl_seconds=3600,
        clock=clock or _Clock(),
    )


async def test_request_dedupes_same_key_and_blocks_other_proposals() -> None:
    service = _service()
    first = await service.request(SESSION, _draft("k1"))
    again = await service.request(SESSION, _draft("k1"))
    other = await service.request(SESSION, _draft("k2"))
    elsewhere = await service.request("tg:2", _draft("k2"))

    assert first.created and first.request is not None
    assert again.request is first.request and not again.created
    assert other.request is None and other.blocked_by is first.request
    assert elsewhere.created


async def test_resolve_applies_once_and_is_idempotent() -> None:
    handler, recorder = _Handler(), _Recorder()
    service = _service(handler, recorder)
    req = (await service.request(SESSION, _draft())).request
    assert req is not None

    first = await service.resolve(req.confirmation_id, ConfirmationDecision.APPROVE)
    second = await service.resolve(req.confirmation_id, ConfirmationDecision.REJECT)

    assert first.changed and not second.changed
    assert second.request is not None and second.request.status == ConfirmationStatus.APPROVED
    assert handler.calls == 1
    assert recorder.records == [(req.confirmation_id, ConfirmationStatus.APPROVED)]
    # The session slot is free again.
    assert (await service.request(SESSION, _draft("k2"))).created


async def test_concurrent_approve_and_reject_resolve_exactly_once() -> None:
    handler, recorder = _Handler(delay=0.01), _Recorder()
    service = _service(handler, recorder)
    req = (await service.request(SESSION, _draft())).request
    assert req is not None

    results = await asyncio.gather(
        service.resolve(req.confirmation_id, ConfirmationDecision.APPROVE),
        service.resolve(req.confirmation_id, ConfirmationDecision.REJECT),
    )

    assert [r.changed for r in results] == [True, False]
    assert req.status == ConfirmationStatus.APPROVED
    assert handler.calls == 1
    assert len(recorder.records) == 1


async def test_failed_apply_marks_request_failed() -> None:
    handler = _Handler(ConfirmationOutcome(ok=False, status="rejected_invalid", detail="bad"))
    service = _service(handler)
    req = (await service.request(SESSION, _draft())).request
    assert req is not None

    await service.resolve(req.confirmation_id, ConfirmationDecision.APPROVE)

    assert req.status == ConfirmationStatus.FAILED
    assert req.detail == "bad"


async def test_handler_exception_is_reported_as_failed() -> None:
    class _Boom(_Handler):
        async def apply(
            self, request: ConfirmationRequest, *, user_id: str | None
        ) -> ConfirmationOutcome:
            raise RuntimeError("mem0 down")

    service = _service(_Boom())
    req = (await service.request(SESSION, _draft())).request
    assert req is not None

    result = await service.resolve(req.confirmation_id, ConfirmationDecision.APPROVE)

    assert result.request is not None and result.request.status == ConfirmationStatus.FAILED
    assert "mem0 down" in result.request.detail


async def test_expired_request_is_not_applied_and_is_recorded() -> None:
    clock, handler, recorder = _Clock(), _Handler(), _Recorder()
    service = _service(handler, recorder, clock)
    req = (await service.request(SESSION, _draft())).request
    assert req is not None
    clock.now += timedelta(hours=2)

    result = await service.resolve(req.confirmation_id, ConfirmationDecision.APPROVE)

    assert not result.changed
    assert req.status == ConfirmationStatus.EXPIRED
    assert handler.calls == 0
    assert recorder.records == [(req.confirmation_id, ConfirmationStatus.EXPIRED)]
    assert service.pending_for_session(SESSION) is None


async def test_expiry_frees_the_session_slot_on_next_request() -> None:
    clock, recorder = _Clock(), _Recorder()
    service = _service(recorder=recorder, clock=clock)
    old = (await service.request(SESSION, _draft("k1"))).request
    assert old is not None
    clock.now += timedelta(hours=2)

    fresh = await service.request(SESSION, _draft("k2"))

    assert fresh.created
    assert old.status == ConfirmationStatus.EXPIRED
    assert recorder.records == [(old.confirmation_id, ConfirmationStatus.EXPIRED)]


async def test_unknown_id_after_restart_resolves_to_nothing() -> None:
    before_restart = _service()
    req = (await before_restart.request(SESSION, _draft())).request
    assert req is not None
    after_restart = _service()

    result = await after_restart.resolve(req.confirmation_id, ConfirmationDecision.APPROVE)

    assert result.request is None and not result.changed


async def test_discard_marks_pending_failed_and_frees_slot() -> None:
    recorder = _Recorder()
    service = _service(recorder=recorder)
    req = (await service.request(SESSION, _draft())).request
    assert req is not None

    await service.discard(req.confirmation_id, "not delivered")
    await service.discard(req.confirmation_id, "again")

    assert req.status == ConfirmationStatus.FAILED and req.detail == "not delivered"
    assert recorder.records == [(req.confirmation_id, ConfirmationStatus.FAILED)]
    assert service.pending_for_session(SESSION) is None


async def test_recorder_failure_does_not_break_resolution() -> None:
    class _BrokenRecorder:
        async def record_resolution(self, request: ConfirmationRequest) -> None:
            raise OSError("disk full")

    service = ConfirmationService(
        ConfirmationHandlerRegistry([_Handler()]), recorder=_BrokenRecorder()
    )
    req = (await service.request(SESSION, _draft())).request
    assert req is not None

    result = await service.resolve(req.confirmation_id, ConfirmationDecision.REJECT)

    assert result.changed and req.status == ConfirmationStatus.REJECTED
