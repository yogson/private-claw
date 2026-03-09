"""
Component ID: CMP_API_FASTAPI_GATEWAY

FastAPI application entry point: bootstraps config and mounts routers.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from assistant.admin.router import router as admin_router
from assistant.api.deps import set_runtime_config
from assistant.api.routers import config as config_router
from assistant.api.routers import health
from assistant.core.bootstrap import bootstrap

# Load .env before the lifespan starts so env overrides and ASSISTANT_ADMIN_TOKEN
# are available to bootstrap() and the auth layer.
load_dotenv()


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
app.include_router(admin_router)


@app.get("/admin", include_in_schema=False)
async def admin_root() -> RedirectResponse:
    """Redirects bare /admin to the config dashboard."""
    return RedirectResponse("/admin/config", status_code=302)
