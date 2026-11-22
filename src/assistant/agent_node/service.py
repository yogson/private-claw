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
import shutil
import uuid
from collections.abc import Callable, Sequence
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
from assistant.core.capabilities.resolver import expand_nested_capabilities
from assistant.core.capabilities.snapshot import CapabilitySnapshot
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
# How long cancel / expiry wait for the worker to stop before answering anyway.
CANCEL_STOP_TIMEOUT_SECONDS = 15.0
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
    *,
    user_prompt: str,
    system_prompt: str | None,
    repo: RepoContext | None,
    capability_prompts: Sequence[str] = (),
) -> str:
    """Delegation objective: capability context, prompts and, for a repo job, the workspace."""
    parts: list[str] = []
    if capability_prompts:
        parts.append("--- Project context ---\n" + "\n\n".join(capability_prompts))
    if system_prompt:
        parts.append(system_prompt.strip())
    parts.append(user_prompt.strip())
    if repo is not None:
        parts.append(
            "--- Environment ---\n"
            f"Repository: {repo.full_name}\n"
            "Your working directory is a dedicated git worktree of it: "
            f"{repo.worktree or repo.path}\n"
            f"Base branch: {repo.base_branch}\n"
            f"The worktree is already on the work branch {repo.branch}, created from "
            f"{repo.base_branch}. Commit your changes on this branch and do not switch "
            "branches. Do not push and do not open a pull request.\n"
            f"Paths to {repo.path} in the project context refer to the main checkout: "
            "make every change in your worktree instead, and do not modify the main "
            "checkout."
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
        capability_snapshot: Callable[[], CapabilitySnapshot] | None = None,
    ) -> None:
        self.config = config
        # Read per job, so live capability reloads apply to the next job.
        self._capability_snapshot = capability_snapshot
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
        self._expiring: dict[str, asyncio.Task[None]] = {}

    def _capability_context(
        self, capability_ids: list[str], *, job_id: str
    ) -> list[tuple[str, str]]:
        """(capability_id, prompt) pairs for a repo's capabilities, nested ones expanded.

        Uses the live catalog snapshot. Ids without a manifest are skipped with a warning
        rather than failing the job; capabilities with an empty prompt are left out.
        """
        if not capability_ids or self._capability_snapshot is None:
            return []
        definitions = self._capability_snapshot().definitions
        context: list[tuple[str, str]] = []
        for cap_id in expand_nested_capabilities(capability_ids, definitions):
            definition = definitions.get(cap_id)
            if definition is None:
                logger.warning("agent_node.capability_missing", job_id=job_id, capability_id=cap_id)
                continue
            prompt = definition.prompt.strip()
            if prompt:
                context.append((cap_id, prompt))
        return context

    def unknown_capabilities(self) -> dict[str, list[str]]:
        """repo_full_name -> configured capability ids that have no manifest right now."""
        if self._capability_snapshot is None:
            return {}
        definitions = self._capability_snapshot().definitions
        unknown: dict[str, list[str]] = {}
        for name, repo_cfg in self.config.repositories.items():
            missing = [
                c
                for c in expand_nested_capabilities(repo_cfg.capabilities, definitions)
                if c not in definitions
            ]
            if missing:
                unknown[name] = missing
        return unknown

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
        """Finalize jobs whose task ended without a callback; enforce the job deadline.

        An expiry waits on the coordinator to stop the run, which can be slow; each one
        runs as its own task so it never holds up the rest of the pass.
        """
        for job in await self._store.list_unfinalized_jobs():
            task = await self._task_store.get(job.job_id)
            if task is not None and task.status not in _TERMINAL:
                if (
                    _now() > datetime.fromisoformat(job.deadline)
                    and job.job_id not in self._expiring
                ):
                    expiry = asyncio.create_task(
                        self._expire(job, task), name=f"agent_node.expire.{job.job_id}"
                    )
                    self._expiring[job.job_id] = expiry
                    expiry.add_done_callback(self._forget_expiry(job.job_id))
                continue
            await self.finalize(job.job_id, task)

    def _forget_expiry(self, job_id: str) -> Callable[[asyncio.Task[None]], None]:
        def _done(_task: asyncio.Task[None]) -> None:
            self._expiring.pop(job_id, None)

        return _done

    async def _expire(self, job: JobRecord, task: TaskRecord) -> None:
        try:
            phase = "queued" if task.status == TaskStatus.PENDING else "running"
            job.timed_out_phase = phase
            await self._store.save_job(job)
            logger.info("agent_node.job.deadline_passed", job_id=job.job_id, phase=phase)
            await self.finalize(job.job_id, await self._stop_task(job.job_id))
        except Exception:
            logger.exception("agent_node.job.expire_failed", job_id=job.job_id)

    async def _stop_task(self, job_id: str) -> TaskRecord | None:
        """Cancel the task, waiting at most ``CANCEL_STOP_TIMEOUT_SECONDS`` for the worker.

        If the backend's teardown is slow, the task record is forced to CANCELLED so the
        caller can answer; the worker finishes stopping in the background.
        """
        stopping = asyncio.ensure_future(self._coordinator.cancel_task(job_id))
        try:
            return await asyncio.wait_for(asyncio.shield(stopping), CANCEL_STOP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("agent_node.job.cancel_stop_slow", job_id=job_id)
            current = await self._task_store.get(job_id)
            if current is not None and current.status not in _TERMINAL:
                current = await self._task_store.update_status(
                    job_id, TaskStatus.CANCELLED, error="Cancelled by user"
                )
            return current

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
        fingerprint = request_fingerprint(body_dump)
        accepted_at = _now()

        # Validate and fetch the base before taking the global lock, so a slow network blocks
        # only this request. A replay of an accepted job needs neither.
        base_repo = self.config.repositories.get(repo_full_name) if repo_full_name else None
        if base_repo is not None and (
            idempotency_key is None or await self._find_existing(idempotency_key) is None
        ):
            base_path = str(Path(base_repo.path).expanduser())
            if not await git_inspect.is_valid_branch_name(base_path, base_branch):
                raise JobRejected(422, f"base branch {base_branch!r} is not a valid branch name")
            # Like the coordinator's worktree mode: base the job on a freshly fetched
            # origin/<base>. A failed fetch (offline, no remote) is not fatal: the job
            # uses the last fetched origin/<base>, else the local branch.
            fetched = await git_inspect.fetch_branch(base_path, base_branch)
            if not fetched.ok:
                logger.warning(
                    "agent_node.base_fetch_failed",
                    repo=repo_full_name,
                    base_branch=base_branch,
                    error=fetched.error[:300],
                )

        async with self._create_lock:
            # Replay / conflict first: a retry of an accepted job must not depend on the
            # current repository mapping.
            if idempotency_key is not None:
                existing = await self._find_existing(idempotency_key)
                if existing is not None:
                    return await self._replay(existing, fingerprint, requested_deadline)
            repo_cfg = None
            if repo_full_name is not None:
                repo_cfg = self.config.repositories.get(repo_full_name)
                if repo_cfg is None:
                    raise JobRejected(422, f"repository {repo_full_name!r} is not configured")
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
                base_sha = await git_inspect.resolve_base(repo_path, base_branch)
                if base_sha is None:
                    raise JobRejected(
                        422, f"base branch {base_branch!r} not found in {repo_full_name!r}"
                    )
                repo = RepoContext(
                    full_name=repo_full_name,
                    path=repo_path,
                    base_branch=base_branch,
                    base_sha=base_sha,
                    branch=f"agent-node/{job_id[:12]}",
                    worktree=str(self._data_root / "agent_node" / "worktrees" / job_id),
                )

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
            if repo is None:
                job.scratch_dir = str(self._data_root / "agent_node" / "workspaces" / job_id)
            # Persist before enqueueing: a crash in between leaves a job the reconcile loop
            # can see (and fail), never a running task nobody knows about.
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

            capability_context = self._capability_context(
                repo_cfg.capabilities if repo_cfg is not None else [], job_id=job_id
            )
            workdir = await self._prepare_workspace(job)
            if workdir is None:
                # Not a refusal: the job exists and is reported failed (workspace_failed).
                job = await self.finalize(job_id, None) or job
                logger.warning("agent_node.job.workspace_failed", job_id=job_id)
                return CreateOutcome(view=await self._view_of(job, None), replayed=False)

            try:
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
                            capability_prompts=[prompt for _, prompt in capability_context],
                        ),
                        "model_id": self._effective_model(model),
                        "backend": self.config.backend,
                        "timeout_seconds": run_seconds,
                        "backend_params": {
                            "directory": workdir,
                            "permission_mode": self.config.permission_mode,
                            # Exactly the MCP servers of the repo's capabilities (none
                            # for a job without a repo), resolved by the backend at run
                            # time so server secrets never land in the task record.
                            "mcp_capabilities": list(
                                repo_cfg.capabilities if repo_cfg is not None else []
                            ),
                        },
                        "tool_params": self._tool_params(run_seconds),
                        # The job's own worktree / scratch dir is its workspace already.
                        "workspace": {"mode": "in_place"},
                        "task_metadata": {
                            "source": AGENT_NODE_SOURCE,
                            "job_id": job_id,
                            "idempotency_key": idempotency_key,
                            "deadline": _iso(deadline),
                            "interactive": False,
                            "capabilities": [cap_id for cap_id, _ in capability_context],
                        },
                    },
                )
            except BaseException:
                await self._discard(job)
                raise
            if not accept.get("accepted"):
                await self._discard(job)
                reason = str(accept.get("rejection_reason") or "rejected")
                if accept.get("retryable"):
                    raise JobRejected(503, reason, retry_after=RETRY_AFTER_SECONDS)
                raise JobRejected(422, reason)
        logger.info("agent_node.job.accepted", job_id=job_id, repo=repo_full_name)
        return CreateOutcome(view=await self._view_of(job, None), replayed=False)

    def _effective_model(self, requested: str | None) -> str | None:
        """Requested model if allowed, else the configured default (None: coordinator default)."""
        if requested is not None:
            if requested in self._runtime_config.model.model_allowlist:
                return requested
            logger.warning(
                "agent_node.model_not_allowed",
                requested=requested,
                fallback=self.config.default_model or "coordinator default",
            )
            return self.config.default_model
        return self.config.default_model

    async def _prepare_workspace(self, job: JobRecord) -> str | None:
        """Create the worktree / scratch dir; None (``preset_failure`` stored) on failure."""
        if job.repo is not None and job.repo.worktree is not None:
            assert job.repo.base_sha is not None
            error = await git_inspect.worktree_add(
                job.repo.path, job.repo.worktree, job.repo.branch, job.repo.base_sha
            )
            if error is not None:
                job.preset_failure = (
                    ErrorKind.WORKSPACE_FAILED.value,
                    f"could not create the job worktree: {error}"[:500],
                )
                await self._store.save_job(job)
                return None
            return job.repo.worktree
        assert job.scratch_dir is not None
        try:
            await asyncio.to_thread(Path(job.scratch_dir).mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            job.preset_failure = (
                ErrorKind.WORKSPACE_FAILED.value,
                f"could not create the job workspace: {exc}",
            )
            await self._store.save_job(job)
            return None
        return job.scratch_dir

    async def _cleanup_workspace(self, job: JobRecord) -> None:
        """Remove the job's worktree (keeping its branch) or scratch dir. Never raises."""
        try:
            if job.repo is not None and job.repo.worktree is not None:
                if Path(job.repo.worktree).exists() and not await git_inspect.worktree_remove(
                    job.repo.path, job.repo.worktree
                ):
                    logger.warning("agent_node.worktree.remove_failed", job_id=job.job_id)
            elif job.scratch_dir is not None:
                await asyncio.to_thread(shutil.rmtree, job.scratch_dir, True)
        except Exception:
            logger.exception("agent_node.workspace.cleanup_failed", job_id=job.job_id)

    async def _discard(self, job: JobRecord) -> None:
        """Undo an accept that did not happen: records, secret, workspace (and its branch)."""
        await self._cleanup_workspace(job)
        if job.repo is not None and job.repo.worktree is not None:
            await git_inspect.branch_delete(job.repo.path, job.repo.branch)
        await self._store.delete_job(job.job_id)
        if job.idempotency_key is not None:
            await self._store.delete_idempotency(job.idempotency_key)
        await self._store.delete_secret(job.job_id)

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
        # Jobs queue behind the coordinator's worker slots instead of being refused.
        params["delegation_skip_concurrency_limit"] = True
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
            await self.finalize(job_id, await self._stop_task(job_id))
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
            if task is None and job.preset_failure is None and not self._missing_task_expired(job):
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
        await self._cleanup_workspace(job)
        if job.webhook_url:
            self._sender.kick()
        return job

    async def _final_result(
        self, job: JobRecord, task: TaskRecord | None
    ) -> tuple[JobResultView, JobStatus, str]:
        now_iso = _iso(_now())
        if job.preset_failure is not None:
            kind, error = job.preset_failure
            return (
                JobResultView(error=error, error_kind=ErrorKind(kind)),
                JobStatus.FAILED,
                now_iso,
            )
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
        # Judged on the work branch's tip against the base commit, not on any HEAD.
        tip = await git_inspect.branch_tip(job.repo.path, job.repo.branch)
        if tip is None:
            return (
                JobResultView(
                    error=f"could not read work branch {job.repo.branch!r} after the run",
                    error_kind=ErrorKind.INTERNAL,
                    **fields,
                ),
                JobStatus.FAILED,
            )
        if tip == job.repo.base_sha:
            return JobResultView(**fields), JobStatus.NO_CHANGES
        fields["commit_sha"] = tip
        fields["branch"] = job.repo.branch
        if job.repo.base_sha is not None:
            fields["diff_stat"] = await git_inspect.diff_stat(job.repo.path, job.repo.base_sha, tip)
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
    if lowered == "cancelled":
        # Not a user cancel (those go through cancel_task): the worker was cancelled by
        # coordinator shutdown.
        return ErrorKind.ORPHANED
    if "timed out" in lowered:
        return ErrorKind.TIMEOUT
    if "unavailable" in lowered and "backend" in lowered:
        return ErrorKind.CONFIG
    if lowered.startswith("backend crashed"):
        return ErrorKind.INTERNAL
    return ErrorKind.AGENT_FAILED
