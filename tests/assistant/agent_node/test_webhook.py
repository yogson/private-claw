"""Completion webhook: wire format, signature, retry classification, outbox recovery."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from assistant.agent_node.service import AgentNodeService
from assistant.agent_node.webhook import (
    WebhookSender,
    backoff_seconds,
    is_retryable,
    parse_retry_after,
    verify_signature,
)
from assistant.store.models import TaskStatus
from tests.assistant.agent_node.conftest import (
    AUTH,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
    Env,
)


def automation_verify(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verbatim logic of automation's resume_webhooks._verify_hmac_signature."""
    if not signature_header:
        return False
    received = signature_header[7:] if signature_header.startswith("sha256=") else signature_header
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("utf-8"), received.encode("utf-8"))


async def _job_with_webhook(env: Env, **extra: object) -> str:
    resp = await env.client.post(
        "/jobs",
        json={
            "user_prompt": "do it",
            "webhook_url": WEBHOOK_URL,
            "webhook_secret": WEBHOOK_SECRET,
            "metadata": {"execution": "e1", "node": "n1"},
            **extra,
        },
        headers={**AUTH, "Idempotency-Key": "e1:n1"},
    )
    assert resp.status_code == 202
    return str(resp.json()["id"])


async def _make_due(env: Env, job_id: str) -> None:
    entry = await env.store.agent_node.get_outbox(job_id)
    assert entry is not None
    entry.next_attempt_at = datetime.now(UTC).isoformat()
    await env.store.agent_node.save_outbox(entry)


class TestWireFormat:
    async def test_single_signed_post_with_the_three_headers(self, env: Env) -> None:
        job_id = await _job_with_webhook(env)
        await env.complete(job_id, "result text")
        await env.sender.deliver_due()
        await env.sender.deliver_due()  # delivered: nothing more is sent
        assert len(env.deliveries) == 1
        request = env.deliveries[0]
        assert request.method == "POST"
        assert str(request.url) == WEBHOOK_URL
        assert request.headers["content-type"] == "application/json"
        assert request.headers["x-agent-node-event"] == "job.completed"
        assert request.headers["x-agent-node-delivery"] == job_id
        signature = request.headers["x-agent-node-signature"]
        assert signature.startswith("sha256=")
        assert automation_verify(request.content, signature, WEBHOOK_SECRET)
        assert verify_signature(body=request.content, secret=WEBHOOK_SECRET, header_value=signature)
        assert not automation_verify(request.content, signature, "other-secret")
        assert not automation_verify(request.content + b" ", signature, WEBHOOK_SECRET)

    async def test_body_is_compact_sorted_json_of_the_view_without_delivery(self, env: Env) -> None:
        job_id = await _job_with_webhook(env)
        await env.complete(job_id, "result text")
        await env.sender.deliver_due()
        raw = env.deliveries[0].content
        payload = json.loads(raw)
        assert raw == json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        assert "delivery" not in payload
        assert payload["id"] == job_id
        assert payload["status"] == "succeeded"
        assert payload["idempotency_key"] == "e1:n1"
        assert payload["metadata"] == {"execution": "e1", "node": "n1"}
        assert payload["result"]["output"] == "result text"
        assert set(payload["result"]) >= {"branch", "commit_sha", "pr_url", "error", "error_kind"}
        # Same view the API serves, minus `delivery`.
        served = (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()
        served.pop("delivery")
        assert served == payload

    async def test_failed_job_webhook_carries_error_and_kind(self, env: Env) -> None:
        job_id = await _job_with_webhook(env)
        task = await env.store.tasks.update_status(job_id, TaskStatus.FAILED, error="boom")
        assert task is not None
        await env.service.on_task_completed(task)
        await env.sender.deliver_due()
        payload = json.loads(env.deliveries[0].content)
        assert payload["status"] == "failed"
        assert payload["result"]["error"] == "boom"
        assert payload["result"]["error_kind"] == "agent_failed"

    async def test_no_webhook_url_means_no_delivery(self, env: Env) -> None:
        resp = await env.client.post("/jobs", json={"user_prompt": "x"}, headers=AUTH)
        job_id = resp.json()["id"]
        await env.complete(job_id)
        await env.sender.deliver_due()
        assert env.deliveries == []
        assert await env.store.agent_node.get_outbox(job_id) is None

    async def test_url_without_secret_is_sent_unsigned(self, env: Env) -> None:
        resp = await env.client.post(
            "/jobs", json={"user_prompt": "x", "webhook_url": WEBHOOK_URL}, headers=AUTH
        )
        await env.complete(resp.json()["id"])
        await env.sender.deliver_due()
        assert "x-agent-node-signature" not in env.deliveries[0].headers

    async def test_secret_is_deleted_after_delivery_and_never_in_records(
        self, env: Env, tmp_path: Path
    ) -> None:
        job_id = await _job_with_webhook(env)
        assert await env.store.agent_node.get_secret(job_id) == WEBHOOK_SECRET
        await env.complete(job_id)
        assert await env.store.agent_node.get_secret(job_id) == WEBHOOK_SECRET  # still needed
        await env.sender.deliver_due()
        assert await env.store.agent_node.get_secret(job_id) is None
        for path in (tmp_path / "data").rglob("*"):
            if path.is_file():
                assert WEBHOOK_SECRET not in path.read_text(errors="ignore"), path


class TestRetryClassification:
    @pytest.mark.parametrize("status", [500, 502, 503, 504, 408, 409, 425, 429])
    def test_retryable_statuses(self, status: int) -> None:
        assert is_retryable(status)

    @pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 410, 422])
    def test_permanent_statuses(self, status: int) -> None:
        assert not is_retryable(status)

    @pytest.mark.parametrize(
        "error", ["ConnectError", "ReadTimeout", "TimeoutError", "RemoteProtocolError"]
    )
    def test_transport_errors_are_retried(self, error: str) -> None:
        assert is_retryable(error)

    @pytest.mark.parametrize("error", ["InvalidURL", "UnsupportedProtocol", "SecretUnavailable"])
    def test_unusable_target_is_permanent(self, error: str) -> None:
        assert not is_retryable(error)

    def test_backoff_doubles_and_caps(self) -> None:
        assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [1, 2, 4, 8]
        assert backoff_seconds(50) == 300

    def test_retry_after_forms(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert parse_retry_after("7", now=now, cap=300) == 7
        assert parse_retry_after("9999", now=now, cap=300) == 300
        assert parse_retry_after("-1", now=now, cap=300) is None
        assert parse_retry_after("soon", now=now, cap=300) is None
        assert parse_retry_after("Thu, 01 Jan 2026 00:00:30 GMT", now=now, cap=300) == 30

    async def test_retry_until_success(self, env: Env) -> None:
        env.webhook_status[:] = [503, 429, 200]
        job_id = await _job_with_webhook(env)
        await env.complete(job_id)
        for _ in range(3):
            await env.sender.attempt(job_id)
            await _make_due(env, job_id)
        assert len(env.deliveries) == 3
        entry = await env.store.agent_node.get_outbox(job_id)
        assert entry is not None
        assert (entry.state, entry.attempts, entry.last_status) == ("delivered", 3, 200)
        # Every attempt carries the identical signed body.
        assert len({r.content for r in env.deliveries}) == 1
        for r in env.deliveries:
            assert automation_verify(r.content, r.headers["x-agent-node-signature"], WEBHOOK_SECRET)

    async def test_retry_is_scheduled_with_backoff(self, env: Env) -> None:
        env.webhook_status[:] = [500]
        job_id = await _job_with_webhook(env)
        await env.complete(job_id)
        await env.sender.attempt(job_id)
        entry = await env.store.agent_node.get_outbox(job_id)
        assert entry is not None and entry.state == "pending"
        wait = (datetime.fromisoformat(entry.next_attempt_at) - datetime.now(UTC)).total_seconds()
        assert 0 < wait <= 1.5
        # Not due yet: another call does nothing.
        await env.sender.attempt(job_id)
        assert len(env.deliveries) == 1
        # The secret is kept while the delivery is still pending.
        assert await env.store.agent_node.get_secret(job_id) == WEBHOOK_SECRET

    @pytest.mark.parametrize("status", [401, 404, 410, 422, 400])
    async def test_permanent_failure_stops_and_drops_the_secret(
        self, env: Env, status: int
    ) -> None:
        env.webhook_status[:] = [status]
        job_id = await _job_with_webhook(env)
        await env.complete(job_id)
        await env.sender.attempt(job_id)
        await _make_due(env, job_id)
        await env.sender.attempt(job_id)
        assert len(env.deliveries) == 1
        entry = await env.store.agent_node.get_outbox(job_id)
        assert entry is not None and entry.state == "failed_permanent"
        assert entry.last_status == status
        assert await env.store.agent_node.get_secret(job_id) is None

    async def test_redirect_is_not_followed_and_is_permanent(self, env: Env) -> None:
        env.webhook_status[:] = [302]
        job_id = await _job_with_webhook(env)
        await env.complete(job_id)
        await env.sender.attempt(job_id)
        assert len(env.deliveries) == 1
        entry = await env.store.agent_node.get_outbox(job_id)
        assert entry is not None and entry.state == "failed_permanent"

    async def test_transport_error_is_retried(self, env: Env, tmp_path: Path) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        sender = WebhookSender(env.store.agent_node, transport=httpx.MockTransport(boom))
        job_id = await _job_with_webhook(env)
        await env.complete(job_id)
        await sender.attempt(job_id)
        entry = await env.store.agent_node.get_outbox(job_id)
        assert entry is not None
        assert (entry.state, entry.last_status) == ("pending", "ConnectError")

    async def test_gives_up_after_deadline_plus_margin(self, env: Env) -> None:
        job_id = await _job_with_webhook(env, timeout_seconds=600)
        await env.complete(job_id)
        entry = await env.store.agent_node.get_outbox(job_id)
        assert entry is not None
        job = await env.store.agent_node.get_job(job_id)
        assert job is not None
        assert datetime.fromisoformat(entry.give_up_at) == datetime.fromisoformat(
            job.deadline
        ) + timedelta(seconds=149)
        entry.give_up_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        entry.next_attempt_at = datetime.now(UTC).isoformat()
        await env.store.agent_node.save_outbox(entry)
        await env.sender.attempt(job_id)
        assert env.deliveries == []
        after = await env.store.agent_node.get_outbox(job_id)
        assert after is not None and after.state == "abandoned"
        assert await env.store.agent_node.get_secret(job_id) is None

    async def test_retry_beyond_the_window_is_abandoned(self, env: Env) -> None:
        env.webhook_status[:] = [503]
        job_id = await _job_with_webhook(env)
        await env.complete(job_id)
        entry = await env.store.agent_node.get_outbox(job_id)
        assert entry is not None
        entry.give_up_at = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
        await env.store.agent_node.save_outbox(entry)
        entry.attempts = 20  # backoff is capped at 300 s: the next try would be past the window
        await env.store.agent_node.save_outbox(entry)
        await env.sender.attempt(job_id)
        after = await env.store.agent_node.get_outbox(job_id)
        assert after is not None and after.state == "abandoned"

    async def test_missing_secret_is_permanent_and_never_sent_unsigned(self, env: Env) -> None:
        job_id = await _job_with_webhook(env)
        await env.complete(job_id)
        await env.store.agent_node.delete_secret(job_id)
        await env.sender.attempt(job_id)
        assert env.deliveries == []
        entry = await env.store.agent_node.get_outbox(job_id)
        assert entry is not None
        assert (entry.state, entry.last_status) == ("failed_permanent", "SecretUnavailable")


class TestOutboxRecovery:
    def _restarted(
        self, env: Env, transport: httpx.MockTransport
    ) -> tuple[AgentNodeService, WebhookSender]:
        """A fresh service + sender over the same on-disk store, as after a process restart."""
        sender = WebhookSender(env.store.agent_node, transport=transport)
        service = AgentNodeService(
            config=env.service.config,
            runtime_config=env.service._runtime_config,  # noqa: SLF001
            store=env.store.agent_node,
            task_store=env.store.tasks,
            coordinator=env.coordinator,
            sender=sender,
            data_root=env.service._data_root,  # noqa: SLF001
        )
        return service, sender

    async def test_pending_delivery_survives_a_restart(self, env: Env) -> None:
        env.webhook_status[:] = [503]
        job_id = await _job_with_webhook(env)
        await env.complete(job_id)
        await env.sender.attempt(job_id)  # first attempt failed; entry stays pending on disk
        assert len(env.deliveries) == 1

        seen: list[httpx.Request] = []

        def ok(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(204)

        _, sender = self._restarted(env, httpx.MockTransport(ok))
        await _make_due(env, job_id)
        await sender.deliver_due()
        assert len(seen) == 1
        assert automation_verify(
            seen[0].content, seen[0].headers["x-agent-node-signature"], WEBHOOK_SECRET
        )
        entry = await env.store.agent_node.get_outbox(job_id)
        assert entry is not None and entry.state == "delivered"

    async def test_attempt_lease_expires_so_a_crash_mid_attempt_is_retried(self, env: Env) -> None:
        job_id = await _job_with_webhook(env)
        await env.complete(job_id)
        entry = await env.store.agent_node.get_outbox(job_id)
        assert entry is not None
        # Simulate a claim written right before the process died.
        entry.attempts = 1
        entry.next_attempt_at = (datetime.now(UTC) + timedelta(seconds=15)).isoformat()
        await env.store.agent_node.save_outbox(entry)
        await env.sender.deliver_due()
        assert env.deliveries == []  # lease still running
        await _make_due(env, job_id)  # lease over
        await env.sender.deliver_due()
        assert len(env.deliveries) == 1

    async def test_job_finished_during_downtime_is_finalized_and_delivered_after_restart(
        self, env: Env
    ) -> None:
        job_id = await _job_with_webhook(env)
        # The process dies before the completion callback ever runs.
        await env.store.tasks.update_status(
            job_id, TaskStatus.COMPLETED, result={"summary": "while down", "usage": {}}
        )
        seen: list[httpx.Request] = []

        def ok(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        service, sender = self._restarted(env, httpx.MockTransport(ok))
        await service.reconcile()
        await sender.deliver_due()
        assert len(seen) == 1
        assert json.loads(seen[0].content)["result"]["output"] == "while down"
        # A second reconcile pass neither re-finalizes nor re-sends.
        await service.reconcile()
        await sender.deliver_due()
        assert len(seen) == 1

    async def test_finalize_is_idempotent_across_callers(self, env: Env) -> None:
        job_id = await _job_with_webhook(env)
        task = await env.store.tasks.update_status(
            job_id, TaskStatus.COMPLETED, result={"summary": "x", "usage": {}}
        )
        assert task is not None
        await env.service.on_task_completed(task)
        await env.service.on_task_completed(task)
        await env.service.reconcile()
        await env.sender.deliver_due()
        await env.sender.deliver_due()
        assert len(env.deliveries) == 1
