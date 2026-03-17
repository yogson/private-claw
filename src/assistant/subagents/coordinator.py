"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Background delegation coordinator for staged provider-backed tasks.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from assistant.core.capabilities.schemas import CapabilityDefinition, DelegationWorkflowDefinition
from assistant.core.config.schemas import RuntimeConfig
from assistant.store.interfaces import StoreFacadeInterface
from assistant.store.models import TaskRecord, TaskStatus
from assistant.subagents.contracts import DelegationAcceptResult, DelegationStageRun
from assistant.subagents.interfaces import (
    DelegationBackendAdapterInterface,
    DelegationCoordinatorInterface,
)

logger = structlog.get_logger(__name__)

_COORDINATOR_SLEEP_SECONDS = 1.0
_DEFAULT_MAX_WORKERS = 3
_DEFAULT_BUDGET_WINDOW_SECONDS = 86400


class DelegationCoordinator(DelegationCoordinatorInterface):
    """Coordinator that enqueues and executes delegated tasks."""

    def __init__(
        self,
        *,
        store: StoreFacadeInterface,
        config: RuntimeConfig,
        capability_definitions: dict[str, CapabilityDefinition],
        backends: list[DelegationBackendAdapterInterface],
        completion_callback: Callable[[TaskRecord], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._capability_definitions = capability_definitions
        self._backends = {b.backend_id: b for b in backends}
        self._completion_callback = completion_callback
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
            workflow_id=request.get("workflow_id"),
        )
        objective = str(request.get("objective", "")).strip()
        if not objective:
            return self._rejected("rejected_invalid", "objective is required")
        workflow = self._resolve_workflow(request.get("workflow_id"))
        if workflow is None:
            return self._rejected(
                "rejected_invalid",
                "no delegation workflow is configured in enabled capabilities",
            )
        backend_id = workflow.backend.strip()
        if backend_id not in self._backends:
            return self._rejected("rejected_invalid", f"backend '{backend_id}' is not available")

        tool_params = request.get("tool_params", {})
        if not self._backend_allowed(backend_id, tool_params):
            return self._rejected(
                "rejected_policy",
                f"backend '{backend_id}' is not in delegation_allowed_backends",
            )
        if not self._models_allowed(workflow, tool_params):
            return self._rejected(
                "rejected_policy",
                "one or more workflow stage models are not allowlisted",
            )
        if await self._reaches_concurrency_limit(tool_params):
            return self._rejected(
                "rejected_policy",
                "max concurrent delegation tasks reached",
            )
        budget_reason, projected_tokens = await self._check_budget(
            session_id=session_id,
            workflow=workflow,
            tool_params=tool_params,
            request=request,
        )
        if budget_reason is not None:
            return self._rejected("rejected_policy", budget_reason)

        ttl_seconds = self._resolve_ttl_seconds(tool_params)
        now = datetime.now(UTC)
        task_id = str(request.get("task_id") or f"dlg-{uuid.uuid4().hex[:12]}")
        metadata = {
            "kind": "delegation",
            "workflow_id": workflow.workflow_id,
            "backend": backend_id,
            "objective": objective,
            "result_format": workflow.result_format,
            "stages": [stage.model_dump() for stage in workflow.stages if stage.enabled],
            "stage_outputs": [],
            "requested_by_user_id": user_id,
            "trace_id": trace_id,
            "chat_id": request.get("chat_id") or self._chat_id_from_session(session_id),
            "projected_tokens": projected_tokens,
            "tool_request_metadata": request.get("metadata", {}),
        }
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
            workflow_id=workflow.workflow_id,
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
            workflow_id=task.metadata.get("workflow_id"),
        )
        objective = str(task.metadata.get("objective", ""))
        stage_outputs: list[dict[str, Any]] = list(task.metadata.get("stage_outputs", []))
        stages = task.metadata.get("stages", [])
        if not isinstance(stages, list) or not stages:
            await self._store.tasks.update_status(
                task.task_id,
                TaskStatus.FAILED,
                error="delegation workflow has no enabled stages",
            )
            return
        for idx, stage in enumerate(stages):
            latest = await self._store.tasks.get(task.task_id)
            if latest is not None and latest.status == TaskStatus.CANCELLED:
                logger.info("subagent.run.cancelled", task_id=task.task_id)
                return
            await self._store.tasks.heartbeat(task.task_id)
            run = DelegationStageRun(
                task_id=task.task_id,
                stage_id=str(stage.get("stage_id", f"stage-{idx + 1}")),
                purpose=str(stage.get("purpose", "")),
                model_id=str(stage.get("model_id", "")),
                objective=objective,
                timeout_seconds=int(stage.get("timeout_seconds", 300)),
                max_turns=int(stage.get("max_turns", 8)),
                stage_index=idx,
                prior_stage_outputs=stage_outputs,
                backend_params={
                    **task.metadata.get("backend_params", {}),
                    **(stage.get("backend_params", {}) if isinstance(stage, dict) else {}),
                },
            )
            logger.info(
                "subagent.run.progress",
                task_id=task.task_id,
                stage_id=run.stage_id,
                stage_index=idx,
                backend=backend_id,
            )
            result = await backend.execute_stage(run)
            stage_outputs.append(
                {
                    "stage_id": run.stage_id,
                    "purpose": run.purpose,
                    "ok": result.ok,
                    "output_text": result.output_text,
                    "error": result.error,
                    "usage": result.usage,
                }
            )
            task.metadata["stage_outputs"] = stage_outputs
            task = await self._store.tasks.update(task)
            if not result.ok:
                updated = await self._store.tasks.update_status(
                    task.task_id,
                    TaskStatus.FAILED,
                    error=result.error or f"stage {run.stage_id} failed",
                    result={"stage_outputs": stage_outputs},
                )
                if updated is not None:
                    logger.warning(
                        "subagent.run.failed",
                        task_id=task.task_id,
                        stage_id=run.stage_id,
                        error=updated.error,
                    )
                    await self._notify_completion(updated)
                return
        summary = stage_outputs[-1].get("output_text", "") if stage_outputs else ""
        updated = await self._store.tasks.update_status(
            task.task_id,
            TaskStatus.COMPLETED,
            result={"stage_outputs": stage_outputs, "summary": summary},
        )
        if updated is not None:
            logger.info(
                "subagent.run.completed",
                task_id=task.task_id,
                stage_count=len(stage_outputs),
            )
            await self._notify_completion(updated)

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

    def _resolve_workflow(self, workflow_id: object) -> DelegationWorkflowDefinition | None:
        enabled_caps = self._enabled_capabilities()
        for cap_id in enabled_caps:
            definition = self._capability_definitions.get(cap_id)
            if definition is None or definition.delegation is None:
                continue
            if workflow_id is None:
                return definition.delegation
            if definition.delegation.workflow_id == str(workflow_id):
                return definition.delegation
        return None

    def _enabled_capabilities(self) -> list[str]:
        denied = frozenset(self._config.capabilities.denied_capabilities)
        return [c for c in self._config.capabilities.enabled_capabilities if c not in denied]

    @staticmethod
    def _backend_allowed(backend: str, tool_params: object) -> bool:
        if not isinstance(tool_params, dict):
            return True
        allow = tool_params.get("delegation_allowed_backends")
        if not isinstance(allow, list) or not allow:
            return True
        return backend in {str(x) for x in allow}

    @staticmethod
    def _models_allowed(workflow: DelegationWorkflowDefinition, tool_params: object) -> bool:
        if not isinstance(tool_params, dict):
            return True
        allow = tool_params.get("delegation_model_allowlist")
        if not isinstance(allow, list) or not allow:
            return True
        allowed = {str(x) for x in allow}
        for stage in workflow.stages:
            if stage.enabled and stage.model_id not in allowed:
                return False
        return True

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
        workflow: DelegationWorkflowDefinition,
        tool_params: object,
        request: dict[str, Any],
    ) -> tuple[str | None, int]:
        projected_tokens = self._projected_tokens(workflow, tool_params, request)
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
        workflow: DelegationWorkflowDefinition,
        tool_params: object,
        request: dict[str, Any],
    ) -> int:
        direct = request.get("max_tokens")
        if isinstance(direct, int) and direct > 0:
            return direct
        if isinstance(direct, str) and direct.isdigit():
            return int(direct)
        estimate_per_turn = 2000
        if isinstance(tool_params, dict):
            parsed = DelegationCoordinator._coerce_int(
                tool_params.get("delegation_estimated_tokens_per_turn")
            )
            if parsed:
                estimate_per_turn = parsed
        total_turns = sum(stage.max_turns for stage in workflow.stages if stage.enabled)
        return total_turns * estimate_per_turn

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
