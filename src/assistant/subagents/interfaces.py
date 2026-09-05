"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Interfaces for delegation coordinator and backend adapters.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from assistant.store.models import TaskRecord
from assistant.subagents.contracts import DelegationResult, DelegationRun


class DelegationBackendAdapterInterface(ABC):
    """Backend adapter interface for delegated execution."""

    @property
    @abstractmethod
    def backend_id(self) -> str:
        """Stable backend identifier used in workflow routing."""

    @abstractmethod
    async def execute(self, request: DelegationRun) -> DelegationResult:
        """Execute delegated task and return normalized output."""

    @property
    def supports_relay(self) -> bool:
        """Whether this backend supports per-task AskUserQuestion relay registration."""
        return False

    def register_relay(
        self,
        task_id: str,
        relay: Callable[[str, list[str]], Awaitable[str]],
    ) -> None:
        """Register a per-task question relay. No-op unless overridden."""
        return

    def unregister_relay(self, task_id: str) -> None:
        """Remove the per-task question relay. No-op unless overridden."""
        return


class DelegationCoordinatorInterface(ABC):
    """Coordinator interface used by tools and API handlers."""

    @abstractmethod
    async def start(self) -> None:
        """Start background coordinator tasks."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop background coordinator tasks."""

    @abstractmethod
    async def enqueue_from_tool(
        self,
        *,
        session_id: str,
        turn_id: str,
        trace_id: str,
        user_id: str | None,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate and enqueue a delegated task from an agent tool call."""

    @abstractmethod
    async def get_task(self, task_id: str, *, session_id: str | None = None) -> TaskRecord | None:
        """Fetch a task by id.

        When ``session_id`` is given, a task belonging to a different session
        is treated as not found.
        """

    @abstractmethod
    async def cancel_task(
        self, task_id: str, *, session_id: str | None = None
    ) -> TaskRecord | None:
        """Cancel a pending or running task; no-op on an already-terminal task.

        Returns the task's current record, or None if the task does not exist
        (or does not belong to ``session_id``, when given).
        """
