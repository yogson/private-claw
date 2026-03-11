"""
Component ID: CMP_API_FASTAPI_GATEWAY

FastAPI application entry point: bootstraps config and mounts routers.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from assistant.admin.router import router as admin_router
from assistant.api.deps import set_runtime_config
from assistant.api.routers import config as config_router
from assistant.api.routers import health
from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.ingestion.factory import build_transcription_service
from assistant.channels.telegram.ingestion.file_downloader import TelegramFileDownloader
from assistant.channels.telegram.models import ChannelResponse, MessageType, NormalizedEvent
from assistant.channels.telegram.polling import run_polling
from assistant.core.bootstrap import bootstrap
from assistant.core.events.mapper import NormalizedEventMapper
from assistant.core.orchestrator import Orchestrator
from assistant.providers.anthropic import AnthropicAdapter
from assistant.store.facade import StoreFacade
from assistant.store.idempotency.service import IngressIdempotencyService

logger = structlog.get_logger(__name__)

# Load .env before the lifespan starts so env overrides and ASSISTANT_ADMIN_TOKEN
# are available to bootstrap() and the auth layer.
load_dotenv()


def _build_orchestrator_handler(
    adapter: TelegramAdapter, orchestrator: Orchestrator
) -> Callable[[NormalizedEvent], Awaitable[ChannelResponse | None]]:
    mapper = NormalizedEventMapper()

    async def _handler(event: NormalizedEvent) -> ChannelResponse | None:
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

        orch_event = mapper.map(event)
        response_text = await orchestrator.execute_turn(orch_event)
        if response_text is None:
            return None
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
    app.state.runtime_config = runtime_config
    app.state.telegram_adapter = None
    app.state.attachment_downloader = None

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

        idempotency = IngressIdempotencyService(
            store.idempotency,
            default_ttl_seconds=runtime_config.store.idempotency_retention_seconds,
        )
        provider = AnthropicAdapter(runtime_config.model)
        attachment_downloader = TelegramFileDownloader(
            bot_token=runtime_config.telegram.bot_token,
            max_size_bytes=runtime_config.telegram.max_attachment_size_bytes,
        )
        app.state.attachment_downloader = attachment_downloader
        orchestrator = Orchestrator(
            store=store,
            provider=provider,
            config=runtime_config,
            idempotency=idempotency,
            attachment_downloader=attachment_downloader,
        )

        transcription_service = build_transcription_service(runtime_config.telegram)
        adapter = TelegramAdapter(
            runtime_config.telegram,
            transcription_service=transcription_service,
            session_store=store.sessions,
        )
        app.state.telegram_adapter = adapter

        handler = _build_orchestrator_handler(adapter, orchestrator)

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
            await store.shutdown()


app = FastAPI(
    title="Private Claw 🦞 v1",
    version="1.0.0",
    lifespan=_lifespan,
)

app.include_router(health.router)
app.include_router(config_router.router)
app.include_router(admin_router)


@app.get("/admin", include_in_schema=False)
async def admin_root() -> RedirectResponse:
    """Redirects bare /admin to the config dashboard."""
    return RedirectResponse("/admin/config", status_code=302)
