"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Per-turn wiring of capability_load / capability_unload.

The capabilities domain decides (``core/capabilities/session_loading.py``); this module
persists the session's loaded set, exposes newly loaded tools on the next model request of
the running turn, and records what happened for the prompt trace.
"""

from typing import Any

import structlog

from assistant.agent.deps import TurnDeps
from assistant.agent.tools.registry import build_incremental_toolset, build_tool_runtime_params
from assistant.core.capabilities.resolver import EffectiveCapabilities
from assistant.core.capabilities.session_loading import (
    capability_prompt,
    decide_load,
    decide_unload,
    with_loaded,
)
from assistant.core.capabilities.snapshot import CapabilitySnapshot
from assistant.core.config.schemas import RuntimeConfig
from assistant.core.session_context.capability_context import SessionCapabilityContextInterface

logger = structlog.get_logger(__name__)

_LOADED_NOTE = (
    "Follow the capability's instructions in 'prompt' now. Its tools are available from "
    "your next step in this turn and stay loaded for the rest of the session."
)
_UNLOADED_NOTE = (
    "Removed from the session. Tools that were already available stay callable until the "
    "end of this turn; from the next turn the capability's prompt and tools are gone."
)


def log_ignored_loaded(session_id: str, effective: EffectiveCapabilities, revision: int) -> None:
    """Warn once per turn about stored loaded ids that do not take effect."""
    for cap_id, reason in effective.ignored_loaded.items():
        logger.warning(
            "capability.loaded_ignored",
            session_id=session_id,
            capability_id=cap_id,
            reason=reason,
            revision=revision,
            hint="Stays in the session's loaded set; shown as inactive in /capabilities",
        )


class TurnCapabilityLoader:
    """capability_load / capability_unload handlers for one turn of one session.

    ``turn_config`` is the configuration the turn's adapter was built from (base policy +
    the loaded set at turn start). Each call re-reads the stored loaded set, so several
    loads in one turn see each other.
    """

    def __init__(
        self,
        *,
        session_id: str,
        turn_config: RuntimeConfig,
        snapshot: CapabilitySnapshot,
        store: SessionCapabilityContextInterface,
        deps: TurnDeps,
    ) -> None:
        self._session_id = session_id
        self._turn_config = turn_config
        self._snapshot = snapshot
        self._store = store
        self._deps = deps
        self._turn_start_params = set(deps.tool_runtime_params)
        self.events: list[dict[str, Any]] = []

    def attach(self) -> None:
        """Install the handlers on the turn's deps."""
        self._deps.capability_load_handler = self.load
        self._deps.capability_unload_handler = self.unload

    def load(self, capability_id: str, reason: str) -> dict[str, Any]:
        """Handle capability_load: validate, persist, expose the new tools this turn."""
        policy = self._turn_config.capabilities
        definitions = self._snapshot.definitions
        stored = self._store.get_loaded_capabilities(self._session_id)
        decision = decide_load(policy, definitions, stored, capability_id)
        if decision.status != "loaded":
            logger.info(
                "capability.load_rejected",
                session_id=self._session_id,
                capability_id=decision.capability_id,
                status=decision.status,
                revision=self._snapshot.revision,
            )
            self._record(
                "load_rejected", decision.capability_id, status=decision.status, reason=reason
            )
            return {
                "status": decision.status,
                "capability_id": decision.capability_id,
                "reason": decision.reason,
            }

        self._store.set_loaded_capabilities(self._session_id, decision.stored)
        new_tools, new_servers = self._refresh_turn_toolset(decision.stored)
        newly_active = decision.newly_active
        logger.info(
            "capability.loaded",
            session_id=self._session_id,
            capability_id=decision.capability_id,
            activated=newly_active,
            loaded=decision.after.loaded,
            revision=self._snapshot.revision,
        )
        # The model's reason may echo user content: it goes to the session's prompt trace,
        # not to the logs.
        self._record("loaded", decision.capability_id, activated=newly_active, reason=reason)
        return {
            "status": "loaded",
            "capability_id": decision.capability_id,
            "activated": newly_active,
            "prompt": capability_prompt(definitions, newly_active),
            "tools": new_tools,
            "mcp_servers": new_servers,
            "tools_available": "next_step",
            "note": _LOADED_NOTE,
        }

    def unload(self, capability_id: str) -> dict[str, Any]:
        """Handle capability_unload: remove from the stored set (effective next turn)."""
        policy = self._turn_config.capabilities
        stored = self._store.get_loaded_capabilities(self._session_id)
        decision = decide_unload(policy, self._snapshot.definitions, stored, capability_id)
        if decision.status != "unloaded":
            logger.info(
                "capability.unload_rejected",
                session_id=self._session_id,
                capability_id=decision.capability_id,
                status=decision.status,
                revision=self._snapshot.revision,
            )
            self._record("unload_rejected", decision.capability_id, status=decision.status)
            return {
                "status": decision.status,
                "capability_id": decision.capability_id,
                "reason": decision.reason,
            }

        self._store.set_loaded_capabilities(self._session_id, decision.stored)
        self._refresh_turn_toolset(decision.stored)
        deactivated = decision.deactivated
        logger.info(
            "capability.unloaded",
            session_id=self._session_id,
            capability_id=decision.capability_id,
            deactivated=deactivated,
            loaded=decision.after.loaded,
            revision=self._snapshot.revision,
        )
        self._record("unloaded", decision.capability_id, deactivated=deactivated)
        result: dict[str, Any] = {
            "status": "unloaded",
            "capability_id": decision.capability_id,
            "deactivated": deactivated,
            "note": _UNLOADED_NOTE,
        }
        if decision.reason:
            result["reason"] = decision.reason
        return result

    # ------------------------------------------------------------------

    def _refresh_turn_toolset(self, stored: list[str]) -> tuple[list[str], list[str]]:
        """Point the turn's dynamic toolset at the tools loaded during this turn."""
        current = self._turn_config.model_copy(
            update={"capabilities": with_loaded(self._turn_config.capabilities, stored)}
        )
        toolset, new_tools, new_servers = build_incremental_toolset(
            self._turn_config, current, self._snapshot
        )
        self._deps.loaded_toolset = toolset
        # Params for tools that are new in this turn only; tools the adapter already had
        # keep their turn-start params until the next turn.
        params = build_tool_runtime_params(current, self._snapshot)
        for tool_id, tool_params in params.items():
            if tool_id not in self._turn_start_params:
                self._deps.tool_runtime_params[tool_id] = tool_params
        return new_tools, new_servers

    def _record(self, event: str, capability_id: str, **extra: Any) -> None:
        self.events.append({"event": event, "capability_id": capability_id, **extra})
