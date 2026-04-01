import json
from datetime import datetime, UTC
from typing import Callable, Awaitable

from assistant.channels.telegram import TelegramAdapter, ChannelResponse, MessageType
from assistant.core.events.models import OrchestratorEvent, EventType, EventSource
from assistant.core.orchestrator.service import Orchestrator
from assistant.store.models import TaskRecord
from assistant.subagents.coordinator import DelegationCoordinator


def _build_delegation_feedback_handler(
    orchestrator: Orchestrator,
    adapter: TelegramAdapter,
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
        }
        if stdout_excerpt:
            payload["stdout_excerpt"] = stdout_excerpt
        capabilities_override = adapter.get_capabilities_override(session_id)
        event = OrchestratorEvent(
            event_id=f"delegation-feedback-{task.task_id}-{task.status.value}",
            event_type=EventType.SYSTEM_CONTROL_EVENT,
            source=EventSource.SYSTEM,
            session_id=session_id,
            user_id=str(task.metadata.get("requested_by_user_id") or "system"),
            created_at=datetime.now(UTC),
            trace_id=trace_id,
            text="[[DELEGATION_COMPLETED]]\n" + json.dumps(payload, separators=(",", ":")),
            metadata={"delegation_feedback": True, "task_id": task.task_id},
            capabilities_override=capabilities_override,
        )
        logfire_ctx = task.metadata.get("logfire_context")
        if isinstance(logfire_ctx, dict) and logfire_ctx:
            try:
                import logfire

                with logfire.attach_context(logfire_ctx):
                    result_msg = await orchestrator.execute_turn(event)
            except Exception:
                result_msg = await orchestrator.execute_turn(event)
        else:
            result_msg = await orchestrator.execute_turn(event)
        if result_msg is None:
            return
        if not result_msg.text.strip() and result_msg.pending_ask is None:
            return
        chat_id = DelegationCoordinator._chat_id_from_session(session_id)
        if chat_id is None:
            return
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
        else:
            response = ChannelResponse(
                response_id=event.event_id,
                channel="telegram",
                session_id=session_id,
                trace_id=trace_id,
                message_type=MessageType.TEXT,
                text=result_msg.text,
            )
        await adapter.send_response(response, chat_id=chat_id)

    return _handler
