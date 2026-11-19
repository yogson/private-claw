"""
Component ID: CMP_API_AGENT_NODE_INGRESS

Wire and persistence models of the agent-node compatible HTTP ingress.

Field names, status strings and error kinds mirror the agent-node server so a
caller (the automation system) cannot tell the two apart.
"""

import ipaddress
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    field_validator,
)

AGENT_NODE_SOURCE = "agent_node_http"
AGENT_NODE_SESSION_PREFIX = "agent-node:"

MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 14400
DEFAULT_TIMEOUT_SECONDS = 1800
MAX_DEADLINE_AHEAD_SECONDS = 7 * 24 * 3600

JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Body fields whose value must never be echoed back (e.g. in a 422).
SECRET_BODY_FIELDS = frozenset({"webhook_secret", "github_token"})

_RFC3339_DATE_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})"
)
_REPO_PATTERN = r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"
_GITHUB_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]{8,1024}")


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NO_CHANGES = "no_changes"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.NO_CHANGES, JobStatus.FAILED, JobStatus.CANCELLED}
)


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"
    WORKSPACE_FAILED = "workspace_failed"
    INVALID_INPUT = "invalid_input"
    CONFIG = "config"
    AGENT_FAILED = "agent_failed"
    INTERNAL = "internal"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class InputFilePayload(BaseModel):
    """A ``files`` entry. Accepted by the schema, refused by the route (unsupported)."""

    path: str = Field(..., min_length=1)
    content_base64: str = Field(..., min_length=1)


class CreateJobBody(BaseModel):
    """Body of ``POST /jobs``. Unknown fields are ignored, as agent-node does."""

    user_prompt: str = Field(..., min_length=1)
    repo_full_name: str | None = Field(default=None, pattern=_REPO_PATTERN)
    base_branch: str = "main"
    system_prompt: str | None = Field(default=None, min_length=1)
    timeout_seconds: StrictInt | None = Field(
        default=None, ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS
    )
    deadline: AwareDatetime | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    files: list[InputFilePayload] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    webhook_url: str | None = Field(default=None, min_length=1)
    webhook_secret: str | None = Field(default=None, min_length=1)
    # Never part of model_dump(): not stored, not logged, not in the fingerprint.
    github_token: SecretStr | None = Field(default=None, exclude=True)

    @field_validator("repo_full_name")
    @classmethod
    def _reject_traversal(cls, value: str | None) -> str | None:
        if value is None:
            return value
        owner, _, repo = value.partition("/")
        if owner in {".", ".."} or repo in {".", ".."}:
            raise ValueError("repo_full_name must not contain '.' or '..' as a component")
        return value

    @field_validator("webhook_url")
    @classmethod
    def _webhook_url_is_https(cls, value: str | None) -> str | None:
        if value is None:
            return value
        url = urlsplit(value)
        if url.hostname and (
            url.scheme == "https" or (url.scheme == "http" and _is_loopback(url.hostname))
        ):
            return value
        raise ValueError(
            "webhook_url must be an https:// URL (http:// only for localhost, 127.0.0.0/8 or ::1)"
        )

    @field_validator("github_token")
    @classmethod
    def _github_token_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not _GITHUB_TOKEN_RE.fullmatch(value.get_secret_value()):
            raise ValueError(
                "github_token must be 8 to 1024 characters, each a letter, a digit, '_', '.' or '-'"
            )
        return value

    @field_validator("deadline", mode="before")
    @classmethod
    def _deadline_is_rfc3339(cls, value: object) -> object:
        if value is not None and not (
            isinstance(value, str) and _RFC3339_DATE_TIME.fullmatch(value)
        ):
            raise ValueError("deadline must be an RFC 3339 date-time string with an offset")
        return value

    @field_validator("deadline")
    @classmethod
    def _deadline_to_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        try:
            value = value.astimezone(UTC)
        except OverflowError:
            raise ValueError("deadline is out of range") from None
        if (value - datetime.now(UTC)).total_seconds() > MAX_DEADLINE_AHEAD_SECONDS:
            raise ValueError(
                f"deadline must be at most {MAX_DEADLINE_AHEAD_SECONDS // 86400} days ahead"
            )
        return value


class JobResultView(BaseModel):
    branch: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    diff_stat: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    output: str | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None


class DeliveryView(BaseModel):
    """Read-only state of the completion webhook. Holds no URL, secret or body."""

    state: Literal["pending", "delivered", "failed_permanent", "abandoned"]
    attempts: int
    last_status: int | str | None = None
    next_attempt_at: str | None = None
    delivered_at: str | None = None


class JobView(BaseModel):
    id: str
    status: JobStatus
    created_at: str
    updated_at: str
    deadline: str | None = None
    idempotency_key: str | None = None
    result: JobResultView
    repo_full_name: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery: DeliveryView | None = None


class IdempotencyRecord(BaseModel):
    """Idempotency key -> job; the fingerprint decides replay vs conflict."""

    key: str
    job_id: str
    fingerprint: str
    created_at: str
    ttl_seconds: int


class RepoContext(BaseModel):
    """Where a repo job runs and what HEAD looked like when it was accepted."""

    full_name: str
    path: str
    base_branch: str
    head_before: str | None = None


class JobRecord(BaseModel):
    """Persistent agent-node job. The webhook secret is NOT part of it.

    ``final_view`` is set exactly once, when the job is finalized; from then on it
    is the authoritative (and webhook-delivered) representation.
    """

    model_config = ConfigDict(extra="ignore")

    job_id: str
    idempotency_key: str | None = None
    created_at: str
    deadline: str
    requested_deadline: str | None = None
    repo_full_name: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    repo: RepoContext | None = None
    webhook_url: str | None = None
    webhook_signed: bool = False
    timed_out_phase: str | None = None
    final_view: dict[str, Any] | None = None


class OutboxEntry(BaseModel):
    """A pending/finished ``job.completed`` delivery. Persisted until pruned."""

    job_id: str
    url: str
    signed: bool
    body: str  # the exact compact sorted-keys JSON that is POSTed
    created_at: str
    give_up_at: str
    state: Literal["pending", "delivered", "failed_permanent", "abandoned"] = "pending"
    attempts: int = 0
    last_status: int | str | None = None
    next_attempt_at: str
    delivered_at: str | None = None
