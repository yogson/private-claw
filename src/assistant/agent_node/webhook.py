"""
Component ID: CMP_API_AGENT_NODE_INGRESS

Completion webhook sender with a persistent outbox, wire-compatible with agent-node.

Exactly one terminal ``POST <webhook_url>`` per job: compact sorted-keys JSON, the
``X-Agent-Node-Event`` / ``-Delivery`` / ``-Signature`` headers, HMAC-SHA256 hex over
the raw bytes. 5xx / 408 / 409 / 425 / 429 / transport errors are retried with
exponential backoff (``Retry-After`` honoured on 429/503); any other status (3xx
included, redirects are never followed) ends the delivery permanently. The delivery
is abandoned ``deadline + margin`` after the job's effective deadline.

The outbox lives in the store, so pending deliveries survive a restart. The webhook
secret is deleted once the delivery is final.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog

from assistant.agent_node.interfaces import AgentNodeStoreInterface
from assistant.agent_node.models import OutboxEntry

logger = structlog.get_logger(__name__)

EVENT_HEADER = "X-Agent-Node-Event"
SIGNATURE_HEADER = "X-Agent-Node-Signature"
DELIVERY_HEADER = "X-Agent-Node-Delivery"
EVENT_JOB_COMPLETED = "job.completed"

RETRYABLE_4XX = frozenset({408, 409, 425, 429})
RETRY_AFTER_STATUSES = frozenset({429, 503})
# Error class names that repeat on every attempt: permanent at once.
PERMANENT_ERRORS = frozenset({"InvalidURL", "UnsupportedProtocol", "SecretUnavailable"})
SECRET_UNAVAILABLE = "SecretUnavailable"
ATTEMPT_TIMEOUT = "TimeoutError"

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 300.0
MIN_ATTEMPT_SECONDS = 2.0
DEADLINE_SLACK_SECONDS = 1.0
CLAIM_LEASE_MARGIN_SECONDS = 5.0
_POLL_SECONDS = 1.0


def is_retryable(status: int | str) -> bool:
    """Whether an attempt with this outcome (HTTP status, or error class name) is retried."""
    if isinstance(status, str):
        return status not in PERMANENT_ERRORS
    return status >= 500 or status in RETRYABLE_4XX


def backoff_seconds(attempt: int) -> float:
    """Seconds from the end of attempt ``attempt`` to the next one: doubling, capped."""
    delay: float = BACKOFF_BASE_SECONDS * 2 ** min(attempt - 1, 32)
    return min(delay, BACKOFF_MAX_SECONDS)


def parse_retry_after(value: str | None, *, now: datetime, cap: float) -> float | None:
    """Seconds a ``Retry-After`` asks to wait, clamped to ``[0, cap]``; None if unreadable."""
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        digits = value.lstrip("0") or "0"
        seconds = float(int(digits)) if len(digits) <= 12 else cap
    else:
        try:
            when = parsedate_to_datetime(value)
            if when is None or when.tzinfo is None:
                return None
            seconds = (when - now).total_seconds()
        except Exception:
            return None
    return min(max(seconds, 0.0), cap)


def encode_body(payload: dict[str, Any]) -> str:
    """The exact bytes (as text) that are signed and POSTed."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(*, body: bytes, secret: str, header_value: str) -> bool:
    """Receiver-side check: constant-time compare of ``sha256=<hex>``."""
    if not header_value.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.removeprefix("sha256="))


def _now() -> datetime:
    return datetime.now(UTC)


class WebhookSender:
    """Delivers outbox entries; ``run()`` is the background loop."""

    def __init__(
        self,
        store: AgentNodeStoreInterface,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._store = store
        self._timeout = timeout_seconds
        self._transport = transport
        self._kick = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="agent_node.webhook_sender")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._kick.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def kick(self) -> None:
        """Wake the loop: a new entry is ready for its first attempt."""
        self._kick.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.deliver_due()
            except Exception:
                logger.exception("agent_node.webhook.loop_error")
            self._kick.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), timeout=_POLL_SECONDS)

    async def deliver_due(self) -> None:
        """Make one attempt for every pending entry whose ``next_attempt_at`` has come."""
        for entry in await self._store.list_pending_outbox():
            if datetime.fromisoformat(entry.next_attempt_at) <= _now():
                await self.attempt(entry.job_id)

    async def attempt(self, job_id: str) -> None:
        """One claimed attempt for ``job_id`` (no-op if not pending or not due)."""
        entry = await self._store.get_outbox(job_id)
        if entry is None or entry.state != "pending":
            return
        now = _now()
        if datetime.fromisoformat(entry.next_attempt_at) > now:
            return
        give_up_at = datetime.fromisoformat(entry.give_up_at)
        limit = (give_up_at - now).total_seconds() - DEADLINE_SLACK_SECONDS
        if limit < min(MIN_ATTEMPT_SECONDS, self._timeout):
            await self._finish(entry, "abandoned", entry.last_status)
            logger.warning("agent_node.webhook.gave_up", job_id=job_id, reason="deadline_window")
            return
        attempt_timeout = min(self._timeout, limit)
        # Claim: count the attempt and lease it, so a crash mid-attempt leaves a lease
        # that expires rather than an attempt that is never retried.
        entry.attempts += 1
        entry.next_attempt_at = (
            now + timedelta(seconds=attempt_timeout + CLAIM_LEASE_MARGIN_SECONDS)
        ).isoformat()
        await self._store.save_outbox(entry)

        secret: str | None = None
        if entry.signed:
            secret = await self._store.get_secret(job_id)
            if not secret:
                await self._finish(entry, "failed_permanent", SECRET_UNAVAILABLE)
                logger.warning("agent_node.webhook.secret_unavailable", job_id=job_id)
                return
        body = entry.body.encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            EVENT_HEADER: EVENT_JOB_COMPLETED,
            DELIVERY_HEADER: job_id,
        }
        if secret:
            headers[SIGNATURE_HEADER] = sign(secret, body)
        status, retry_after = await self._post(entry.url, body, headers, attempt_timeout)

        if isinstance(status, int) and 200 <= status < 300:
            await self._finish(entry, "delivered", status)
            logger.info(
                "agent_node.webhook.delivered",
                job_id=job_id,
                status_code=status,
                attempt=entry.attempts,
            )
            return
        if not is_retryable(status):
            await self._finish(entry, "failed_permanent", status)
            logger.warning("agent_node.webhook.permanent_failure", job_id=job_id, status=status)
            return
        delay = backoff_seconds(entry.attempts)
        if retry_after is not None:
            delay = max(delay, retry_after)
        next_at = _now() + timedelta(seconds=delay)
        if next_at >= give_up_at:
            await self._finish(entry, "abandoned", status)
            logger.warning("agent_node.webhook.gave_up", job_id=job_id, reason="deadline_window")
            return
        entry.last_status = status
        entry.next_attempt_at = next_at.isoformat()
        await self._store.save_outbox(entry)
        logger.info(
            "agent_node.webhook.retry",
            job_id=job_id,
            status=status,
            attempt=entry.attempts,
            delay_s=round(delay, 1),
        )

    async def _finish(self, entry: OutboxEntry, state: str, last_status: int | str | None) -> None:
        entry.state = state  # type: ignore[assignment]
        entry.last_status = last_status
        entry.next_attempt_at = _now().isoformat()
        if state == "delivered":
            entry.delivered_at = _now().isoformat()
        await self._store.save_outbox(entry)
        # The secret has no use once the delivery is final.
        await self._store.delete_secret(entry.job_id)

    async def _post(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int | str, float | None]:
        """One POST: (HTTP status or error class name, honoured Retry-After)."""
        try:
            async with (
                httpx.AsyncClient(
                    timeout=timeout, follow_redirects=False, transport=self._transport
                ) as client,
                asyncio.timeout(timeout),
            ):
                response = await client.post(url, content=body, headers=headers)
                retry_after = None
                if response.status_code in RETRY_AFTER_STATUSES:
                    retry_after = parse_retry_after(
                        response.headers.get("retry-after"),
                        now=_now(),
                        cap=BACKOFF_MAX_SECONDS,
                    )
                return response.status_code, retry_after
        except TimeoutError:
            return ATTEMPT_TIMEOUT, None
        except httpx.InvalidURL:
            return "InvalidURL", None
        except httpx.UnsupportedProtocol:
            return "UnsupportedProtocol", None
        except Exception as exc:
            return type(exc).__name__, None
