"""
Component ID: CMP_API_FASTAPI_GATEWAY

FastAPI application entry point: bootstraps config and mounts routers.
"""

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from assistant.admin.router import router as admin_router
from assistant.api.lifespan import _lifespan
from assistant.api.routers import config as config_router
from assistant.api.routers import health
from assistant.api.routers import tasks as tasks_router


# Load .env before the lifespan starts so env overrides and ASSISTANT_ADMIN_TOKEN
# are available to bootstrap() and the auth layer.
load_dotenv()

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
