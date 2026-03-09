"""
Component ID: CMP_API_FASTAPI_GATEWAY

FastAPI application entry point: bootstraps config and mounts routers.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from assistant.api.deps import set_runtime_config
from assistant.api.routers import config as config_router
from assistant.api.routers import health
from assistant.core.bootstrap import bootstrap


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    runtime_config = bootstrap()
    set_runtime_config(runtime_config)
    yield


app = FastAPI(
    title="Personal AI Assistant v1",
    version="1.0.0",
    lifespan=_lifespan,
)

app.include_router(health.router)
app.include_router(config_router.router)
