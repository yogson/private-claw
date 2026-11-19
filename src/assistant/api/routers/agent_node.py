"""
Component ID: CMP_API_AGENT_NODE_INGRESS

agent-node compatible endpoints: ``POST /jobs``, ``POST /jobs/{id}/cancel``,
``GET /jobs/{id}``. Bearer-token protected; indistinguishable from an agent-node
for the automation caller (field names, status codes and ``detail`` strings).
"""

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from assistant.agent_node.idempotency import InvalidIdempotencyKey, validate_idempotency_key
from assistant.agent_node.models import (
    JOB_ID_PATTERN,
    SECRET_BODY_FIELDS,
    CreateJobBody,
    JobView,
)
from assistant.agent_node.service import (
    FILES_UNSUPPORTED_DETAIL,
    AgentNodeService,
    DeadlineNotInFuture,
    IdempotencyConflict,
    JobRejected,
)

logger = structlog.get_logger(__name__)

UNAUTHORIZED_DETAIL = "invalid or missing bearer token"
JOB_NOT_FOUND_DETAIL = "job not found"
IDEMPOTENT_REPLAYED_HEADER = "Idempotent-Replayed"
REDACTED = "[REDACTED]"

_bearer = HTTPBearer(auto_error=False)

router = APIRouter(prefix="/jobs", tags=["agent-node"])


def _service(request: Request) -> AgentNodeService:
    service: AgentNodeService | None = getattr(request.app.state, "agent_node", None)
    if service is None:
        # Ingress disabled: behave as if the routes do not exist.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return service


def require_bearer(
    request: Request,
    service: Annotated[AgentNodeService, Depends(_service)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)] = None,
) -> None:
    """Bearer token only. Judged on the raw header so non-ASCII tricks cannot pass."""
    raw_header = request.headers.get("Authorization", "")
    expected = service.config.auth_token
    # The service only exists with a token that passed auth_token_problem() (ASCII).
    if credentials is not None and raw_header.isascii() and expected and expected.isascii():
        token = raw_header.partition(" ")[2].strip(" \t")
        if secrets.compare_digest(token.encode("ascii"), expected.encode("ascii")):
            return
    logger.warning(
        "agent_node.auth_rejected",
        path=request.url.path,
        method=request.method,
        client=request.client.host if request.client else None,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


Auth = Depends(require_bearer)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if k in SECRET_BODY_FIELDS and v is not None else _scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def redact_validation_errors(errors: Any) -> list[dict[str, Any]]:
    """``RequestValidationError.errors()`` with every secret value redacted."""
    redacted = []
    for error in errors:
        error = dict(error)
        if "input" in error:
            about_secret = any(
                part in SECRET_BODY_FIELDS
                for part in error.get("loc") or ()
                if isinstance(part, str)
            )
            error["input"] = REDACTED if about_secret else _scrub(error["input"])
        redacted.append(error)
    return redacted


async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """FastAPI's default 422 shape with secret fields redacted (app-wide)."""
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(redact_validation_errors(exc.errors()))},
    )


def _deadline_not_in_future(deadline_input: Any) -> RequestValidationError:
    message = "deadline must be in the future"
    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "deadline"),
                "msg": f"Value error, {message}",
                "input": deadline_input,
                "ctx": {"error": ValueError(message)},
            }
        ]
    )


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=JobView, dependencies=[Auth])
async def create_job(
    body: CreateJobBody,
    request: Request,
    response: Response,
    service: Annotated[AgentNodeService, Depends(_service)],
) -> JobView | JSONResponse:
    """Accept a job and run it in the background; the result arrives by webhook."""
    try:
        idempotency_key = validate_idempotency_key(request.headers.getlist("idempotency-key"))
    except InvalidIdempotencyKey as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    raw: Any = None
    deadline_passed = body.deadline is not None and body.deadline <= datetime.now(UTC)
    if deadline_passed:
        with_raw = await request.json()
        raw = with_raw.get("deadline") if isinstance(with_raw, dict) else None
    if body.files:
        raise HTTPException(status_code=422, detail=FILES_UNSUPPORTED_DETAIL)
    try:
        outcome = await service.create_job(
            body_dump=body.model_dump(mode="json"),
            user_prompt=body.user_prompt,
            system_prompt=body.system_prompt,
            repo_full_name=body.repo_full_name,
            base_branch=body.base_branch,
            timeout_seconds=body.timeout_seconds,
            requested_deadline=body.deadline,
            model=body.model,
            metadata=body.metadata,
            webhook_url=body.webhook_url,
            webhook_secret=body.webhook_secret,
            idempotency_key=idempotency_key,
        )
    except JobRejected as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers=headers
        ) from None
    except IdempotencyConflict as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": exc.detail, "reason": exc.reason},
        )
    except DeadlineNotInFuture:
        raise _deadline_not_in_future(raw) from None
    if outcome.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers[IDEMPOTENT_REPLAYED_HEADER] = "true"
    return outcome.view


def _checked_job_id(job_id: str) -> str:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise HTTPException(status_code=404, detail=JOB_NOT_FOUND_DETAIL)
    return job_id


@router.get("/{job_id}", response_model=JobView, dependencies=[Auth])
async def get_job(job_id: str, service: Annotated[AgentNodeService, Depends(_service)]) -> JobView:
    view = await service.get_view(_checked_job_id(job_id))
    if view is None:
        raise HTTPException(status_code=404, detail=JOB_NOT_FOUND_DETAIL)
    return view


@router.post("/{job_id}/cancel", response_model=JobView, dependencies=[Auth])
async def cancel_job(
    job_id: str, service: Annotated[AgentNodeService, Depends(_service)]
) -> JobView:
    """Cancel an unfinished job. Idempotent; a finished job is returned unchanged."""
    view = await service.cancel(_checked_job_id(job_id))
    if view is None:
        raise HTTPException(status_code=404, detail=JOB_NOT_FOUND_DETAIL)
    return view
