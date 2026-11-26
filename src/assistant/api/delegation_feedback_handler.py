import json
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from datetime import UTC, datetime

import structlog

from assistant.api.confirmation_handler import (
    deliver_with_confirmations,
    discard_confirmations_on_error,
)
from assistant.api.orchestrator_handler import _build_tool_call_notifier
from assistant.channels.telegram import ChannelResponse, MessageType, TelegramAdapter
from assistant.channels.telegram.verbose_state import VerboseStateService
from assistant.core.confirmation import ConfirmationService
from assistant.core.events.models import (
    SYSTEM_USER_ID,
    EventSource,
    EventType,
    OrchestratorEvent,
)
from assistant.core.orchestrator.service import Orchestrator
from assistant.store.interfaces import LockAcquisitionError
from assistant.store.models import TaskRecord
from assistant.subagents.coordinator import DelegationCoordinator
from assistant.subagents.workspaces import task_workspace_info

logger = structlog.get_logger(__name__)

_SUMMARY_EXCERPT_CHARS = 500


def _lock_timeout_notice(task: TaskRecord, summary: str) -> str:
    """User-facing notice for a completion whose follow-up turn could not run."""
    text = (
        f"Delegated task {task.task_id} finished ({task.status.value}), but the session "
        "stayed busy with another turn, so I could not process the result. "
        f"Ask me to check the status of {task.task_id} to continue."
    )
    if summary:
        excerpt = summary[:_SUMMARY_EXCERPT_CHARS]
        if len(summary) > _SUMMARY_EXCERPT_CHARS:
            excerpt += "..."
        text += f"\n\nSummary: {excerpt}"
    return text


def _build_delegation_feedback_handler(
    orchestrator: Orchestrator,
    adapter: TelegramAdapter,
    confirmations: ConfirmationService | None = None,
    verbose_state: VerboseStateService | None = None,
) -> Callable[[TaskRecord], Awaitable[None]]:
    async def _handler(task: TaskRecord) -> None:
        session_id = task.parent_session_id
        if not session_id:
            return
        trace_id = str(task.metadata.get("trace_id") or task.task_id)
        result = task.result if isinstance(task.result, dict) else {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
        raw_stdout = artifacts.get("raw_stdout") if isinstance(artifacts, dict) else ""
        stdout_excerpt = (
            raw_stdout[:2000] + "... [truncated]"
            if isinstance(raw_stdout, str) and len(raw_stdout) > 2000
            else raw_stdout
            if isinstance(raw_stdout, str)
            else ""
        )
        payload = {
            "type": "delegation_completion",
            "task_id": task.task_id,
            "status": task.status.value,
            "objective": task.metadata.get("objective"),
            "summary": result.get("summary") if isinstance(result.get("summary"), str) else "",
            "error": task.error,
            "usage": usage,
            "backend": task.metadata.get("backend"),
            "model_id": task.metadata.get("model_id"),
            "parent_turn_id": task.parent_turn_id,
            # Reuse workspace.branch as ``branch`` for follow-up rounds of the same work.
            "workspace": task_workspace_info(task.metadata),
        }
        if stdout_excerpt:
            payload["stdout_excerpt"] = stdout_excerpt
        capabilities_override = adapter.get_capabilities_override(session_id)
        event = OrchestratorEvent(
            event_id=f"delegation-feedback-{task.task_id}-{task.status.value}",
            event_type=EventType.SYSTEM_CONTROL_EVENT,
            source=EventSource.SYSTEM,
            session_id=session_id,
            user_id=str(task.metadata.get("requested_by_user_id") or SYSTEM_USER_ID),
            created_at=datetime.now(UTC),
            trace_id=trace_id,
            text="[[DELEGATION_COMPLETED]]\n" + json.dumps(payload, separators=(",", ":")),
            metadata={"delegation_feedback": True, "task_id": task.task_id},
            capabilities_override=capabilities_override,
        )
        chat_id = DelegationCoordinator._chat_id_from_session(session_id)
        # This follow-up turn runs the full agent with all its tools, so in verbose mode
        # its tool calls must be shown like those of a user-initiated turn.
        notifier = None
        if chat_id and verbose_state is not None:
            notifier = _build_tool_call_notifier(
                adapter, chat_id, session_id, trace_id, verbose_state
            )
        logfire_ctx = task.metadata.get("logfire_context")
        with ExitStack() as stack:
            if isinstance(logfire_ctx, dict) and logfire_ctx:
                try:
                    import logfire

                    stack.enter_context(logfire.attach_context(logfire_ctx))
                except Exception:
                    pass
            try:
                result_msg = await orchestrator.execute_turn(event, tool_call_notifier=notifier)
            except LockAcquisitionError as exc:
                # The session stayed busy past the lock wait timeout. The event id is
                # already registered as processed, so report the completion to the user
                # instead of dropping it silently.
                logger.error(
                    "delegation_feedback.lock_timeout",
                    task_id=task.task_id,
                    session_id=session_id,
                    trace_id=trace_id,
                    event_id=event.event_id,
                    error=str(exc),
                )
                if chat_id is not None:
                    summary = payload["summary"] if isinstance(payload["summary"], str) else ""
                    await adapter.send_response(
                        ChannelResponse(
                            response_id=event.event_id,
                            channel="telegram",
                            session_id=session_id,
                            trace_id=trace_id,
                            message_type=MessageType.TEXT,
                            text=_lock_timeout_notice(task, summary),
                        ),
                        chat_id=chat_id,
                    )
                return
        if result_msg is None:
            return
        async with discard_confirmations_on_error(confirmations, result_msg.confirmations):
            response: ChannelResponse | None = None
            if result_msg.pending_ask is not None:
                prompt_text = result_msg.pending_ask.question
                if result_msg.text.strip():
                    prompt_text = f"{result_msg.text}\n\n{prompt_text}"
                response = adapter.build_ask_question_response(
                    session_id=session_id,
                    trace_id=trace_id,
                    question=prompt_text,
                    options=result_msg.pending_ask.options,
                )
            elif result_msg.text.strip():
                response = ChannelResponse(
                    response_id=event.event_id,
                    channel="telegram",
                    session_id=session_id,
                    trace_id=trace_id,
                    message_type=MessageType.TEXT,
                    text=result_msg.text,
                )
        response = await deliver_with_confirmations(
            adapter,
            confirmations,
            response=response,
            confirmations=result_msg.confirmations,
            chat_id=chat_id,
            trace_id=trace_id,
        )
        if response is not None and chat_id is not None:
            await adapter.send_response(response, chat_id=chat_id)

    return _handler
