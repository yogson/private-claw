"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Provider-agnostic delegation contracts for single-run delegated tasks.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DelegationTerminalStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class DelegationRun(BaseModel):
    """Resolved payload passed to a backend adapter."""

    task_id: str
    objective: str
    model_id: str
    timeout_seconds: int = Field(default=300, ge=1)
    max_turns: int = Field(default=25, ge=1)
    backend_params: dict[str, Any] = Field(default_factory=dict)


class DelegationResult(BaseModel):
    """Normalized backend result."""

    ok: bool
    output_text: str = ""
    artifacts: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class DelegationAcceptResult(BaseModel):
    """Immediate acknowledgement returned by the enqueue API."""

    accepted: bool
    task_id: str
    status: str
    rejection_reason: str | None = None
    expires_at: str | None = None
