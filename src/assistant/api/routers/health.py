"""
Component ID: CMP_API_FASTAPI_GATEWAY

Health endpoints: liveness and readiness checks.
"""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get("/live", response_model=HealthResponse, summary="Liveness check")
async def liveness() -> HealthResponse:
    """Returns 200 when the service process is alive."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse, summary="Readiness check")
async def readiness() -> HealthResponse:
    """Returns 200 when the service is ready to handle requests."""
    return HealthResponse(status="ok")
