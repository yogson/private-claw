"""
Component ID: CMP_API_FASTAPI_GATEWAY

FastAPI application entry point: bootstraps config and mounts routers.
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse

from assistant.admin.router import router as admin_router
from assistant.api.lifespan import _lifespan
from assistant.api.routers import agent_node as agent_node_router
from assistant.api.routers import config as config_router
from assistant.api.routers import health
from assistant.api.routers import tasks as tasks_router

# Load .env before the lifespan starts so env overrides and ASSISTANT_ADMIN_TOKEN
# are available to bootstrap() and the auth layer.
load_dotenv()
# pydantic-ai v2 prints a startup banner when observability is off; keep logs clean.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

app = FastAPI(
    title="Private Claw 🦞 v1",
    version="1.0.0",
    lifespan=_lifespan,
)

app.include_router(health.router)
app.include_router(config_router.router)
app.include_router(tasks_router.router)
app.include_router(admin_router)
app.include_router(agent_node_router.router)
# Keeps FastAPI's 422 shape but never echoes webhook_secret / github_token back.
app.add_exception_handler(
    RequestValidationError,
    agent_node_router.request_validation_exception_handler,  # type: ignore[arg-type]
)


@app.get("/admin", include_in_schema=False)
async def admin_root() -> RedirectResponse:
    """Redirects bare /admin to the config dashboard."""
    return RedirectResponse("/admin/config", status_code=302)
