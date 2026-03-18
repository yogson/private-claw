"""
Component ID: CMP_API_FASTAPI_GATEWAY

FastAPI application entry point: bootstraps config and mounts routers.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

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
from assistant.channels.telegram.polling import run_polling
from assistant.channels.telegram.usage import UsageStatsService
from assistant.core.bootstrap import bootstrap
from assistant.core.events.mapper import NormalizedEventMapper
from assistant.core.orchestrator.confirmation import MemoryConfirmationService
from assistant.core.orchestrator.service import Orchestrator
from assistant.core.session_context import (
    ActiveSessionContextService,
    SessionModelContextService,
)
from assistant.memory.mem0 import Mem0MemoryWriteService, Mem0RetrievalService
from assistant.observability.logging import configure_logging
from assistant.store.facade import StoreFacade
from assistant.store.idempotency.service import IngressIdempotencyService
from assistant.subagents.backends import ClaudeCodeBackendAdapter
from assistant.subagents.coordinator import DelegationCoordinator

logger = structlog.get_logger(__name__)

# Load .env before the lifespan starts so env overrides and ASSISTANT_ADMIN_TOKEN
# are available to bootstrap() and the auth layer.
load_dotenv()


def _build_orchestrator_handler(
    adapter: TelegramAdapter,
    orchestrator: Orchestrator,
    delegation_coordinator: DelegationCoordinator | None = None,
    memory_confirmations: MemoryConfirmationService | None = None,
    usage_service: Any = None,
) -> Callable[[NormalizedEvent], Awaitable[ChannelResponse | None]]:
    mapper = NormalizedEventMapper()

    async def _handler(event: NormalizedEvent) -> ChannelResponse | None:
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
        orch_result = await orchestrator.execute_turn(orch_event)
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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    runtime_config = bootstrap()
    set_runtime_config(runtime_config)
    log_path = configure_logging(runtime_config.app)
    logger.info("logging.started", log_file=str(log_path))
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
        adapter = TelegramAdapter(
            runtime_config.telegram,
            transcription_service=transcription_service,
            session_store=store.sessions,
            active_session_context=active_session_context,
            model_context=model_context,
            model_allowlist=runtime_config.model.model_allowlist,
            default_model_id=runtime_config.model.default_model_id,
        )
        app.state.telegram_adapter = adapter

        delegation_coordinator = DelegationCoordinator(
            store=store,
            config=runtime_config,
            backends=[ClaudeCodeBackendAdapter()],
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

        usage_service = UsageStatsService(
            session_store=store.sessions,
            archive_dir=data_root / "runtime" / "usage_archive",
            default_model_id=runtime_config.model.default_model_id,
        )
        handler = _build_orchestrator_handler(
            adapter,
            orchestrator,
            delegation_coordinator,
            memory_confirmations,
            usage_service=usage_service,
        )

        async def _dispatch(event: NormalizedEvent) -> ChannelResponse | None:
            return await handler(event)

        polling_task = asyncio.create_task(
            run_polling(
                adapter,
                runtime_config.telegram,
                _dispatch,
                stop_event=polling_stop,
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
