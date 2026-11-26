"""Tests for SessionTurnQueue: per-session FIFO turn serialization."""

import asyncio

import pytest

from assistant.core.session.turn_queue import SessionTurnQueue
from assistant.store.interfaces import LockAcquisitionError


async def _settle() -> None:
    """Let every ready task run until it blocks."""
    for _ in range(10):
        await asyncio.sleep(0)


class _Turn:
    """A turn that holds its slot until ``release`` is set, recording what happened."""

    def __init__(self, queue: SessionTurnQueue, session_id: str, name: str, log: list[str]) -> None:
        self.queue = queue
        self.session_id = session_id
        self.name = name
        self.log = log
        self.release = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        async with self.queue.slot(self.session_id, trace_id=f"tr-{self.name}", turn_kind="test"):
            self.log.append(f"start:{self.name}")
            await self.release.wait()
            self.log.append(f"end:{self.name}")

    def start(self) -> "_Turn":
        self.task = asyncio.create_task(self._run())
        return self


class TestOrdering:
    async def test_same_session_turns_run_one_at_a_time_in_arrival_order(self) -> None:
        queue = SessionTurnQueue()
        log: list[str] = []
        turns = [_Turn(queue, "s1", name, log) for name in ("a", "b", "c", "d")]
        for turn in turns:
            turn.start()
            await asyncio.sleep(0)  # arrival order a, b, c, d
        await _settle()
        assert log == ["start:a"]
        assert queue.depth("s1") == 4

        # Release out of order: a waiting turn that is told to finish must still wait.
        turns[3].release.set()
        turns[2].release.set()
        await _settle()
        assert log == ["start:a"]

        turns[0].release.set()
        await _settle()
        turns[1].release.set()
        await asyncio.gather(*(t.task for t in turns if t.task is not None))
        assert log == [
            "start:a",
            "end:a",
            "start:b",
            "end:b",
            "start:c",
            "end:c",
            "start:d",
            "end:d",
        ]

    async def test_different_sessions_do_not_wait_for_each_other(self) -> None:
        queue = SessionTurnQueue()
        log: list[str] = []
        a = _Turn(queue, "s1", "a", log).start()
        b = _Turn(queue, "s2", "b", log).start()
        await _settle()
        assert sorted(log) == ["start:a", "start:b"]
        assert len(queue) == 2
        b.release.set()
        await _settle()
        assert log[-1] == "end:b"
        a.release.set()
        await asyncio.gather(a.task, b.task)  # type: ignore[arg-type]

    async def test_newcomer_does_not_overtake_a_woken_waiter(self) -> None:
        queue = SessionTurnQueue()
        log: list[str] = []
        async with queue.slot("s1", trace_id="tr-a", turn_kind="test"):
            b = _Turn(queue, "s1", "b", log).start()
            await _settle()
        # The slot has just been handed to b, which has not resumed yet; c arrives now.
        c = _Turn(queue, "s1", "c", log).start()
        await _settle()
        assert log == ["start:b"]
        c.release.set()
        b.release.set()
        await asyncio.gather(b.task, c.task)  # type: ignore[arg-type]
        assert log == ["start:b", "end:b", "start:c", "end:c"]


class TestRelease:
    async def test_slot_is_released_after_an_exception(self) -> None:
        queue = SessionTurnQueue()
        with pytest.raises(RuntimeError):
            async with queue.slot("s1", trace_id="tr", turn_kind="test"):
                raise RuntimeError("boom")
        assert len(queue) == 0
        async with asyncio.timeout(1):
            async with queue.slot("s1", trace_id="tr", turn_kind="test"):
                pass

    async def test_exception_in_running_turn_lets_the_next_one_run(self) -> None:
        queue = SessionTurnQueue()
        gate = asyncio.Event()

        async def failing() -> None:
            async with queue.slot("s1", trace_id="tr-a", turn_kind="test"):
                await gate.wait()
                raise RuntimeError("boom")

        ran: list[str] = []

        async def next_turn() -> None:
            async with queue.slot("s1", trace_id="tr-b", turn_kind="test"):
                ran.append("b")

        first = asyncio.create_task(failing())
        await _settle()
        second = asyncio.create_task(next_turn())
        await _settle()
        assert ran == []
        gate.set()
        with pytest.raises(RuntimeError):
            await first
        async with asyncio.timeout(1):
            await second
        assert ran == ["b"]
        assert len(queue) == 0

    async def test_cancelling_the_running_turn_lets_the_next_one_run(self) -> None:
        """/stop cancels the running turn; the queued turn behind it starts."""
        queue = SessionTurnQueue()
        log: list[str] = []
        a = _Turn(queue, "s1", "a", log).start()
        b = _Turn(queue, "s1", "b", log).start()
        await _settle()
        a.task.cancel()  # type: ignore[union-attr]
        with pytest.raises(asyncio.CancelledError):
            await a.task  # type: ignore[misc]
        await _settle()
        assert log == ["start:a", "start:b"]
        b.release.set()
        await b.task  # type: ignore[misc]
        assert len(queue) == 0

    async def test_cancelled_waiting_turn_does_not_block_the_next_one(self) -> None:
        queue = SessionTurnQueue()
        log: list[str] = []
        a = _Turn(queue, "s1", "a", log).start()
        b = _Turn(queue, "s1", "b", log).start()
        c = _Turn(queue, "s1", "c", log).start()
        await _settle()
        b.task.cancel()  # type: ignore[union-attr]
        with pytest.raises(asyncio.CancelledError):
            await b.task  # type: ignore[misc]
        assert queue.depth("s1") == 2
        a.release.set()
        c.release.set()
        async with asyncio.timeout(1):
            await asyncio.gather(a.task, c.task)  # type: ignore[arg-type]
        assert log == ["start:a", "end:a", "start:c", "end:c"]
        assert len(queue) == 0

    async def test_waiter_cancelled_while_being_woken_passes_the_slot_on(self) -> None:
        queue = SessionTurnQueue()
        log: list[str] = []
        async with queue.slot("s1", trace_id="tr-a", turn_kind="test"):
            b = _Turn(queue, "s1", "b", log).start()
            c = _Turn(queue, "s1", "c", log).start()
            await _settle()
        # The slot has just been handed to b, which has not resumed yet: cancel it now.
        b.task.cancel()  # type: ignore[union-attr]
        with pytest.raises(asyncio.CancelledError):
            await b.task  # type: ignore[misc]
        c.release.set()
        async with asyncio.timeout(1):
            await c.task  # type: ignore[misc]
        assert log == ["start:c", "end:c"]
        assert len(queue) == 0

    async def test_wait_timeout_raises_lock_error_and_keeps_the_queue_moving(self) -> None:
        queue = SessionTurnQueue(wait_timeout_seconds=0.01)
        log: list[str] = []
        a = _Turn(queue, "s1", "a", log).start()
        await _settle()
        with pytest.raises(LockAcquisitionError):
            async with queue.slot("s1", trace_id="tr-b", turn_kind="test"):
                log.append("start:b")
        c = _Turn(queue, "s1", "c", log).start()
        a.release.set()
        c.release.set()
        async with asyncio.timeout(1):
            await asyncio.gather(a.task, c.task)  # type: ignore[arg-type]
        assert log == ["start:a", "end:a", "start:c", "end:c"]


class TestRegistry:
    async def test_idle_sessions_are_removed(self) -> None:
        queue = SessionTurnQueue()
        for i in range(50):
            async with queue.slot(f"s{i}", trace_id="tr", turn_kind="test"):
                assert queue.depth(f"s{i}") == 1
        assert len(queue) == 0
        assert queue.depth("s0") == 0

    async def test_entry_survives_while_a_turn_is_still_queued(self) -> None:
        queue = SessionTurnQueue()
        log: list[str] = []
        a = _Turn(queue, "s1", "a", log).start()
        b = _Turn(queue, "s1", "b", log).start()
        await _settle()
        a.release.set()
        await a.task  # type: ignore[misc]
        assert queue.depth("s1") == 1
        b.release.set()
        await b.task  # type: ignore[misc]
        assert len(queue) == 0
