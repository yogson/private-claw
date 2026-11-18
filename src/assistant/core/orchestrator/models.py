"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Orchestrator result, pending ask, and new-confirmation models.
"""

from dataclasses import dataclass, field

from assistant.core.confirmation.models import ConfirmationRequest


@dataclass
class PendingAskData:
    """Pending ask_question awaiting user selection."""

    question_id: str  # tool_call_id
    question: str
    options: list[dict[str, str]]  # [{"id": "0", "label": "A"}, ...]
    session_id: str
    turn_id: str
    tool_call_id: str


@dataclass
class OrchestratorResult:
    """Result of orchestrator turn execution."""

    text: str
    pending_ask: PendingAskData | None = None
    pending_webapp_buttons: list[dict[str, str]] | None = None
    pending_webapp_message: str | None = None
    confirmations: list[ConfirmationRequest] = field(default_factory=list)
    """Confirmations created during this turn; the channel must render each one."""
