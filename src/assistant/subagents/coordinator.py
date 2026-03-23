"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Background delegation coordinator for provider-backed tasks.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from assistant.core.config.schemas import RuntimeConfig
from assistant.store.interfaces import StoreFacadeInterface
from assistant.store.models import TaskRecord, TaskStatus
from assistant.subagents.contracts import DelegationAcceptResult, DelegationRun
from assistant.subagents.interfaces import (
    DelegationBackendAdapterInterface,
    DelegationCoordinatorInterface,
)

logger = structlog.get_logger(__name__)

_COORDINATOR_SLEEP_SECONDS = 1.0
_DEFAULT_MAX_WORKERS = 3
_DEFAULT_BUDGET_WINDOW_SECONDS = 86400
_DEFAULT_BACKEND = "claude_code"


class DelegationCoordinator(DelegationCoordinatorInterface):
    """Coordinator that enqueues and executes delegated tasks."""

    def __init__(
        self,
        *,
        store: StoreFacadeInterface,
        config: RuntimeConfig,
        backends: list[DelegationBackendAdapterInterface],
        completion_callback: Callable[[TaskRecord], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._backends = {b.backend_id: b for b in backends}
        self._completion_callback = completion_callback
        # Optional callback for relaying AskUserQuestion to the channel layer.
        # Signature: (task_id, session_id, question, options) -> None
        # The callback is responsible only for sending the question; the answer
        # is delivered via submit_delegation_answer().
        self._question_relay_callback: (
            Callable[[str, str, str, list[str]], Awaitable[None]] | None
        ) = None
        # Pending asyncio Futures for in-flight AskUserQuestion relays.
        # Keyed by chat_id (stable Telegram identifier, extracted from session)
        # so that a session switch between task creation and reply does not lose
        # the answer.  v1 constraint: only one pending question per chat at a time.
        self._pending_questions: dict[str, asyncio.Future[str]] = {}
        self._stop = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._max_workers = self._default_max_workers()
        self._worker_sem = asyncio.Semaphore(self._max_workers)

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._worker_task is not None:
            return
        await self._recover_inflight_tasks()
        self._stop.clear()
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Stop the background polling loop."""
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
        callback: Callable[[str, str, str, list[str]], Awaitable[None]] | None,
    ) -> None:
        """Register a channel-layer callback for relaying AskUserQuestion to the user.

        The callback receives ``(task_id, session_id, question, options)`` and is
        responsible for sending the question to the channel layer (e.g. Telegram).
        The answer must be delivered back via :meth:`submit_delegation_answer`.
        """
        self._question_relay_callback = callback

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
        if await self._reaches_concurrency_limit(tool_params):
            return self._rejected(
                "rejected_policy",
                "max concurrent delegation tasks reached",
            )
        budget_reason, projected_tokens = await self._check_budget(
            session_id=session_id,
            max_turns=self._resolve_max_turns(request, tool_params),
            tool_params=tool_params,
        )
        if budget_reason is not None:
            return self._rejected("rejected_policy", budget_reason)

        ttl_seconds = self._resolve_ttl_seconds(tool_params)
        now = datetime.now(UTC)
        task_id = str(request.get("task_id") or f"dlg-{uuid.uuid4().hex[:12]}")
        metadata = {
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
            "backend_params": request.get("backend_params", {}),
        }
        logfire_ctx = request.get("logfire_context")
        if isinstance(logfire_ctx, dict) and logfire_ctx:
            metadata["logfire_context"] = logfire_ctx
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
        await self._store.tasks.create(task)
        accepted = DelegationAcceptResult(
            accepted=True,
            task_id=task_id,
            status=TaskStatus.PENDING.value,
            expires_at=task.expires_at.isoformat() if task.expires_at else None,
        )
        logger.info(
            "subagent.spawn.accepted",
            task_id=task_id,
            model_id=model_id,
            backend=backend_id,
            projected_tokens=projected_tokens,
        )
        return accepted.model_dump()

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return await self._store.tasks.get(task_id)

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            self._reap_finished_inflight()
            available_slots = max(0, self._max_workers - len(self._inflight))
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
        async with self._worker_sem:
            task = await self._store.tasks.get(task_id)
            if task is None:
                return
            await self._execute_task(task)

    async def _execute_task(self, task: TaskRecord) -> None:
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
            if updated is not None:
                await self._notify_completion(updated)
            return
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
        latest = await self._store.tasks.get(task.task_id)
        if latest is not None and latest.status == TaskStatus.CANCELLED:
            logger.info("subagent.run.cancelled", task_id=task.task_id)
            return
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
        )
        logger.info(
            "subagent.run.progress",
            task_id=task.task_id,
            backend=backend_id,
        )
        # For backends that support AskUserQuestion relay, register
        # a per-task relay closure before executing and clean it up afterwards.
        if backend.supports_relay:
            self._register_streaming_relay(backend, task)
        try:
            result = await backend.execute(run)
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
                await self._notify_completion(updated)
            return
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
            await self._notify_completion(updated)

    def _register_streaming_relay(
        self,
        backend: DelegationBackendAdapterInterface,
        task: TaskRecord,
    ) -> None:
        """Build and register a per-task question relay on a streaming backend.

        The relay closure:
        1. Calls ``_question_relay_callback`` to notify the channel layer (send to Telegram)
        2. Only after a confirmed send, registers an asyncio.Future keyed by chat_id
        3. Awaits the Future, which is resolved via :meth:`submit_delegation_answer`
        """
        if self._question_relay_callback is None:
            return
        task_id = task.task_id
        session_id = task.parent_session_id or ""
        pending_key = self._pending_question_key(session_id)
        relay_callback = self._question_relay_callback

        async def _relay(question: str, options: list[str]) -> str:
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            try:
                # Notify the channel layer; propagates on send failure so we do
                # not register a Future for a question the user never received.
                await relay_callback(task_id, session_id, question, options)
            except Exception:
                logger.exception(
                    "subagent.question_relay.send_failed",
                    task_id=task_id,
                    session_id=session_id,
                )
                future.cancel()
                # Surface a visible error so the user is not left waiting.
                await relay_callback(
                    task_id,
                    session_id,
                    "\u26a0\ufe0f Could not relay question to you \u2014 delegation task aborted.",
                    [],
                )
                return ""
            # Register only after confirmed send so has_pending_question() only
            # returns True when the user actually received the question.
            self._pending_questions[pending_key] = future
            try:
                # Wait until submit_delegation_answer() resolves the future
                return await asyncio.wait_for(asyncio.shield(future), timeout=300)
            except TimeoutError:
                logger.warning(
                    "subagent.question_relay.answer_timeout",
                    task_id=task_id,
                    session_id=session_id,
                )
                return ""
            finally:
                self._pending_questions.pop(pending_key, None)
                if not future.done():
                    future.cancel()

        backend.register_relay(task_id, _relay)

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
            return "task token budget exceeded", projected_tokens

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
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _parse_positive_int(value: object) -> int | None:
        try:
            parsed = int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        if parsed is None:
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _rejected(status: str, reason: str) -> dict[str, Any]:
        logger.info("subagent.spawn.blocked", status=status, reason=reason)
        return DelegationAcceptResult(
            accepted=False,
            task_id="",
            status=status,
            rejection_reason=reason,
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
