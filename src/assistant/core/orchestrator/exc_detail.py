"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Shared helper for extracting structured cause details from chained exceptions.
"""

from pydantic import ValidationError


def extract_cause_detail(exc: BaseException) -> tuple[str, str]:
    """Return ``(cause_type, cause_detail)`` for the chained cause of *exc*.

    Provides a consistent representation for structured logging:
    - ``cause_type``: class name of ``exc.__cause__``, or ``"unknown"``.
    - ``cause_detail``: human-readable error body.  For
      :class:`pydantic.ValidationError` the compact ``errors()`` list is used;
      for everything else, ``str(cause)``.
    """
    cause = exc.__cause__
    if cause is None:
        return "unknown", ""
    cause_type = type(cause).__name__
    if isinstance(cause, ValidationError):
        cause_detail = str(cause.errors(include_url=False))
    else:
        cause_detail = str(cause)
    return cause_type, cause_detail
