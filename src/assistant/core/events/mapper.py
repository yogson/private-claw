"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Maps a channel-produced event payload to the canonical OrchestratorEvent
(INT_ORCH_EVENT_INPUT).

The mapper accepts any Pydantic model whose fields satisfy the OrchestratorEvent
schema, avoiding a hard import dependency from core back into any specific channel
package.  Scheduler events are constructed directly via OrchestratorEvent; this
mapper covers the channel-adapter path only.
"""

from typing import Any

from pydantic import BaseModel

from assistant.core.events.models import OrchestratorEvent


class NormalizedEventMapper:
    """Converts a channel event model into a canonical OrchestratorEvent."""

    def map(self, event: BaseModel) -> OrchestratorEvent:
        """
        Map any Pydantic event model to OrchestratorEvent.

        The source model must carry all required OrchestratorEvent fields.
        The scheduler field is always None for channel-originated events.
        """
        payload: dict[str, Any] = event.model_dump()
        payload["scheduler"] = None
        return OrchestratorEvent.model_validate(payload)
