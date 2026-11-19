"""
Component ID: CMP_API_AGENT_NODE_INGRESS

Interfaces of the agent-node ingress persistence layer.
"""

from abc import ABC, abstractmethod

from assistant.agent_node.models import IdempotencyRecord, JobRecord, OutboxEntry


class AgentNodeStoreInterface(ABC):
    """Persistence for jobs, idempotency records, the webhook outbox and webhook secrets."""

    @abstractmethod
    async def save_job(self, job: JobRecord) -> None: ...

    @abstractmethod
    async def delete_job(self, job_id: str) -> None: ...

    @abstractmethod
    async def delete_idempotency(self, key: str) -> None: ...

    @abstractmethod
    async def get_job(self, job_id: str) -> JobRecord | None: ...

    @abstractmethod
    async def list_unfinalized_jobs(self) -> list[JobRecord]: ...

    @abstractmethod
    async def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        """Return the live (non-expired) record for ``key``."""

    @abstractmethod
    async def save_idempotency(self, record: IdempotencyRecord) -> None: ...

    @abstractmethod
    async def save_outbox(self, entry: OutboxEntry) -> None: ...

    @abstractmethod
    async def get_outbox(self, job_id: str) -> OutboxEntry | None: ...

    @abstractmethod
    async def list_pending_outbox(self) -> list[OutboxEntry]: ...

    @abstractmethod
    async def save_secret(self, job_id: str, secret: str) -> None: ...

    @abstractmethod
    async def get_secret(self, job_id: str) -> str | None: ...

    @abstractmethod
    async def delete_secret(self, job_id: str) -> None: ...

    @abstractmethod
    async def cleanup_expired(self, retention_seconds: int) -> int:
        """Drop idempotency records past their TTL and finished jobs/outbox past retention."""
