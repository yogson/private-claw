"""
Component ID: CMP_OBSERVABILITY_LOGGING

Correlation ID generation and async-context propagation.

Every request/turn must carry a stable trace_id across component boundaries.
Use `set_trace_id` at ingress and `get_trace_id` everywhere downstream.

SessionTraceManager groups all agent turns within a single session under one
Logfire/OpenTelemetry trace by emitting a real root span on the first turn and
reusing its propagation context for every subsequent turn.
"""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

import structlog

_trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="")
_logger = structlog.get_logger(__name__)


def generate_trace_id() -> str:
    """Generate a new unique trace ID (UUID4 hex string)."""
    return uuid.uuid4().hex


def set_trace_id(trace_id: str) -> Token[str]:
    """Set the current trace ID in the async context.

    Returns a Token that can be used to reset the context variable
    to its previous value via `reset_trace_id`.
    """
    return _trace_id_ctx.set(trace_id)


def reset_trace_id(token: Token[str]) -> None:
    """Reset the trace ID context variable to its previous value."""
    _trace_id_ctx.reset(token)


def get_trace_id_from_context() -> str:
    """Return the current trace ID from context if set, else empty string. Does not mutate."""
    return _trace_id_ctx.get()


def get_trace_id() -> str:
    """Return the current trace ID, generating a new one if none is set."""
    current = _trace_id_ctx.get()
    if not current:
        new_id = generate_trace_id()
        _trace_id_ctx.set(new_id)
        return new_id
    return current


class SessionTraceManager:
    """Groups all orchestrator turns of a session under a single Logfire trace.

    On the **first** turn the manager opens a real ``logfire.span`` (the session
    root) and captures its W3C trace-context via ``logfire.get_context()``.
    Every **subsequent** turn restores that context with
    ``logfire.attach_context()`` so its spans become children of the same root.

    The session root span is ended immediately after the first turn completes,
    but Logfire still recognises it as the parent for later child spans.
    """

    def __init__(self) -> None:
        self._contexts: dict[str, dict[str, str]] = {}

    @contextmanager
    def session_trace(
        self,
        session_id: str,
        turn_id: str,
    ) -> Generator[None, None, None]:
        """Context-manager that places the current turn inside the session trace."""
        try:
            import logfire
        except ImportError:
            yield
            return

        stored_ctx = self._contexts.get(session_id)
        if stored_ctx is not None:
            yield from self._continue_session(logfire, stored_ctx, session_id, turn_id)
        else:
            yield from self._start_session(logfire, session_id, turn_id)

    def drop_session(self, session_id: str) -> None:
        """Remove stored trace context for a session (e.g. on /reset)."""
        self._contexts.pop(session_id, None)

    def _start_session(
        self,
        logfire: Any,
        session_id: str,
        turn_id: str,
    ) -> Generator[None, None, None]:
        with logfire.span(
            "session {session_id}",
            session_id=session_id,
        ):
            self._contexts[session_id] = logfire.get_context()
            with logfire.span(
                "orchestrator turn {turn_id}",
                turn_id=turn_id,
                session_id=session_id,
            ):
                yield

    def _continue_session(
        self,
        logfire: Any,
        stored_ctx: dict[str, str],
        session_id: str,
        turn_id: str,
    ) -> Generator[None, None, None]:
        with logfire.attach_context(stored_ctx), logfire.span(
            "orchestrator turn {turn_id}",
            turn_id=turn_id,
            session_id=session_id,
        ):
            yield
