"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Background delegation coordinator for provider-backed tasks.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from assistant.agent_node.models import AGENT_NODE_SOURCE
from assistant.core.config.schemas import RuntimeConfig
from assistant.store.filesystem.task_log import append_log_line, read_log_tail, task_log_path
from assistant.store.interfaces import StoreFacadeInterface
from assistant.store.models import TaskRecord, TaskStatus
from assistant.subagents.contracts import DelegationAcceptResult, DelegationRun
from assistant.subagents.interfaces import (
    DelegationBackendAdapterInterface,
    DelegationCoordinatorInterface,
)
from assistant.subagents.permission_modes import clamp_permission_mode, resolve_permission_mode
from assistant.subagents.workspaces import (
    ResolvedWorkspace,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceRequest,
    environment_block,
)

logger = structlog.get_logger(__name__)

_COORDINATOR_SLEEP_SECONDS = 1.0
_DEFAULT_MAX_WORKERS = 3
_DEFAULT_BUDGET_WINDOW_SECONDS = 86400
_DEFAULT_BACKEND = "claude_code"
# Without a directory these would run in the process cwd: the live primary checkout.
_DIRECTORY_REQUIRED_BACKENDS = frozenset({"claude_code", "claude_code_streaming"})
# Default wait for the user's answer to a sub-agent's AskUserQuestion relay. Configurable
# via delegation_question_timeout_seconds (see resolve_question_timeout_seconds) and capped
# per task by its timeout_seconds. claude_code_streaming.py derives its SDK stdin-close
# timeout from the configured value.
DELEGATION_RELAY_TIMEOUT_S = 1800
_TERMINAL_TASK_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.EXPIRED,
)
_TASK_BUDGET_REASON = "task token budget exceeded"
_DEFAULT_LOG_TAIL_LINES = 200
_MAX_LOG_TAIL_LINES = 2000


def resolve_question_timeout_seconds(config: RuntimeConfig) -> int:
    """Configured wait for a relayed question's answer, before the per-task cap.

    Read from the ``delegate_subagent_task`` defaults in tools.yaml
    (``delegation_question_timeout_seconds``); :data:`DELEGATION_RELAY_TIMEOUT_S` if unset.
    """
    for tool in config.tools.tools:
        if tool.tool_id != "delegate_subagent_task" or tool.default_params is None:
            continue
        value = tool.default_params.delegation_question_timeout_seconds
        if value is not None:
            return value
    return DELEGATION_RELAY_TIMEOUT_S


def _format_wait(seconds: int) -> str:
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def question_expired_answer(timeout_seconds: int) -> str:
    """The answer a sub-agent gets when the user did not answer its question in time."""
    return (
        f"No answer from the user within {_format_wait(timeout_seconds)}; the question "
        "expired. Proceed with your best judgement and state the assumption, or stop "
        "and report."
    )


def question_expired_notice(timeout_seconds: int) -> str:
    """Shown to the user in place of a question that is no longer awaited (timeout)."""
    return (
        f"\u231b This question expired: no answer within {_format_wait(timeout_seconds)}. "
        "The task continued without your answer."
    )


QUESTION_WITHDRAWN_NOTICE = (
    "\u231b This question is no longer awaited: the task stopped before you answered."
)


class DelegationCoordinator(DelegationCoordinatorInterface):
    """Coordinator that enqueues and executes delegated tasks."""

    def __init__(
        self,
        *,
        store: StoreFacadeInterface,
        config: RuntimeConfig,
        backends: list[DelegationBackendAdapterInterface],
        completion_callback: Callable[[TaskRecord], Awaitable[None]] | None = None,
        workspaces: WorkspaceManager | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._backends = {b.backend_id: b for b in backends}
        self._completion_callback = completion_callback
        # Sibling of the store's runtime/tasks/ dir - append-only activity
        # logs, not TaskRecord JSON, so they don't share that store's
        # whole-file-rewrite-per-update cost. See store/filesystem/task_log.py.
        self._log_dir = Path(self._config.app.data_root) / "runtime" / "task_logs"
        # Optional callback for relaying AskUserQuestion to the channel layer.
        # Signature: (task_id, session_id, question, options) -> handle
        # The callback is responsible only for sending the question; the answer
        # is delivered via submit_delegation_answer().  The returned handle is
        # opaque here and only handed back to _question_expired_callback.
        self._question_relay_callback: (
            Callable[[str, str, str, list[str]], Awaitable[Any]] | None
        ) = None
        # Optional channel callback that marks a sent question as no longer awaited
        # (e.g. edits the message and drops its buttons).
        # Signature: (task_id, session_id, handle, notice) -> None
        self._question_expired_callback: Callable[[str, str, Any, str], Awaitable[None]] | None = (
            None
        )
        self._question_timeout_s = resolve_question_timeout_seconds(config)
        # Background "no longer awaited" notices of cancelled relays (kept referenced).
        self._question_notice_tasks: set[asyncio.Task[None]] = set()
        # Pending asyncio Futures for in-flight AskUserQuestion relays.
        # Keyed by chat_id (stable Telegram identifier, extracted from session)
        # so that a session switch between task creation and reply does not lose
        # the answer.  v1 constraint: only one pending question per chat at a time.
        self._pending_questions: dict[str, asyncio.Future[str]] = {}
        self._stop = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._inflight: dict[str, asyncio.Task[None]] = {}
        # task_ids with a user-initiated cancel_task() in flight, so the
        # BaseException handler in _execute_task can tell a deliberate
        # cancellation apart from an unexpected asyncio cancellation.
        self._cancel_requested: set[str] = set()
        # task_ids whose worker finished the run and is now in the completion callback.
        self._notifying: set[str] = set()
        self._max_workers = self._default_max_workers()
        self._worker_sem = asyncio.Semaphore(self._max_workers)
        # Per-task workspaces (worktree / readonly / in_place), see subagents/workspaces.py.
        self._workspaces = workspaces or WorkspaceManager(config)
        self._reconcile_task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._worker_task is not None:
            return
        await self._recover_inflight_tasks()
        await self._reclaim_pending_workspaces()
        # One-shot startup cleanup (git worktree prune + reconcile); not a worker.
        self._reconcile_task = asyncio.create_task(self._startup_reconcile())
        self._stop.clear()
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Stop the background polling loop."""
        if self._reconcile_task is not None:
            if not self._reconcile_task.done():
                self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reconcile_task
            self._reconcile_task = None
        if self._worker_task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._worker_task, timeout=10.0)
        except TimeoutError:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        self._worker_task = None

    def set_completion_callback(
        self, callback: Callable[[TaskRecord], Awaitable[None]] | None
    ) -> None:
        """Update terminal-task completion callback at runtime."""
        self._completion_callback = callback

    def set_question_relay_callback(
        self,
        callback: Callable[[str, str, str, list[str]], Awaitable[Any]] | None,
    ) -> None:
        """Register a channel-layer callback for relaying AskUserQuestion to the user.

        The callback receives ``(task_id, session_id, question, options)`` and is
        responsible for sending the question to the channel layer (e.g. Telegram).
        The answer must be delivered back via :meth:`submit_delegation_answer`.
        Whatever it returns identifies the sent question to the expired callback.
        """
        self._question_relay_callback = callback

    def set_question_expired_callback(
        self,
        callback: Callable[[str, str, Any, str], Awaitable[None]] | None,
    ) -> None:
        """Register a channel-layer callback for questions that are no longer awaited.

        Called with ``(task_id, session_id, handle, notice)`` when a relayed question
        times out or its task stops waiting; ``handle`` is what the relay callback
        returned for that question and ``notice`` is the text to show in its place.
        """
        self._question_expired_callback = callback

    def has_pending_question(self, session_id: str) -> bool:
        """Return True if the session has an in-flight AskUserQuestion relay.

        The lookup is keyed by chat_id (extracted from session_id) so that a
        session switch between task creation and reply does not lose the answer.
        """
        key = self._pending_question_key(session_id)
        return key in self._pending_questions

    def submit_delegation_answer(self, session_id: str, answer: str) -> bool:
        """Fulfil a pending AskUserQuestion relay with the user's answer.

        Returns True if a pending question was found and resolved, False otherwise.
        """
        key = self._pending_question_key(session_id)
        future = self._pending_questions.get(key)
        if future is not None and not future.done():
            future.set_result(answer)
            return True
        # No sub-agent waits any more (e.g. the question expired meanwhile).
        logger.info("subagent.question_relay.late_answer_dropped", session_id=session_id)
        return False

    async def enqueue_from_tool(
        self,
        *,
        session_id: str,
        turn_id: str,
        trace_id: str,
        user_id: str | None,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate and create a pending delegated task."""
        logger.info(
            "subagent.spawn.requested",
            session_id=session_id,
            turn_id=turn_id,
            trace_id=trace_id,
            model_id=request.get("model_id"),
        )
        objective = str(request.get("objective", "")).strip()
        if not objective:
            return self._rejected("rejected_invalid", "objective is required")
        backend_id = self._resolve_backend(request)
        if backend_id not in self._backends:
            return self._rejected("rejected_invalid", f"backend '{backend_id}' is not available")
        raw_params = request.get("backend_params")
        directory = raw_params.get("directory") if isinstance(raw_params, dict) else None
        if backend_id in _DIRECTORY_REQUIRED_BACKENDS and not (
            isinstance(directory, str) and directory.strip()
        ):
            return self._rejected(
                "rejected_invalid",
                f"directory is required for backend '{backend_id}': pass the absolute path "
                "of the project (or folder) the sub-agent should work in",
            )

        tool_params = request.get("tool_params", {})
        model_id = self._resolve_model_id(request, tool_params)
        if model_id is None:
            attempted_model = self._attempted_model_id(request, tool_params)
            return self._rejected(
                "rejected_policy",
                f"model_id '{attempted_model}' is not allowed",
            )
        if not self._backend_allowed(backend_id, tool_params):
            return self._rejected(
                "rejected_policy",
                f"backend '{backend_id}' is not in delegation_allowed_backends",
            )
        if not self._model_allowed(model_id, tool_params):
            return self._rejected(
                "rejected_policy",
                "model_id is not in delegation_model_allowlist",
            )
        # Callers that queue behind the worker slots (HTTP ingress) opt out of the limit.
        skip_limit = isinstance(tool_params, dict) and bool(
            tool_params.get("delegation_skip_concurrency_limit")
        )
        if not skip_limit and await self._reaches_concurrency_limit(tool_params):
            return self._rejected(
                "rejected_policy",
                "max concurrent delegation tasks reached",
                retryable=True,
            )
        budget_reason, projected_tokens = await self._check_budget(
            session_id=session_id,
            max_turns=self._resolve_max_turns(request, tool_params),
            tool_params=tool_params,
        )
        if budget_reason is not None:
            # Window budgets free up as usage ages out; a per-task cap never will.
            return self._rejected(
                "rejected_policy",
                budget_reason,
                retryable=budget_reason != _TASK_BUDGET_REASON,
            )

        ttl_seconds = self._resolve_ttl_seconds(tool_params)
        task_id = str(request.get("task_id") or f"dlg-{uuid.uuid4().hex[:12]}")
        backend_params = self._resolve_backend_params(
            request,
            tool_params,
            log_context={"task_id": task_id, "trace_id": trace_id, "session_id": session_id},
        )
        try:
            workspace = await self._resolve_workspace(task_id, request, backend_params)
        except WorkspaceError as exc:
            return self._rejected("rejected_workspace", exc.reason, retryable=exc.retryable)
        now = datetime.now(UTC)
        metadata: dict[str, Any] = {
            "kind": "delegation",
            "backend": backend_id,
            "objective": objective,
            "model_id": model_id,
            "max_turns": self._resolve_max_turns(request, tool_params),
            "timeout_seconds": self._resolve_timeout_seconds(request, tool_params),
            "requested_by_user_id": user_id,
            "trace_id": trace_id,
            "chat_id": self._chat_id_from_session(session_id),
            "projected_tokens": projected_tokens,
            "backend_params": backend_params,
        }
        if workspace is not None:
            metadata["workspace"] = workspace.model_dump(mode="json")
        logfire_ctx = request.get("logfire_context")
        if isinstance(logfire_ctx, dict) and logfire_ctx:
            metadata["logfire_context"] = logfire_ctx
        # Caller-supplied extras (e.g. the HTTP ingress's ``source``); never override core keys.
        extra_metadata = request.get("task_metadata")
        if isinstance(extra_metadata, dict):
            for key, value in extra_metadata.items():
                metadata.setdefault(key, value)
        task = TaskRecord(
            task_id=task_id,
            parent_session_id=session_id,
            parent_turn_id=turn_id,
            task_type="delegation",
            status=TaskStatus.PENDING,
            created_at=now,
            updated_at=now,
            ttl_seconds=ttl_seconds,
            expires_at=now + timedelta(seconds=ttl_seconds),
            metadata=metadata,
        )
        try:
            await self._store.tasks.create(task)
        except BaseException:
            await self._workspaces.release(task_id)
            raise
        accepted = DelegationAcceptResult(
            accepted=True,
            task_id=task_id,
            status=TaskStatus.PENDING.value,
            expires_at=task.expires_at.isoformat() if task.expires_at else None,
            workspace=workspace.info() if workspace is not None else None,
        )
        logger.info(
            "subagent.spawn.accepted",
            task_id=task_id,
            model_id=model_id,
            backend=backend_id,
            projected_tokens=projected_tokens,
            workspace_mode=workspace.mode.value if workspace is not None else None,
            directory=backend_params.get("directory"),
            branch=workspace.branch if workspace is not None else None,
        )
        return accepted.model_dump(mode="json")

    async def _resolve_workspace(
        self, task_id: str, request: dict[str, Any], backend_params: dict[str, Any]
    ) -> ResolvedWorkspace | None:
        """Apply the workspace mode; rewrites ``backend_params["directory"]`` to the cwd.

        Requests without a directory (non-Claude-Code backends only) get no workspace.
        """
        workspace_request = WorkspaceRequest.parse(request.get("workspace"))
        directory = backend_params.get("directory")
        if not isinstance(directory, str) or not directory.strip():
            if not workspace_request.is_empty:
                raise WorkspaceError("workspace_mode, branch and base need a directory")
            return None
        workspace = await self._workspaces.resolve(
            task_id=task_id,
            directory=directory,
            request=workspace_request,
            is_task_active=self._is_task_active,
        )
        backend_params["directory"] = workspace.directory
        return workspace

    async def _is_task_active(self, task_id: str) -> bool:
        task = await self._store.tasks.get(task_id)
        return task is not None and task.status not in _TERMINAL_TASK_STATUSES

    @staticmethod
    def _task_workspace(task: TaskRecord) -> ResolvedWorkspace | None:
        raw = task.metadata.get("workspace")
        if not isinstance(raw, dict):
            return None
        try:
            return ResolvedWorkspace.model_validate(raw)
        except ValueError:
            logger.warning("subagent.workspace.invalid_metadata", task_id=task.task_id)
            return None

    async def _reclaim_pending_workspaces(self) -> None:
        """After a restart, pending worktree tasks take their worktree ownership back."""
        for task in await self._store.tasks.list_by_status(TaskStatus.PENDING):
            workspace = self._task_workspace(task) if task.task_type == "delegation" else None
            if workspace is None:
                continue
            try:
                await self._workspaces.ensure_owned(task.task_id, workspace)
            except WorkspaceError as exc:
                # The task fails with this reason when the worker picks it up.
                logger.warning(
                    "subagent.workspace.reclaim_failed", task_id=task.task_id, reason=exc.reason
                )

    async def _startup_reconcile(self) -> None:
        try:
            await self._workspaces.reconcile(prune=True)
        except Exception:
            logger.exception("subagent.workspace.reconcile_failed")

    async def get_task(self, task_id: str, *, session_id: str | None = None) -> TaskRecord | None:
        """Fetch a task by id.

        When ``session_id`` is given, the lookup is scoped to that session:
        a task belonging to a different session is treated as not found, so
        one chat can't probe another session's delegated work by guessing ids.
        """
        task = await self._store.tasks.get(task_id)
        if task is None:
            return None
        if session_id is not None and task.parent_session_id != session_id:
            return None
        return task

    async def cancel_task(
        self, task_id: str, *, session_id: str | None = None
    ) -> TaskRecord | None:
        """Cancel a pending or running task (see ``get_task`` for ``session_id`` scoping).

        A task still PENDING (not yet claimed by the worker loop) is flipped
        to CANCELLED directly - the worker loop only polls for PENDING tasks,
        so this pre-empts execution.

        A RUNNING task has its in-flight asyncio.Task cancelled and is awaited
        so the caller gets back the final, settled status. Backends propagate
        the resulting asyncio.CancelledError out of ``execute()`` after
        terminating their own subprocess/subquery (see claude_code.py and
        claude_code_streaming.py), so this actually stops the underlying work
        rather than merely relabeling the store record.
        """
        task = await self.get_task(task_id, session_id=session_id)
        if task is None:
            return None
        if task.status in _TERMINAL_TASK_STATUSES:
            return task
        worker = self._inflight.get(task_id)
        if worker is not None and not worker.done():
            self._cancel_requested.add(task_id)
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
            self._cancel_requested.discard(task_id)
        # worker.cancel() only guarantees the coroutine stops - it does not
        # guarantee _execute_task got a chance to persist that as a status
        # transition. That handler only covers cancellation while blocked in
        # backend.execute(); a task cancelled before its first line ever runs
        # (asyncio.create_task() hasn't been scheduled yet) or between the
        # RUNNING transition and backend.execute() starting leaves the store
        # record exactly as it was (PENDING/RUNNING), which would make the
        # worker loop pick a "cancelled" PENDING task back up. Force it
        # terminal here so cancel_task's contract - the record it returns is
        # always settled - holds regardless of where the cancellation landed.
        final = await self._store.tasks.get(task_id)
        if final is not None and final.status not in _TERMINAL_TASK_STATUSES:
            final = await self._store.tasks.update_status(
                task_id, TaskStatus.CANCELLED, error="Cancelled by user"
            )
        await self._workspaces.release(task_id)
        return final

    async def read_task_log(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        tail_lines: int = _DEFAULT_LOG_TAIL_LINES,
    ) -> dict[str, Any] | None:
        """Return the last ``tail_lines`` activity lines for a task's run.

        Returns None if the task doesn't exist (or isn't owned by
        ``session_id``, when given) so callers can distinguish "unknown task"
        from "no log content yet". Both delegation backends (claude_code and
        claude_code_streaming) write incremental activity lines here.
        """
        task = await self.get_task(task_id, session_id=session_id)
        if task is None:
            return None
        lines = await self.read_log_lines(task_id, tail_lines)
        return {"task_id": task_id, "status": task.status.value, "lines": lines}

    async def read_log_lines(self, task_id: str, tail_lines: int) -> list[str]:
        """Return the last ``tail_lines`` log lines without an ownership lookup.

        For callers that already fetched (and authorized) the task. The file is
        read in a worker thread so a large log never blocks the event loop.
        """
        capped = max(1, min(tail_lines, _MAX_LOG_TAIL_LINES))
        return await asyncio.to_thread(read_log_tail, task_log_path(self._log_dir, task_id), capped)

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            self._reap_finished_inflight()
            # Workers that only deliver the completion callback no longer hold a slot.
            busy = len(self._inflight) - len(self._notifying.intersection(self._inflight))
            available_slots = max(0, self._max_workers - busy)
            if available_slots > 0:
                pending = await self._store.tasks.list_by_status(TaskStatus.PENDING)
                for task in pending:
                    if available_slots <= 0:
                        break
                    if task.task_type != "delegation" or task.task_id in self._inflight:
                        continue
                    worker = asyncio.create_task(self._execute_task_guarded(task.task_id))
                    self._inflight[task.task_id] = worker
                    available_slots -= 1
            await asyncio.sleep(_COORDINATOR_SLEEP_SECONDS)
        await self._drain_inflight()

    async def _execute_task_guarded(self, task_id: str) -> None:
        finished: TaskRecord | None = None
        async with self._worker_sem:
            task = await self._store.tasks.get(task_id)
            if task is None:
                return
            try:
                workspace = self._task_workspace(task)
                if workspace is None:
                    finished = await self._execute_task(task)
                else:
                    try:
                        await self._workspaces.ensure_owned(task_id, workspace)
                        lock = self._workspaces.in_place_lock(
                            task_id,
                            workspace,
                            on_wait=lambda holder: self._log_lock_wait(task_id, holder),
                        )
                        async with lock:
                            finished = await self._execute_task(task)
                    except WorkspaceError as exc:
                        finished = await self._fail_workspace(task, exc.reason)
            finally:
                # The worktree outlives the task; only its ownership ends here.
                await self._workspaces.release(task_id)
        # The task is terminal and its workspace (worktree ownership / in_place lock) and
        # worker slot are free, so the completion turn can delegate the next round on
        # the same branch or checkout.
        if finished is not None:
            self._notifying.add(task_id)
            try:
                await self._notify_completion(finished)
            finally:
                self._notifying.discard(task_id)

    async def _log_lock_wait(self, task_id: str, holder: dict[str, Any]) -> None:
        line = f"[workspace] waiting for the in_place lock (held by {holder.get('task_id')})"
        await append_log_line(task_log_path(self._log_dir, task_id), line)

    async def _fail_workspace(self, task: TaskRecord, reason: str) -> TaskRecord | None:
        """Mark the task FAILED; returns the record to notify about (None if none)."""
        latest = await self._store.tasks.get(task.task_id)
        if latest is not None and latest.status in _TERMINAL_TASK_STATUSES:
            return None
        updated = await self._store.tasks.update_status(
            task.task_id, TaskStatus.FAILED, error=f"workspace: {reason}"
        )
        logger.warning("subagent.run.failed", task_id=task.task_id, error=reason)
        return updated

    async def _execute_task(self, task: TaskRecord) -> TaskRecord | None:
        """Run the task to a terminal status.

        Returns the terminal record for the completion callback (None when there is
        nothing to notify, e.g. a cancelled task). The caller notifies only after the
        task's workspace is released.
        """
        backend_id = str(task.metadata.get("backend", "")).strip()
        backend = self._backends.get(backend_id)
        if backend is None:
            updated = await self._store.tasks.update_status(
                task.task_id,
                TaskStatus.FAILED,
                error=f"backend '{backend_id}' unavailable",
            )
            logger.warning(
                "subagent.run.failed",
                task_id=task.task_id,
                backend=backend_id,
                error=f"backend '{backend_id}' unavailable",
            )
            return updated
        updated_task = await self._store.tasks.update_status(task.task_id, TaskStatus.RUNNING)
        if updated_task is not None:
            task = updated_task
        logger.info(
            "subagent.run.started",
            task_id=task.task_id,
            backend=backend_id,
            model_id=task.metadata.get("model_id"),
        )
        objective = str(task.metadata.get("objective", ""))
        workspace = self._task_workspace(task)
        # agent-node jobs describe their own job worktree / scratch dir in the objective;
        # the generic block (e.g. in_place's "no worktree for this task") would contradict it.
        if workspace is not None and task.metadata.get("source") != AGENT_NODE_SOURCE:
            objective = f"{environment_block(workspace)}\n{objective}"
        latest = await self._store.tasks.get(task.task_id)
        if latest is not None and latest.status == TaskStatus.CANCELLED:
            logger.info("subagent.run.cancelled", task_id=task.task_id)
            return None
        await self._store.tasks.heartbeat(task.task_id)
        run = DelegationRun(
            task_id=task.task_id,
            objective=objective,
            model_id=str(task.metadata.get("model_id", self._config.model.default_model_id)),
            timeout_seconds=int(task.metadata.get("timeout_seconds", 300)),
            max_turns=int(task.metadata.get("max_turns", 8)),
            backend_params=(
                dict(task.metadata.get("backend_params", {}))
                if isinstance(task.metadata.get("backend_params"), dict)
                else {}
            ),
            log_path=str(task_log_path(self._log_dir, task.task_id)),
        )
        logger.info(
            "subagent.run.progress",
            task_id=task.task_id,
            backend=backend_id,
            permission_mode=run.backend_params.get("permission_mode"),
        )
        # For backends that support AskUserQuestion relay, register
        # a per-task relay closure before executing and clean it up afterwards.
        if backend.supports_relay:
            self._register_streaming_relay(backend, task)
        try:
            try:
                result = await backend.execute(run)
            except Exception as exc:
                # Backend should return DelegationResult(ok=False, ...) for
                # normal failures. A raised exception means something
                # unexpected — mark the task FAILED so it doesn't ghost in
                # RUNNING forever and notify completion downstream.
                logger.exception(
                    "subagent.run.crashed",
                    task_id=task.task_id,
                    backend=backend_id,
                )
                updated = await self._store.tasks.update_status(
                    task.task_id,
                    TaskStatus.FAILED,
                    error=f"backend crashed: {type(exc).__name__}: {exc}",
                )
                return updated
            except BaseException:
                # Cancellation / SystemExit / KeyboardInterrupt: best-effort
                # mark the terminal status for state consistency, then
                # re-raise so the asyncio cancellation chain isn't swallowed.
                # A task_id present in _cancel_requested got here via a
                # deliberate cancel_task() call, not an unexpected shutdown -
                # record that distinction instead of always saying FAILED.
                was_requested = task.task_id in self._cancel_requested
                self._cancel_requested.discard(task.task_id)
                with contextlib.suppress(Exception):
                    await self._store.tasks.update_status(
                        task.task_id,
                        TaskStatus.CANCELLED if was_requested else TaskStatus.FAILED,
                        error="Cancelled by user" if was_requested else "cancelled",
                    )
                raise
        finally:
            if backend.supports_relay:
                backend.unregister_relay(task.task_id)
        if not result.ok:
            updated = await self._store.tasks.update_status(
                task.task_id,
                TaskStatus.FAILED,
                error=result.error or "delegation run failed",
                result={"usage": result.usage, "artifacts": result.artifacts},
            )
            if updated is not None:
                logger.warning(
                    "subagent.run.failed",
                    task_id=task.task_id,
                    error=updated.error,
                )
            return updated
        summary = result.output_text
        updated = await self._store.tasks.update_status(
            task.task_id,
            TaskStatus.COMPLETED,
            result={"summary": summary, "usage": result.usage, "artifacts": result.artifacts},
        )
        if updated is not None:
            logger.info(
                "subagent.run.completed",
                task_id=task.task_id,
            )
        return updated

    def _register_streaming_relay(
        self,
        backend: DelegationBackendAdapterInterface,
        task: TaskRecord,
    ) -> None:
        """Build and register a per-task question relay on a streaming backend.

        The relay closure:
        1. Calls ``_question_relay_callback`` to notify the channel layer (send to Telegram)
        2. Only after a confirmed send, registers an asyncio.Future keyed by chat_id
        3. Awaits the Future, which is resolved via :meth:`submit_delegation_answer`,
           for the configured question timeout capped by the task's timeout_seconds
        4. On timeout, withdraws the question, has the channel mark it expired and
           returns :func:`question_expired_answer` to the sub-agent
        """
        if self._question_relay_callback is None:
            return
        if task.metadata.get("interactive") is False:
            # No human to ask (e.g. HTTP ingress jobs): leave the backend without a relay
            # so AskUserQuestion is answered at once with "user unavailable" instead of
            # waiting out the question timeout for a reply that cannot come.
            logger.info("subagent.question_relay.disabled", task_id=task.task_id)
            return
        task_id = task.task_id
        session_id = task.parent_session_id or ""
        pending_key = self._pending_question_key(session_id)
        relay_callback = self._question_relay_callback
        # Waiting past the task's own timeout is pointless: the run is gone by then.
        task_timeout = self._parse_positive_int(task.metadata.get("timeout_seconds"))
        timeout_s = min(self._question_timeout_s, task_timeout or self._question_timeout_s)
        log_ctx = {
            "task_id": task_id,
            "session_id": session_id,
            "trace_id": task.metadata.get("trace_id"),
            "turn_id": task.parent_turn_id,
        }

        async def _relay(question: str, options: list[str]) -> str:
            loop = asyncio.get_running_loop()
            future: asyncio.Future[str] = loop.create_future()
            try:
                # Notify the channel layer; propagates on send failure so we do
                # not register a Future for a question the user never received.
                handle = await relay_callback(task_id, session_id, question, options)
            except Exception:
                logger.exception(
                    "subagent.question_relay.send_failed",
                    task_id=task_id,
                    session_id=session_id,
                )
                future.cancel()
                # Surface a visible error so the user is not left waiting.
                # Guard against a secondary failure in relay_callback itself.
                _err_msg = (
                    "\u26a0\ufe0f Could not relay question to you \u2014 delegation task aborted."
                )
                try:
                    await relay_callback(task_id, session_id, _err_msg, [])
                except Exception:
                    logger.exception(
                        "subagent.question_relay.error_notify_failed",
                        task_id=task_id,
                        session_id=session_id,
                    )
                return ""
            # Register only after confirmed send so has_pending_question() only
            # returns True when the user actually received the question.
            self._pending_questions[pending_key] = future
            asked_at = loop.time()
            try:
                # Wait until submit_delegation_answer() resolves the future
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
            except TimeoutError:
                # Withdraw before notifying the channel, so an answer arriving
                # meanwhile is not taken for one the sub-agent still waits for.
                self._withdraw_question(pending_key, future)
                logger.warning(
                    "subagent.question_relay.answer_timeout",
                    **log_ctx,
                    timeout_s=timeout_s,
                    waited_s=round(loop.time() - asked_at, 1),
                )
                await self._notify_question_expired(
                    task_id, session_id, handle, question_expired_notice(timeout_s)
                )
                return question_expired_answer(timeout_s)
            except asyncio.CancelledError:
                # The task stopped (cancelled, run timeout, shutdown) while waiting.
                self._withdraw_question(pending_key, future)
                logger.info(
                    "subagent.question_relay.withdrawn",
                    **log_ctx,
                    waited_s=round(loop.time() - asked_at, 1),
                )
                notice = asyncio.create_task(
                    self._notify_question_expired(
                        task_id, session_id, handle, QUESTION_WITHDRAWN_NOTICE
                    )
                )
                self._question_notice_tasks.add(notice)
                notice.add_done_callback(self._question_notice_tasks.discard)
                raise
            finally:
                self._withdraw_question(pending_key, future)

        backend.register_relay(task_id, _relay)

    def _withdraw_question(self, pending_key: str, future: asyncio.Future[str]) -> None:
        """Stop awaiting ``future``; never touches another question of the same chat."""
        if self._pending_questions.get(pending_key) is future:
            del self._pending_questions[pending_key]
        if not future.done():
            future.cancel()

    async def _notify_question_expired(
        self, task_id: str, session_id: str, handle: Any, notice: str
    ) -> None:
        if self._question_expired_callback is None:
            return
        try:
            await self._question_expired_callback(task_id, session_id, handle, notice)
        except Exception:
            logger.warning(
                "subagent.question_relay.expire_notify_failed",
                task_id=task_id,
                session_id=session_id,
                exc_info=True,
            )

    async def _notify_completion(self, task: TaskRecord) -> None:
        if self._completion_callback is None:
            return
        try:
            await self._completion_callback(task)
        except Exception:
            logger.exception("subagent.callback.failed", task_id=task.task_id)

    async def _recover_inflight_tasks(self) -> None:
        running = await self._store.tasks.list_by_status(TaskStatus.RUNNING)
        for task in running:
            if task.task_type != "delegation":
                continue
            await self._store.tasks.update_status(
                task.task_id,
                TaskStatus.FAILED,
                error="Task interrupted by process restart",
            )
            logger.warning("subagent.run.recovered_as_failed", task_id=task.task_id)

    def _resolve_backend(self, request: dict[str, Any]) -> str:
        raw_backend = request.get("backend")
        backend = str(raw_backend).strip() if raw_backend is not None else ""
        return backend or _DEFAULT_BACKEND

    def _resolve_model_id(self, request: dict[str, Any], tool_params: object) -> str | None:
        model_id = self._attempted_model_id(request, tool_params)
        return model_id if model_id in self._config.model.model_allowlist else None

    def _attempted_model_id(self, request: dict[str, Any], tool_params: object) -> str:
        raw_model_id = request.get("model_id")
        model_id = str(raw_model_id).strip() if raw_model_id is not None else ""
        if not model_id and isinstance(tool_params, dict):
            raw_default_model_id = tool_params.get("delegation_default_model_id")
            model_id = str(raw_default_model_id).strip() if raw_default_model_id is not None else ""
        if not model_id:
            model_id = self._config.model.default_model_id
        return model_id

    @staticmethod
    def _backend_allowed(backend: str, tool_params: object) -> bool:
        if not isinstance(tool_params, dict):
            return True
        allow = tool_params.get("delegation_allowed_backends")
        if not isinstance(allow, list) or not allow:
            return True
        return backend in {str(x) for x in allow}

    @staticmethod
    def _model_allowed(model_id: str, tool_params: object) -> bool:
        if not isinstance(tool_params, dict):
            return True
        allow = tool_params.get("delegation_model_allowlist")
        if not isinstance(allow, list) or not allow:
            return True
        allowed = {str(x) for x in allow}
        return model_id in allowed

    @staticmethod
    def _resolve_ttl_seconds(tool_params: object) -> int:
        default_ttl = 3600
        if not isinstance(tool_params, dict):
            return default_ttl
        raw_default = tool_params.get("delegation_default_ttl_seconds")
        raw_max = tool_params.get("delegation_max_ttl_seconds")
        try:
            ttl = int(raw_default) if raw_default is not None else default_ttl
        except (TypeError, ValueError):
            ttl = default_ttl
        try:
            max_ttl = int(raw_max) if raw_max is not None else ttl
        except (TypeError, ValueError):
            max_ttl = ttl
        if max_ttl > 0:
            ttl = min(ttl, max_ttl)
        return max(ttl, 1)

    async def _reaches_concurrency_limit(self, tool_params: object) -> bool:
        if not isinstance(tool_params, dict):
            return False
        raw_limit = tool_params.get("delegation_max_concurrent_tasks")
        try:
            limit = int(raw_limit) if raw_limit is not None else 0
        except (TypeError, ValueError):
            return False
        if limit <= 0:
            return False
        running = await self._store.tasks.list_by_status(TaskStatus.RUNNING)
        pending = await self._store.tasks.list_by_status(TaskStatus.PENDING)
        delegation_count = sum(1 for t in running + pending if t.task_type == "delegation")
        return delegation_count >= limit

    async def _check_budget(
        self,
        *,
        session_id: str,
        max_turns: int,
        tool_params: object,
    ) -> tuple[str | None, int]:
        projected_tokens = self._projected_tokens(max_turns, tool_params)
        if projected_tokens <= 0:
            return None, projected_tokens
        if not isinstance(tool_params, dict):
            return None, projected_tokens

        per_task_cap = self._coerce_int(tool_params.get("delegation_per_task_token_cap"))
        if per_task_cap and projected_tokens > per_task_cap:
            return _TASK_BUDGET_REASON, projected_tokens

        budget_window = (
            self._coerce_int(tool_params.get("delegation_budget_window_seconds"))
            or _DEFAULT_BUDGET_WINDOW_SECONDS
        )
        cutoff = datetime.now(UTC) - timedelta(seconds=budget_window)
        tasks = await self._list_all_delegation_tasks()
        relevant = [t for t in tasks if t.updated_at >= cutoff]
        global_used = sum(self._task_token_usage(t) for t in relevant)
        session_used = sum(
            self._task_token_usage(t) for t in relevant if t.parent_session_id == session_id
        )

        per_session_cap = self._coerce_int(tool_params.get("delegation_per_session_token_cap"))
        if per_session_cap and (session_used + projected_tokens) > per_session_cap:
            return "session token budget exceeded", projected_tokens

        global_cap = self._coerce_int(tool_params.get("delegation_global_token_cap"))
        if global_cap and (global_used + projected_tokens) > global_cap:
            return "global token budget exceeded", projected_tokens
        return None, projected_tokens

    async def _list_all_delegation_tasks(self) -> list[TaskRecord]:
        statuses = (
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
        )
        all_tasks: list[TaskRecord] = []
        seen: set[str] = set()
        for status in statuses:
            tasks = await self._store.tasks.list_by_status(status)
            for task in tasks:
                if task.task_type != "delegation" or task.task_id in seen:
                    continue
                seen.add(task.task_id)
                all_tasks.append(task)
        return all_tasks

    @staticmethod
    def _projected_tokens(
        max_turns: int,
        tool_params: object,
    ) -> int:
        estimate_per_turn = 2000
        if isinstance(tool_params, dict):
            parsed = DelegationCoordinator._coerce_int(
                tool_params.get("delegation_estimated_tokens_per_turn")
            )
            if parsed:
                estimate_per_turn = parsed
        return max(max_turns, 1) * estimate_per_turn

    @staticmethod
    def _task_token_usage(task: TaskRecord) -> int:
        if isinstance(task.result, dict):
            usage = task.result.get("usage")
            if isinstance(usage, dict):
                total = usage.get("total_tokens")
                if isinstance(total, int):
                    return total
        projected = task.metadata.get("projected_tokens")
        if isinstance(projected, int):
            return projected
        return 0

    @staticmethod
    def _coerce_int(value: object) -> int | None:
        try:
            parsed = int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _parse_positive_int(value: object) -> int | None:
        try:
            parsed = int(value) if value is not None else None  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return None
        if parsed is None:
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _rejected(status: str, reason: str, *, retryable: bool = False) -> dict[str, Any]:
        logger.info("subagent.spawn.blocked", status=status, reason=reason)
        return DelegationAcceptResult(
            accepted=False,
            task_id="",
            status=status,
            rejection_reason=reason,
            retryable=retryable,
        ).model_dump()

    def _default_max_workers(self) -> int:
        for tool in self._config.tools.tools:
            if tool.tool_id != "delegate_subagent_task":
                continue
            if tool.default_params is None:
                break
            value = tool.default_params.delegation_max_concurrent_tasks
            if value is not None and value > 0:
                return value
        return _DEFAULT_MAX_WORKERS

    @staticmethod
    def _resolve_max_turns(request: dict[str, Any], tool_params: object) -> int:
        parsed = DelegationCoordinator._parse_positive_int(request.get("max_turns"))
        if parsed is not None and parsed > 0:
            return parsed
        if isinstance(tool_params, dict):
            parsed_default = DelegationCoordinator._parse_positive_int(
                tool_params.get("delegation_default_max_turns")
            )
            if parsed_default is not None and parsed_default > 0:
                return parsed_default
        return 8

    @staticmethod
    def _resolve_backend_params(
        request: dict[str, Any],
        tool_params: object,
        *,
        log_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw = request.get("backend_params")
        backend_params = dict(raw) if isinstance(raw, dict) else {}
        # Models often fill optional fields with "" or null; treat those as unset.
        requested_effort = backend_params.get("effort")
        if not (isinstance(requested_effort, str) and requested_effort.strip()):
            backend_params.pop("effort", None)
            if isinstance(tool_params, dict):
                default_effort = tool_params.get("delegation_default_effort")
                if isinstance(default_effort, str) and default_effort.strip():
                    backend_params["effort"] = default_effort.strip()
        # Always pin the mode on the task so the backend never guesses and the
        # stored task records what the run was allowed to do.  The configured
        # mode is a ceiling: a request may only make the run more restrictive.
        context = log_context or {}
        configured_mode = resolve_permission_mode(
            tool_params.get("delegation_default_permission_mode")
            if isinstance(tool_params, dict)
            else None,
            source="delegation_default_permission_mode",
            **context,
        )
        requested_mode = resolve_permission_mode(
            backend_params.get("permission_mode"),
            configured_mode,
            source="backend_params.permission_mode",
            **context,
        )
        backend_params["permission_mode"] = clamp_permission_mode(
            requested_mode, configured_mode, **context
        )
        return backend_params

    @staticmethod
    def _resolve_timeout_seconds(request: dict[str, Any], tool_params: object) -> int:
        parsed = DelegationCoordinator._parse_positive_int(request.get("timeout_seconds"))
        if parsed is not None and parsed > 0:
            return parsed
        if isinstance(tool_params, dict):
            parsed_default = DelegationCoordinator._parse_positive_int(
                tool_params.get("delegation_default_timeout_seconds")
            )
            if parsed_default is not None and parsed_default > 0:
                return parsed_default
        return 300

    def _reap_finished_inflight(self) -> None:
        done_ids = [task_id for task_id, worker in self._inflight.items() if worker.done()]
        for task_id in done_ids:
            worker = self._inflight.pop(task_id)
            if worker.cancelled():
                continue
            exc = worker.exception()
            if exc is not None:
                logger.exception("subagent.run.worker_failed", task_id=task_id, error=str(exc))

    async def _drain_inflight(self) -> None:
        if not self._inflight:
            return
        workers = list(self._inflight.values())
        self._inflight.clear()
        await asyncio.gather(*workers, return_exceptions=True)

    @staticmethod
    def _chat_id_from_session(session_id: str) -> int | None:
        # Telegram sessions are formatted as tg:<chat_id>[:suffix]
        parts = session_id.split(":")
        if len(parts) < 2 or parts[0] != "tg":
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    @staticmethod
    def _pending_question_key(session_id: str) -> str:
        """Return a stable key for pending question lookup.

        Keying by chat_id (the stable Telegram user/chat identifier extracted
        from ``tg:{chat_id}:{suffix}``) ensures that a session switch between
        task creation and reply does not lose the answer.  Falls back to the
        full session_id for non-Telegram sessions.
        """
        parts = session_id.split(":")
        if len(parts) >= 2 and parts[0] == "tg":
            return parts[1]
        return session_id
