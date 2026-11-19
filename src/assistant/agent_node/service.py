"""
Component ID: CMP_API_AGENT_NODE_INGRESS

agent-node ingress service: turns ``POST /jobs`` into delegation tasks, derives the
agent-node job view from the task, finalizes jobs exactly once and queues the single
completion webhook.

State machine: a job is *unfinalized* until its task reaches a terminal status and
:meth:`AgentNodeService.finalize` stores ``JobRecord.final_view`` (and the outbox
entry). Finalization is idempotent and reachable from the coordinator's completion
callback, from the cancel/GET/replay paths and from the reconcile loop, so a job is
finalized - and its webhook queued - once, whichever path gets there first and
whether the task finished normally, expired, was cancelled elsewhere or was
interrupted by a restart.
"""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from assistant.agent_node import git_inspect
from assistant.agent_node.idempotency import request_fingerprint
from assistant.agent_node.interfaces import AgentNodeStoreInterface
from assistant.agent_node.models import (
    AGENT_NODE_SESSION_PREFIX,
    AGENT_NODE_SOURCE,
    DEFAULT_TIMEOUT_SECONDS,
    DeliveryView,
    ErrorKind,
    IdempotencyRecord,
    JobRecord,
    JobResultView,
    JobStatus,
    JobView,
    OutboxEntry,
    RepoContext,
)
from assistant.agent_node.webhook import WebhookSender, encode_body
from assistant.core.config.schemas import AgentNodeConfig, RuntimeConfig
from assistant.store.interfaces import TaskStoreInterface
from assistant.store.models import TaskRecord, TaskStatus
from assistant.subagents.interfaces import DelegationCoordinatorInterface

logger = structlog.get_logger(__name__)

IDEMPOTENCY_CONFLICT_DETAIL = "Idempotency-Key was already used with a different request"
IDEMPOTENCY_EARLIER_DEADLINE_DETAIL = (
    "Idempotency-Key was already used for a job with a later deadline; "
    "the requested deadline is earlier"
)
REASON_DIFFERENT_REQUEST = "idempotency_key_reused_with_different_request"
REASON_EARLIER_DEADLINE = "idempotency_deadline_earlier_than_job"
FILES_UNSUPPORTED_DETAIL = "input files are not supported"
CANCELLED_WHILE_QUEUED = "cancelled while queued; the agent was not started"
CANCELLED_WHILE_RUNNING = "cancelled while running; the agent was stopped"
RETRY_AFTER_SECONDS = 30
_RECONCILE_INTERVAL_SECONDS = 5.0
_CLEANUP_INTERVAL_SECONDS = 3600.0
# A job whose task never appeared (crash between accept steps) is orphaned after this.
_MISSING_TASK_GRACE_SECONDS = 60.0
_RESTART_ERROR_PREFIX = "Task interrupted by process restart"


class JobRejected(Exception):
    """The request cannot be accepted; ``status_code`` / ``detail`` go to the client."""

    def __init__(self, status_code: int, detail: str, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


class IdempotencyConflict(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class DeadlineNotInFuture(Exception):
    """``deadline`` has passed and a new job would have been created."""


@dataclass
class CreateOutcome:
    view: JobView
    replayed: bool


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def build_objective(
    *, user_prompt: str, system_prompt: str | None, repo: RepoContext | None, job_id: str
) -> str:
    """Delegation objective: prompts plus, for a repo job, where and how to work."""
    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt.strip())
    parts.append(user_prompt.strip())
    if repo is not None:
        parts.append(
            "--- Environment ---\n"
            f"Repository: {repo.full_name}\n"
            f"Local checkout (your working directory): {repo.path}\n"
            f"Base branch: {repo.base_branch}\n"
            f"Create a work branch named agent-node/{job_id[:12]} from {repo.base_branch} "
            "and commit your changes there. Do not push, do not open a pull request, "
            "and do not alter other branches. Leave unrelated uncommitted changes untouched."
        )
    return "\n\n".join(parts)


class AgentNodeService:
    """Application service behind the agent-node compatible router."""

    def __init__(
        self,
        *,
        config: AgentNodeConfig,
        runtime_config: RuntimeConfig,
        store: AgentNodeStoreInterface,
        task_store: TaskStoreInterface,
        coordinator: DelegationCoordinatorInterface,
        sender: WebhookSender,
        data_root: Path,
        idempotency_ttl_seconds: int = 86400,
    ) -> None:
        self.config = config
        self._runtime_config = runtime_config
        self._store = store
        self._task_store = task_store
        self._coordinator = coordinator
        self._sender = sender
        self._data_root = data_root
        self._idempotency_ttl = idempotency_ttl_seconds
        self._create_lock = asyncio.Lock()
        self._finalize_lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        """Resume pending deliveries and begin reconciling jobs."""
        self._sender.start()
        self._loop_task = asyncio.create_task(self._reconcile_loop(), name="agent_node.reconcile")

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        await self._sender.stop()

    async def _reconcile_loop(self) -> None:
        last_cleanup = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                await self.reconcile()
                if loop.time() - last_cleanup >= _CLEANUP_INTERVAL_SECONDS:
                    last_cleanup = loop.time()
                    await self._store.cleanup_expired(self._idempotency_ttl)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("agent_node.reconcile.error")
            await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)

    async def reconcile(self) -> None:
        """Finalize jobs whose task ended without a callback; enforce the job deadline."""
        for job in await self._store.list_unfinalized_jobs():
            task = await self._task_store.get(job.job_id)
            if task is not None and task.status not in _TERMINAL:
                if _now() > datetime.fromisoformat(job.deadline):
                    await self._expire(job, task)
                continue
            await self.finalize(job.job_id, task)

    async def _expire(self, job: JobRecord, task: TaskRecord) -> None:
        phase = "queued" if task.status == TaskStatus.PENDING else "running"
        job.timed_out_phase = phase
        await self._store.save_job(job)
        logger.info("agent_node.job.deadline_passed", job_id=job.job_id, phase=phase)
        settled = await self._coordinator.cancel_task(job.job_id)
        await self.finalize(job.job_id, settled)

    # -- create ---------------------------------------------------------------------

    async def create_job(
        self,
        *,
        body_dump: dict[str, Any],
        user_prompt: str,
        system_prompt: str | None,
        repo_full_name: str | None,
        base_branch: str,
        timeout_seconds: int | None,
        requested_deadline: datetime | None,
        model: str | None,
        metadata: dict[str, Any],
        webhook_url: str | None,
        webhook_secret: str | None,
        idempotency_key: str | None,
    ) -> CreateOutcome:
        """Accept a job (or replay the one under ``idempotency_key``).

        ``body_dump`` is the validated body (``model_dump(mode="json")``) used for the
        fingerprint; ``github_token`` is not in it and is never passed here at all.
        """
        repo_cfg = None
        if repo_full_name is not None:
            repo_cfg = self.config.repositories.get(repo_full_name)
            if repo_cfg is None:
                raise JobRejected(422, f"repository {repo_full_name!r} is not configured")
        fingerprint = request_fingerprint(body_dump)
        accepted_at = _now()

        async with self._create_lock:
            if idempotency_key is not None:
                existing = await self._find_existing(idempotency_key)
                if existing is not None:
                    return await self._replay(existing, fingerprint, requested_deadline)
            if requested_deadline is not None and requested_deadline <= accepted_at:
                raise DeadlineNotInFuture

            timeout = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
            deadline = accepted_at + timedelta(seconds=timeout)
            if requested_deadline is not None:
                deadline = min(deadline, requested_deadline)
            run_seconds = max(1, int((deadline - accepted_at).total_seconds()))

            job_id = uuid.uuid4().hex
            repo: RepoContext | None = None
            if repo_cfg is not None and repo_full_name is not None:
                repo_path = str(Path(repo_cfg.path).expanduser())
                repo = RepoContext(
                    full_name=repo_full_name,
                    path=repo_path,
                    base_branch=base_branch,
                    head_before=await git_inspect.head_sha(repo_path),
                )
                workdir = repo_path
            else:
                workdir_path = self._data_root / "agent_node" / "workspaces" / job_id
                await asyncio.to_thread(workdir_path.mkdir, parents=True, exist_ok=True)
                workdir = str(workdir_path)

            accept = await self._coordinator.enqueue_from_tool(
                session_id=f"{AGENT_NODE_SESSION_PREFIX}{job_id}",
                turn_id=job_id,
                trace_id=job_id,
                user_id=None,
                request={
                    "task_id": job_id,
                    "objective": build_objective(
                        user_prompt=user_prompt,
                        system_prompt=system_prompt,
                        repo=repo,
                        job_id=job_id,
                    ),
                    "model_id": model or self.config.default_model,
                    "backend": self.config.backend,
                    "timeout_seconds": run_seconds,
                    "backend_params": {"directory": workdir},
                    "tool_params": self._tool_params(run_seconds),
                    "task_metadata": {
                        "source": AGENT_NODE_SOURCE,
                        "job_id": job_id,
                        "idempotency_key": idempotency_key,
                        "deadline": _iso(deadline),
                        "interactive": False,
                    },
                },
            )
            if not accept.get("accepted"):
                reason = str(accept.get("rejection_reason") or "rejected")
                if accept.get("retryable"):
                    raise JobRejected(503, reason, retry_after=RETRY_AFTER_SECONDS)
                raise JobRejected(422, reason)

            job = JobRecord(
                job_id=job_id,
                idempotency_key=idempotency_key,
                created_at=_iso(accepted_at),
                deadline=_iso(deadline),
                requested_deadline=_iso(requested_deadline) if requested_deadline else None,
                repo_full_name=repo_full_name,
                model=model,
                metadata=metadata,
                repo=repo,
                webhook_url=webhook_url,
                webhook_signed=bool(webhook_url and webhook_secret),
            )
            if webhook_url and webhook_secret:
                await self._store.save_secret(job_id, webhook_secret)
            await self._store.save_job(job)
            if idempotency_key is not None:
                await self._store.save_idempotency(
                    IdempotencyRecord(
                        key=idempotency_key,
                        job_id=job_id,
                        fingerprint=fingerprint,
                        created_at=_iso(accepted_at),
                        ttl_seconds=self._idempotency_ttl,
                    )
                )
        logger.info("agent_node.job.accepted", job_id=job_id, repo=repo_full_name)
        return CreateOutcome(view=await self._view_of(job, None), replayed=False)

    async def _find_existing(self, key: str) -> tuple[IdempotencyRecord, JobRecord] | None:
        record = await self._store.get_idempotency(key)
        if record is None:
            return None
        job = await self._store.get_job(record.job_id)
        return None if job is None else (record, job)

    async def _replay(
        self,
        existing: tuple[IdempotencyRecord, JobRecord],
        fingerprint: str,
        requested_deadline: datetime | None,
    ) -> CreateOutcome:
        record, job = existing
        if record.fingerprint != fingerprint:
            logger.warning("agent_node.idempotency_conflict", existing_job_id=job.job_id)
            raise IdempotencyConflict(REASON_DIFFERENT_REQUEST, IDEMPOTENCY_CONFLICT_DETAIL)
        view = await self.get_view(job.job_id)
        assert view is not None
        if (
            requested_deadline is not None
            and view.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            and requested_deadline < datetime.fromisoformat(job.deadline)
        ):
            raise IdempotencyConflict(REASON_EARLIER_DEADLINE, IDEMPOTENCY_EARLIER_DEADLINE_DETAIL)
        logger.info("agent_node.job.replayed", job_id=job.job_id, status=view.status.value)
        return CreateOutcome(view=view, replayed=True)

    def _tool_params(self, run_seconds: int) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for tool in self._runtime_config.tools.tools:
            if tool.tool_id == "delegate_subagent_task" and tool.default_params is not None:
                params = tool.default_params.model_dump(exclude_none=True)
        ttl = run_seconds + 60
        params["delegation_default_ttl_seconds"] = ttl
        params["delegation_max_ttl_seconds"] = ttl
        return params

    # -- read / cancel -----------------------------------------------------------------

    async def get_view(self, job_id: str) -> JobView | None:
        job = await self._store.get_job(job_id)
        if job is None:
            return None
        task = await self._task_store.get(job_id)
        task_ended = task is None or task.status in _TERMINAL
        if job.final_view is None and task_ended and (task or self._missing_task_expired(job)):
            job = await self.finalize(job_id, task) or job
        return await self._view_of(job, task)

    async def cancel(self, job_id: str) -> JobView | None:
        """Cancel an unfinished job; a finished one is returned unchanged. None: no such job."""
        job = await self._store.get_job(job_id)
        if job is None:
            return None
        if job.final_view is None:
            task = await self._coordinator.cancel_task(job_id)
            await self.finalize(job_id, task)
        return await self.get_view(job_id)

    async def _view_of(self, job: JobRecord, task: TaskRecord | None) -> JobView:
        if job.final_view is not None:
            view = JobView.model_validate(job.final_view)
        else:
            view = self._live_view(job, task)
        outbox = await self._store.get_outbox(job.job_id)
        if outbox is not None:
            view.delivery = DeliveryView(
                state=outbox.state,
                attempts=outbox.attempts,
                last_status=outbox.last_status,
                next_attempt_at=outbox.next_attempt_at if outbox.state == "pending" else None,
                delivered_at=outbox.delivered_at,
            )
        return view

    def _live_view(self, job: JobRecord, task: TaskRecord | None) -> JobView:
        running = task is not None and task.status == TaskStatus.RUNNING
        updated = task.updated_at.isoformat() if task is not None else job.created_at
        return self._base_view(
            job,
            status=JobStatus.RUNNING if running else JobStatus.QUEUED,
            updated_at=updated,
            result=JobResultView(),
        )

    @staticmethod
    def _base_view(
        job: JobRecord, *, status: JobStatus, updated_at: str, result: JobResultView
    ) -> JobView:
        return JobView(
            id=job.job_id,
            status=status,
            created_at=job.created_at,
            updated_at=updated_at,
            deadline=job.deadline,
            idempotency_key=job.idempotency_key,
            result=result,
            repo_full_name=job.repo_full_name,
            model=job.model,
            metadata=job.metadata,
        )

    def _missing_task_expired(self, job: JobRecord) -> bool:
        age = (_now() - datetime.fromisoformat(job.created_at)).total_seconds()
        return age > _MISSING_TASK_GRACE_SECONDS

    # -- completion ---------------------------------------------------------------------

    async def on_task_completed(self, task: TaskRecord) -> None:
        """Coordinator completion callback for ``source == agent_node_http`` tasks."""
        job_id = str(task.metadata.get("job_id") or task.task_id)
        job = await self.finalize(job_id, task)
        if job is None:
            logger.warning("agent_node.completion.unknown_job", job_id=job_id)

    async def finalize(self, job_id: str, task: TaskRecord | None) -> JobRecord | None:
        """Store the final view and queue the webhook, once. Idempotent.

        A task that is not terminal (or absent and still within the grace period) is left
        alone. Returns the (possibly already finalized) job record.
        """
        async with self._finalize_lock:
            job = await self._store.get_job(job_id)
            if job is None or job.final_view is not None:
                return job
            if task is not None and task.status not in _TERMINAL:
                return job
            if task is None and not self._missing_task_expired(job):
                return job
            result_view, status, updated_at = await self._final_result(job, task)
            view = self._base_view(job, status=status, updated_at=updated_at, result=result_view)
            payload = view.model_dump(mode="json", exclude={"delivery"})
            if job.webhook_url and await self._store.get_outbox(job_id) is None:
                give_up = datetime.fromisoformat(job.deadline) + timedelta(
                    seconds=self.config.webhook_deadline_margin_seconds
                )
                now = _now()
                await self._store.save_outbox(
                    OutboxEntry(
                        job_id=job_id,
                        url=job.webhook_url,
                        signed=job.webhook_signed,
                        body=encode_body(payload),
                        created_at=_iso(now),
                        give_up_at=_iso(max(give_up, now + timedelta(seconds=30))),
                        next_attempt_at=_iso(now),
                    )
                )
            job.final_view = payload
            await self._store.save_job(job)
        logger.info("agent_node.job.finalized", job_id=job_id, status=status.value)
        if job.webhook_url:
            self._sender.kick()
        return job

    async def _final_result(
        self, job: JobRecord, task: TaskRecord | None
    ) -> tuple[JobResultView, JobStatus, str]:
        now_iso = _iso(_now())
        if task is None:
            return (
                JobResultView(
                    error="task record is missing; the agent was not started",
                    error_kind=ErrorKind.ORPHANED,
                ),
                JobStatus.FAILED,
                now_iso,
            )
        updated_at = (task.completed_at or task.updated_at).isoformat()
        started = task.started_at is not None
        if task.status == TaskStatus.COMPLETED:
            completed, status = await self._completed(job, task)
            return completed, status, updated_at
        if job.timed_out_phase is not None:
            return (
                JobResultView(
                    error=(
                        f"deadline passed while {job.timed_out_phase}; "
                        + ("the agent was stopped" if started else "the agent was not started")
                    ),
                    error_kind=ErrorKind.TIMEOUT,
                ),
                JobStatus.FAILED,
                updated_at,
            )
        usage = self._usage_result(task)
        if task.status == TaskStatus.CANCELLED:
            return (
                JobResultView(
                    error=CANCELLED_WHILE_RUNNING if started else CANCELLED_WHILE_QUEUED,
                    error_kind=ErrorKind.CANCELLED,
                    **usage,
                ),
                JobStatus.CANCELLED,
                updated_at,
            )
        if task.status == TaskStatus.EXPIRED:
            return (
                JobResultView(
                    error="job expired before it finished", error_kind=ErrorKind.TIMEOUT, **usage
                ),
                JobStatus.FAILED,
                updated_at,
            )
        error = task.error or "delegation run failed"
        return (
            JobResultView(error=error, error_kind=_failure_kind(error), **usage),
            JobStatus.FAILED,
            updated_at,
        )

    @staticmethod
    def _usage_result(task: TaskRecord) -> dict[str, Any]:
        result = task.result if isinstance(task.result, dict) else {}
        raw_usage = result.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        cost = usage.get("total_cost_usd")
        turns = usage.get("num_turns")
        duration = usage.get("duration_ms")
        if duration is None and task.started_at is not None and task.completed_at is not None:
            duration = int((task.completed_at - task.started_at).total_seconds() * 1000)
        return {
            "cost_usd": float(cost) if isinstance(cost, int | float) else None,
            "num_turns": int(turns) if isinstance(turns, int | float) else None,
            "duration_ms": int(duration) if isinstance(duration, int | float) else None,
        }

    async def _completed(self, job: JobRecord, task: TaskRecord) -> tuple[JobResultView, JobStatus]:
        """Result of a finished run. A repo job whose HEAD did not move is ``no_changes``."""
        result = task.result if isinstance(task.result, dict) else {}
        summary = result.get("summary")
        fields: dict[str, Any] = {
            "output": summary if isinstance(summary, str) else "",
            **self._usage_result(task),
        }
        if job.repo is None:
            return JobResultView(**fields), JobStatus.SUCCEEDED
        after = await git_inspect.head_sha(job.repo.path)
        if after is None or after == job.repo.head_before:
            return JobResultView(**fields), JobStatus.NO_CHANGES
        fields["commit_sha"] = after
        fields["branch"] = await git_inspect.current_branch(job.repo.path)
        if job.repo.head_before is not None:
            fields["diff_stat"] = await git_inspect.diff_stat(
                job.repo.path, job.repo.head_before, after
            )
        return JobResultView(**fields), JobStatus.SUCCEEDED


_TERMINAL = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.EXPIRED,
)


def _failure_kind(error: str) -> ErrorKind:
    lowered = error.lower()
    if lowered.startswith(_RESTART_ERROR_PREFIX.lower()):
        return ErrorKind.ORPHANED
    if "timed out" in lowered:
        return ErrorKind.TIMEOUT
    if "unavailable" in lowered and "backend" in lowered:
        return ErrorKind.CONFIG
    if lowered.startswith("backend crashed"):
        return ErrorKind.INTERNAL
    return ErrorKind.AGENT_FAILED
