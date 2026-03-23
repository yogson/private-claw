"""
Component ID: CMP_API_FASTAPI_GATEWAY

FastAPI application entry point: bootstraps config and mounts routers.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from pydantic_ai.exceptions import ModelHTTPError

from assistant.admin.router import router as admin_router
from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
from assistant.api.deps import set_runtime_config
from assistant.api.routers import config as config_router
from assistant.api.routers import health
from assistant.api.routers import tasks as tasks_router
from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.ingestion.factory import build_transcription_service
from assistant.channels.telegram.ingestion.file_downloader import TelegramFileDownloader
from assistant.channels.telegram.models import ChannelResponse, MessageType, NormalizedEvent
from assistant.channels.telegram.polling import CancellationRegistry, run_polling
from assistant.channels.telegram.usage import UsageStatsService
from assistant.channels.telegram.verbose_state import VerboseStateService
from assistant.core.bootstrap import bootstrap
from assistant.core.capabilities.loader import load_capability_definitions
from assistant.core.events.mapper import NormalizedEventMapper
from assistant.core.events.models import EventSource, EventType, OrchestratorEvent
from assistant.core.orchestrator.confirmation import MemoryConfirmationService
from assistant.core.orchestrator.service import Orchestrator
from assistant.store.models import SessionRecordType
from assistant.core.session_context import (
    ActiveSessionContextInterface,
    ActiveSessionContextService,
    SessionModelContextService,
)
from assistant.memory.mem0 import Mem0MemoryWriteService, Mem0RetrievalService
from assistant.observability.logfire import configure_logfire
from assistant.observability.logging import configure_logging
from assistant.store.facade import StoreFacade
from assistant.store.idempotency.service import IngressIdempotencyService
from assistant.store.models import TaskRecord
from assistant.subagents.backends import ClaudeCodeBackendAdapter, ClaudeCodeStreamingBackendAdapter
from assistant.subagents.coordinator import DelegationCoordinator

logger = structlog.get_logger(__name__)

# Load .env before the lifespan starts so env overrides and ASSISTANT_ADMIN_TOKEN
# are available to bootstrap() and the auth layer.
load_dotenv()


def _is_token_limit_error(exc: ModelHTTPError) -> bool:
    """Return True if the error indicates the prompt exceeded the model's token limit.

    Detection is tuned for Anthropic-style error bodies (dict with error.message).
    Assumes exc.body is a dict or None; other types are treated as empty.
    """
    if exc.status_code != 400:
        return False
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    msg = str(error.get("message", "")).lower()
    return "prompt is too long" in msg or "token" in msg and "maximum" in msg


def _build_tool_call_notifier(
    adapter: TelegramAdapter,
    chat_id: int,
    session_id: str,
    trace_id: str,
) -> "Callable[[str, str], Awaitable[None]]":
    """Build an async notifier that sends a verbose tool-call message to Telegram."""
    import json as _json

    async def _notifier(tool_name: str, args_json: str) -> None:
        try:
            args = _json.loads(args_json) if args_json and args_json != "{}" else {}
        except Exception:
            args = {}
        if args:
            pretty = _json.dumps(args, ensure_ascii=False, indent=None)
            text = f"⚙️ `{tool_name}`\n```\n{pretty}\n```"
        else:
            text = f"⚙️ `{tool_name}`"
        response = ChannelResponse(
            response_id=str(uuid.uuid4()),
            channel="telegram",
            session_id=session_id,
            trace_id=trace_id,
            message_type=MessageType.TEXT,
            text=text,
            parse_mode="Markdown",
        )
        try:
            await adapter.send_response(response, chat_id=chat_id)
        except Exception:
            pass

    return _notifier


async def _session_has_user_messages(store: Any, session_id: str) -> bool:
    """Return True if the session already contains at least one USER_MESSAGE record."""
    records = await store.sessions.read_window(session_id, max_records=50)
    return any(r.record_type == SessionRecordType.USER_MESSAGE for r in records)


async def _notify_system_started(
    adapter: TelegramAdapter,
    active_session_context: ActiveSessionContextInterface,
) -> None:
    """Send 'System started.' to every chat that has an active session.

    Chat IDs are derived from the persisted session context, which maps
    'telegram:<chat_id>' context keys to session IDs.  Unknown or unparseable
    keys are silently skipped.

    Note: ``list_context_ids()`` returns *all* persisted sessions, including
    stale ones from previous runs.  No TTL filter is applied, so users whose
    sessions have long since expired will still receive the notification.
    """
    context_ids = active_session_context.list_context_ids()
    for context_id in context_ids:
        # Context IDs for Telegram take the form 'telegram:<chat_id>'
        if not context_id.startswith("telegram:"):
            continue
        raw_chat_id = context_id.removeprefix("telegram:")
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            continue
        response = ChannelResponse(
            response_id=str(uuid.uuid4()),
            channel="telegram",
            session_id="__system__",
            trace_id=str(uuid.uuid4()),
            message_type=MessageType.TEXT,
            text="System started.",
        )
        try:
            await adapter.send_response(response, chat_id=chat_id)
            logger.info("system.started.notified", chat_id=chat_id)
        except Exception:
            logger.warning("system.started.notify_failed", chat_id=chat_id)


def _build_orchestrator_handler(
    adapter: TelegramAdapter,
    orchestrator: Orchestrator,
    delegation_coordinator: DelegationCoordinator | None = None,
    memory_confirmations: MemoryConfirmationService | None = None,
    usage_service: Any = None,
    cancellation_registry: CancellationRegistry | None = None,
    verbose_state: VerboseStateService | None = None,
    store: Any = None,
) -> Callable[[NormalizedEvent], Awaitable[ChannelResponse | None]]:
    mapper = NormalizedEventMapper()

    async def _handler(event: NormalizedEvent) -> ChannelResponse | None:
        # If a streaming delegation task is waiting for user input on this session,
        # treat the incoming message as the answer rather than a normal turn.
        if (
            event.callback_query is None
            and event.text
            and delegation_coordinator is not None
            and delegation_coordinator.has_pending_question(event.session_id)
        ):
            submitted = delegation_coordinator.submit_delegation_answer(
                event.session_id, event.text
            )
            if submitted:
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="Answer received. The task will continue.",
                )
        # Route inline-keyboard button taps for delegation question options.
        if event.callback_query is not None and adapter.is_delegation_question_callback(event):
            resolution = adapter.consume_delegation_question_callback(event)
            if resolution is None:
                logger.info("delegation.question.token_invalid", trace_id=event.trace_id)
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="Invalid or expired delegation answer.",
                )
            q_session_id, answer_text = resolution
            if delegation_coordinator is None or not delegation_coordinator.has_pending_question(q_session_id):
                logger.info("delegation.question.expired", session_id=q_session_id, trace_id=event.trace_id)
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=q_session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="The question has already been answered or timed out.",
                )
            submitted = delegation_coordinator.submit_delegation_answer(q_session_id, answer_text)
            if submitted:
                logger.info("delegation.question.answered", session_id=q_session_id, trace_id=event.trace_id)
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=q_session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="Answer received. The task will continue.",
                )
            logger.warning("delegation.question.submit_failed", session_id=q_session_id, trace_id=event.trace_id)
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=q_session_id,
                trace_id=event.trace_id,
                message_type=MessageType.TEXT,
                text="Could not submit answer. The task may have already completed.",
            )
        if adapter.is_stop_request(event):
            cancelled = (
                cancellation_registry.cancel(event.session_id)
                if cancellation_registry is not None
                else False
            )
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=event.session_id,
                trace_id=event.trace_id,
                message_type=MessageType.TEXT,
                text="Stopped." if cancelled else "Nothing is currently running.",
            )
        if adapter.is_verbose_request(event):
            chat_id = int(event.metadata.get("chat_id", 0))
            now_on = (
                verbose_state.toggle(chat_id) if verbose_state is not None and chat_id else False
            )
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=event.session_id,
                trace_id=event.trace_id,
                message_type=MessageType.TEXT,
                text="Verbose mode *on* — tool calls will be shown."
                if now_on
                else "Verbose mode *off*.",
                parse_mode="Markdown",
            )
        if adapter.is_session_new_request(event):
            session_id = adapter.start_new_session(event)
            if session_id is None:
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="Could not start a new session for this chat.",
                )
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=session_id,
                trace_id=event.trace_id,
                message_type=MessageType.TEXT,
                text="Started a new session. Continue your conversation.",
            )
        if adapter.is_session_reset_request(event):
            if not adapter.is_session_reset_available():
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="Session reset is not available.",
                )
            if usage_service is not None:
                await usage_service.archive_session_usage(event.session_id, event.user_id)
            cleared = await adapter.reset_session_context(event)
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=event.session_id,
                trace_id=event.trace_id,
                message_type=MessageType.TEXT,
                text=(
                    "Session context reset. Starting fresh."
                    if cleared
                    else "Session context is already empty."
                ),
            )
        if adapter.is_session_resume_request(event):
            chat_id = int(event.metadata.get("chat_id", 0))
            return await adapter.build_session_menu_response(
                chat_id, event.session_id, event.trace_id
            )
        if adapter.is_session_resume_callback(event):
            session_id = adapter.handle_session_resume_callback(event)
            if session_id:
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="Switched to session. Continue your conversation.",
                )
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=event.session_id,
                trace_id=event.trace_id,
                message_type=MessageType.TEXT,
                text="Invalid or expired session selection.",
            )
        if adapter.is_model_request(event):
            chat_id = int(event.metadata.get("chat_id", 0))
            return await adapter.build_model_menu_response(
                chat_id, event.session_id, event.trace_id
            )
        if adapter.is_model_callback_request(event):
            model_id = adapter.handle_model_callback(event)
            if model_id:
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text=f"Model set to `{model_id}`. Continue your conversation.",
                    parse_mode="Markdown",
                )
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=event.session_id,
                trace_id=event.trace_id,
                message_type=MessageType.TEXT,
                text="Invalid or expired model selection.",
            )
        if adapter.is_capabilities_request(event):
            chat_id = int(event.metadata.get("chat_id", 0))
            if store is not None and await _session_has_user_messages(store, event.session_id):
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text=(
                        "Capabilities can only be changed in a fresh session. "
                        "Use /new to start a new session first."
                    ),
                )
            return await adapter.build_capabilities_menu_response(
                chat_id, event.session_id, event.trace_id
            )
        if adapter.is_capabilities_callback_request(event):
            chat_id = int(event.metadata.get("chat_id", 0))
            if store is not None and await _session_has_user_messages(store, event.session_id):
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text=(
                        "Capabilities can only be changed in a fresh session. "
                        "Use /new to start a new session first."
                    ),
                )
            capability_id = adapter.handle_capabilities_callback(event)
            if capability_id is None:
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="Invalid or expired capability selection.",
                )
            return await adapter.build_capabilities_menu_response(
                chat_id, event.session_id, event.trace_id
            )
        if adapter.is_memory_confirmation_callback(event):
            if memory_confirmations is None:
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="Memory confirmation is not available.",
                )
            mem_resolution = adapter.consume_memory_confirmation_callback(event)
            if mem_resolution is None:
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="Invalid or expired confirmation action.",
                )
            mem_session_id, mem_tool_call_id, approve = mem_resolution
            ok, message = await memory_confirmations.resolve_pending(
                session_id=mem_session_id,
                tool_call_id=mem_tool_call_id,
                approve=approve,
                user_id=event.user_id,
            )
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=mem_session_id if ok else event.session_id,
                trace_id=event.trace_id,
                message_type=MessageType.TEXT,
                text=message,
            )
        if event.callback_query is not None and adapter.is_task_callback(event):
            parsed = adapter.parse_task_callback(event)
            if parsed is None:
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text="Invalid or expired task action.",
                )
            task_id, action = parsed
            task = None
            if delegation_coordinator is not None:
                task = await delegation_coordinator.get_task(task_id)
            if task is None:
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text=f"Task `{task_id}` not found.",
                    parse_mode="Markdown",
                )
            if action == "status":
                text = (
                    f"Task `{task.task_id}` status: *{task.status.value}*\nType: `{task.task_type}`"
                )
                if task.error:
                    text += f"\nError: {task.error}"
            else:
                summary = ""
                if isinstance(task.result, dict):
                    summary = str(task.result.get("summary", "")).strip()
                text = summary or f"Task `{task.task_id}` has no summary yet."
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=task.parent_session_id or event.session_id,
                trace_id=event.trace_id,
                message_type=MessageType.TEXT,
                text=text,
                parse_mode="Markdown",
            )
        if adapter.is_usage_request(event):
            if usage_service is not None:
                return cast(
                    ChannelResponse,
                    await usage_service.build_usage_response(event),
                )
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=event.session_id,
                trace_id=event.trace_id,
                message_type=MessageType.TEXT,
                text="Usage stats not available.",
            )

        orch_event = mapper.map(event)
        chat_id = int(event.metadata.get("chat_id", 0))
        model_override = adapter.get_model_override(chat_id) if chat_id else None
        if model_override:
            orch_event = orch_event.model_copy(update={"model_id_override": model_override})
        capabilities_override = adapter.get_capabilities_override(chat_id) if chat_id else None
        if capabilities_override is not None:
            orch_event = orch_event.model_copy(
                update={"capabilities_override": capabilities_override}
            )
        notifier = None
        if chat_id and verbose_state is not None and verbose_state.is_enabled(chat_id):
            notifier = _build_tool_call_notifier(
                adapter, chat_id, orch_event.session_id, event.trace_id
            )
        try:
            orch_result = await orchestrator.execute_turn(orch_event, tool_call_notifier=notifier)
        except ModelHTTPError as exc:
            if _is_token_limit_error(exc):
                logger.warning(
                    "orchestrator.token_limit_exceeded",
                    session_id=orch_event.session_id,
                    trace_id=event.trace_id,
                    status_code=exc.status_code,
                )
                if usage_service is not None:
                    await usage_service.archive_session_usage(orch_event.session_id, event.user_id)
                if adapter.is_session_reset_available():
                    await adapter.reset_session_context(event)
                    reset_msg = "Session has been reset. Please try your message again."
                else:
                    reset_msg = (
                        "Session reset is not available. Please use /reset or start a new session."
                    )
                return ChannelResponse(
                    response_id=str(uuid.uuid4()),
                    channel="telegram",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    message_type=MessageType.TEXT,
                    text=f"Conversation history exceeded the model limit. {reset_msg}",
                )
            raise
        if orch_result is None:
            return None
        session_id = orch_event.session_id
        response_text = orch_result.text
        if orch_result.pending_ask is not None:
            prompt_text = orch_result.pending_ask.question
            if response_text.strip():
                prompt_text = f"{response_text}\n\n{prompt_text}"
            return adapter.build_ask_question_response(
                session_id=session_id,
                trace_id=event.trace_id,
                question=prompt_text,
                options=orch_result.pending_ask.options,
            )
        if memory_confirmations is not None:
            pending = await memory_confirmations.list_pending(session_id)
            if not pending:
                logger.debug(
                    "memory.confirmation.no_pending",
                    session_id=session_id,
                    trace_id=event.trace_id,
                )
            if pending:
                chat_id = int(event.metadata.get("chat_id", 0))
                if not chat_id:
                    logger.warning(
                        "memory.confirmation.skipped_no_chat_id",
                        session_id=session_id,
                        trace_id=event.trace_id,
                    )
                if chat_id:
                    logger.info(
                        "memory.confirmation.showing_ui",
                        session_id=session_id,
                        tool_call_id=pending[0].tool_call_id,
                        trace_id=event.trace_id,
                    )
                    prompt_text = response_text
                    proposal_reason = pending[0].proposal.reason.strip()
                    if proposal_reason:
                        prompt_text = f"{response_text}\n\nPending memory update: {proposal_reason}"
                    return adapter.build_memory_confirmation_response(
                        chat_id=chat_id,
                        session_id=session_id,
                        trace_id=event.trace_id,
                        tool_call_id=pending[0].tool_call_id,
                        prompt_text=prompt_text,
                    )
        return ChannelResponse(
            response_id=event.event_id,
            channel="telegram",
            session_id=event.session_id,
            trace_id=event.trace_id,
            message_type=MessageType.TEXT,
            text=response_text,
        )

    return _handler


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


def _build_question_relay_handler(
    adapter: TelegramAdapter,
) -> "Callable[[str, str, str, list[str]], Awaitable[None]]":
    """Build a question-relay callback that sends AskUserQuestion to Telegram.

    The callback sends the question as a Telegram message.  The coordinator
    manages the asyncio.Future lifecycle and waits for the answer via
    :meth:`DelegationCoordinator.submit_delegation_answer`.
    """

    async def _relay(
        task_id: str, session_id: str, question: str, options: list[str]
    ) -> None:
        chat_id = DelegationCoordinator._chat_id_from_session(session_id)
        if chat_id is None:
            logger.warning(
                "subagent.question_relay.no_chat_id",
                task_id=task_id,
                session_id=session_id,
            )
            return

        response = adapter.build_delegation_question_response(
            chat_id=chat_id,
            session_id=session_id,
            trace_id=task_id,
            question=f"[Delegation task] {question}",
            options=options,
        )
        # Propagate send errors so the coordinator can abort the wait and
        # surface a visible error to the user rather than silently swallowing.
        await adapter.send_response(response, chat_id=chat_id)

    return _relay


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    runtime_config = bootstrap()
    set_runtime_config(runtime_config)
    log_path = configure_logging(runtime_config.app)
    logger.info("logging.started", log_file=str(log_path))
    logfire_configured = configure_logfire(runtime_config.app)
    logger.info("logfire.status", configured=logfire_configured)
    app.state.runtime_config = runtime_config
    app.state.telegram_adapter = None
    app.state.attachment_downloader = None
    app.state.store = None
    app.state.delegation_coordinator = None

    store: StoreFacade | None = None
    polling_task: asyncio.Task[None] | None = None
    polling_stop = asyncio.Event()

    if runtime_config.telegram.enabled:
        data_root = Path(runtime_config.app.data_root)

        store = StoreFacade(
            data_root=data_root,
            lock_ttl_seconds=runtime_config.store.lock_ttl_seconds,
            idempotency_ttl_seconds=runtime_config.store.idempotency_retention_seconds,
        )
        await store.initialize()
        app.state.store = store

        idempotency = IngressIdempotencyService(
            store.idempotency,
            default_ttl_seconds=runtime_config.store.idempotency_retention_seconds,
        )
        if not runtime_config.memory.api_key.strip():
            raise SystemExit(
                "Mem0 api_key is required. Set ASSISTANT_MEMORY_API_KEY or "
                "configure api_key in config/memory.yaml"
            ) from None
        memory_writer = Mem0MemoryWriteService(runtime_config.memory, data_root=data_root)
        memory_retrieval = Mem0RetrievalService(runtime_config.memory)
        memory_confirmations = MemoryConfirmationService(store.sessions, memory_writer)
        attachment_downloader = TelegramFileDownloader(
            bot_token=runtime_config.telegram.bot_token,
            max_size_bytes=runtime_config.telegram.max_attachment_size_bytes,
        )
        app.state.attachment_downloader = attachment_downloader

        model_id = runtime_config.model.default_model_id
        if not model_id.startswith("anthropic:"):
            model_id = f"anthropic:{model_id}"
        pydantic_ai_adapter = PydanticAITurnAdapter(
            model_id=model_id,
            max_tokens=runtime_config.model.max_tokens_default,
            config=runtime_config,
        )

        transcription_service = build_transcription_service(runtime_config.telegram)
        active_session_context = ActiveSessionContextService(
            data_root / "runtime" / "active_session_context.json"
        )
        model_context = SessionModelContextService(
            data_root / "runtime" / "active_model_context.json"
        )
        capability_definitions = load_capability_definitions(
            config_dir=runtime_config.config_dir
        )
        adapter = TelegramAdapter(
            runtime_config.telegram,
            transcription_service=transcription_service,
            session_store=store.sessions,
            active_session_context=active_session_context,
            model_context=model_context,
            model_allowlist=runtime_config.model.model_allowlist,
            default_model_id=runtime_config.model.default_model_id,
            capability_definitions=capability_definitions,
            default_capabilities=runtime_config.capabilities.enabled_capabilities,
        )
        app.state.telegram_adapter = adapter

        streaming_backend = ClaudeCodeStreamingBackendAdapter()
        delegation_coordinator = DelegationCoordinator(
            store=store,
            config=runtime_config,
            backends=[ClaudeCodeBackendAdapter(), streaming_backend],
        )
        await delegation_coordinator.start()
        app.state.delegation_coordinator = delegation_coordinator
        orchestrator = Orchestrator(
            store=store,
            config=runtime_config,
            idempotency=idempotency,
            attachment_downloader=attachment_downloader,
            memory_writer=memory_writer,
            memory_retrieval=memory_retrieval,
            pydantic_ai_adapter=pydantic_ai_adapter,
            delegation_coordinator=delegation_coordinator,
        )
        delegation_coordinator.set_completion_callback(
            _build_delegation_feedback_handler(orchestrator, adapter)
        )
        delegation_coordinator.set_question_relay_callback(
            _build_question_relay_handler(adapter)
        )

        usage_service = UsageStatsService(
            session_store=store.sessions,
            archive_dir=data_root / "runtime" / "usage_archive",
            default_model_id=runtime_config.model.default_model_id,
        )
        cancellation_registry = CancellationRegistry()
        verbose_state = VerboseStateService(
            storage_path=data_root / "runtime" / "verbose_state.json"
        )
        handler = _build_orchestrator_handler(
            adapter,
            orchestrator,
            delegation_coordinator,
            memory_confirmations,
            usage_service=usage_service,
            cancellation_registry=cancellation_registry,
            verbose_state=verbose_state,
            store=store,
        )

        async def _dispatch(event: NormalizedEvent) -> ChannelResponse | None:
            return await handler(event)

        await _notify_system_started(adapter, active_session_context)

        polling_task = asyncio.create_task(
            run_polling(
                adapter,
                runtime_config.telegram,
                _dispatch,
                stop_event=polling_stop,
                cancellation_registry=cancellation_registry,
            )
        )

    try:
        yield
    finally:
        if polling_task is not None:
            polling_stop.set()
            try:
                await asyncio.wait_for(polling_task, timeout=10.0)
            except TimeoutError:
                polling_task.cancel()
                with suppress(asyncio.CancelledError):
                    await polling_task
        adapter = app.state.telegram_adapter
        if adapter is not None:
            await adapter.close()
            logger.info("telegram.polling.stopped")
        downloader = app.state.attachment_downloader
        if downloader is not None:
            await downloader.close()
        if store is not None:
            coordinator = app.state.delegation_coordinator
            if coordinator is not None:
                await coordinator.stop()
            await store.shutdown()


app = FastAPI(
    title="Private Claw 🦞 v1",
    version="1.0.0",
    lifespan=_lifespan,
)

app.include_router(health.router)
app.include_router(config_router.router)
app.include_router(tasks_router.router)
app.include_router(admin_router)


@app.get("/admin", include_in_schema=False)
async def admin_root() -> RedirectResponse:
    """Redirects bare /admin to the config dashboard."""
    return RedirectResponse("/admin/config", status_code=302)
