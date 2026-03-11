"""
Component ID: CMP_API_FASTAPI_GATEWAY

FastAPI application entry point: bootstraps config and mounts routers.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import cast

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
from assistant.channels.telegram.models import ChannelResponse, MessageType, NormalizedEvent
from assistant.channels.telegram.polling import run_polling
from assistant.core.bootstrap import bootstrap

logger = structlog.get_logger(__name__)

# Load .env before the lifespan starts so env overrides and ASSISTANT_ADMIN_TOKEN
# are available to bootstrap() and the auth layer.
load_dotenv()


async def _default_telegram_event_handler(event: NormalizedEvent) -> ChannelResponse:
    """
    Temporary bridge until orchestrator wiring is available.

    Ensures polling events reach a concrete handler and produce a response object.
    """
    return ChannelResponse(
        response_id=event.event_id,
        channel="telegram",
        session_id=event.session_id,
        trace_id=event.trace_id,
        message_type=MessageType.TEXT,
        text=event.text or "Received.",
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    runtime_config = bootstrap()
    set_runtime_config(runtime_config)
    app.state.runtime_config = runtime_config
    app.state.telegram_adapter = None
    app.state.telegram_event_handler = _default_telegram_event_handler

    polling_task: asyncio.Task[None] | None = None
    polling_stop = asyncio.Event()

    if runtime_config.telegram.enabled:
        transcription_service = build_transcription_service(runtime_config.telegram)
        adapter = TelegramAdapter(
            runtime_config.telegram, transcription_service=transcription_service
        )
        app.state.telegram_adapter = adapter

        async def _dispatch(event: NormalizedEvent) -> ChannelResponse | None:
            handler = app.state.telegram_event_handler
            result = await handler(event)
            return cast(ChannelResponse | None, result)

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
