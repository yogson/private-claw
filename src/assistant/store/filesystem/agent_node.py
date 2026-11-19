"""
Component ID: CMP_STORE_AGENT_NODE

Filesystem persistence for the agent-node ingress: job records, idempotency
records, the webhook outbox and webhook secrets (kept apart, mode 0600).
"""

import asyncio
import contextlib
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from assistant.agent_node.interfaces import AgentNodeStoreInterface
from assistant.agent_node.models import IdempotencyRecord, JobRecord, OutboxEntry
from assistant.store.filesystem.atomic import atomic_write, atomic_write_text, ensure_directory

logger = structlog.get_logger(__name__)

_M = TypeVar("_M", bound=BaseModel)


def _safe_name(raw: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in raw)


class FilesystemAgentNodeStore(AgentNodeStoreInterface):
    """Layout: ``<root>/{jobs,idempotency,outbox,secrets}/<name>.json``."""

    def __init__(self, root: Path) -> None:
        self._jobs_dir = root / "jobs"
        self._idem_dir = root / "idempotency"
        self._outbox_dir = root / "outbox"
        self._secrets_dir = root / "secrets"
        for d in (self._jobs_dir, self._idem_dir, self._outbox_dir, self._secrets_dir):
            ensure_directory(d)
        # Serializes read-modify-write cycles on the same store (single process).
        self._lock = asyncio.Lock()

    @staticmethod
    def _read(path: Path, model: type[_M]) -> _M | None:
        try:
            return model.model_validate_json(path.read_text())
        except FileNotFoundError:
            return None
        except (ValidationError, ValueError, OSError):
            logger.warning("agent_node.store.unreadable_record", path=str(path))
            return None

    def _job_path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{_safe_name(job_id)}.json"

    def _outbox_path(self, job_id: str) -> Path:
        return self._outbox_dir / f"{_safe_name(job_id)}.json"

    def _idem_path(self, key: str) -> Path:
        return self._idem_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

    def _secret_path(self, job_id: str) -> Path:
        return self._secrets_dir / _safe_name(job_id)

    async def save_job(self, job: JobRecord) -> None:
        await atomic_write_text(self._job_path(job.job_id), job.model_dump_json())

    async def delete_job(self, job_id: str) -> None:
        self._job_path(job_id).unlink(missing_ok=True)

    async def delete_idempotency(self, key: str) -> None:
        self._idem_path(key).unlink(missing_ok=True)

    async def get_job(self, job_id: str) -> JobRecord | None:
        return self._read(self._job_path(job_id), JobRecord)

    async def list_unfinalized_jobs(self) -> list[JobRecord]:
        jobs = []
        for path in self._jobs_dir.glob("*.json"):
            job = self._read(path, JobRecord)
            if job is not None and job.final_view is None:
                jobs.append(job)
        return jobs

    async def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        record = self._read(self._idem_path(key), IdempotencyRecord)
        if record is None or record.key != key:
            return None
        created = datetime.fromisoformat(record.created_at)
        if datetime.now(UTC) >= created + timedelta(seconds=record.ttl_seconds):
            return None
        return record

    async def save_idempotency(self, record: IdempotencyRecord) -> None:
        await atomic_write_text(self._idem_path(record.key), record.model_dump_json())

    async def save_outbox(self, entry: OutboxEntry) -> None:
        await atomic_write_text(self._outbox_path(entry.job_id), entry.model_dump_json())

    async def get_outbox(self, job_id: str) -> OutboxEntry | None:
        return self._read(self._outbox_path(job_id), OutboxEntry)

    async def list_pending_outbox(self) -> list[OutboxEntry]:
        entries = []
        for path in self._outbox_dir.glob("*.json"):
            entry = self._read(path, OutboxEntry)
            if entry is not None and entry.state == "pending":
                entries.append(entry)
        return entries

    async def save_secret(self, job_id: str, secret: str) -> None:
        path = self._secret_path(job_id)
        await atomic_write(path, secret.encode("utf-8"))
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    async def get_secret(self, job_id: str) -> str | None:
        try:
            return self._secret_path(job_id).read_text(encoding="utf-8") or None
        except FileNotFoundError:
            return None

    async def delete_secret(self, job_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._secret_path(job_id).unlink()

    async def cleanup_expired(self, retention_seconds: int) -> int:
        removed = 0
        now = datetime.now(UTC)
        for path in self._idem_dir.glob("*.json"):
            record = self._read(path, IdempotencyRecord)
            created = datetime.fromisoformat(record.created_at) if record else None
            if record is None or (
                created is not None and now >= created + timedelta(seconds=record.ttl_seconds)
            ):
                path.unlink(missing_ok=True)
                removed += 1
        cutoff = now - timedelta(seconds=retention_seconds)
        for path in self._jobs_dir.glob("*.json"):
            job = self._read(path, JobRecord)
            if job is None or job.final_view is None:
                continue
            if datetime.fromisoformat(job.created_at) >= cutoff:
                continue
            outbox = self._read(self._outbox_path(job.job_id), OutboxEntry)
            if outbox is not None and outbox.state == "pending":
                continue
            path.unlink(missing_ok=True)
            self._outbox_path(job.job_id).unlink(missing_ok=True)
            removed += 1
        return removed
