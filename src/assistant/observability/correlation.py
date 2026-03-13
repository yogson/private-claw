"""
Component ID: CMP_OBSERVABILITY_LOGGING

Correlation ID generation and async-context propagation.

Every request/turn must carry a stable trace_id across component boundaries.
Use `set_trace_id` at ingress and `get_trace_id` everywhere downstream.
"""

import uuid
from contextvars import ContextVar, Token

_trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="")


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
