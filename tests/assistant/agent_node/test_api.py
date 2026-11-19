"""Contract tests for POST /jobs, POST /jobs/{id}/cancel and GET /jobs/{id}."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from assistant.store.models import TaskStatus
from tests.assistant.agent_node.conftest import (
    AUTH,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
    Env,
    git,
)

RESULT_FIELDS = {
    "branch",
    "commit_sha",
    "pr_url",
    "pr_number",
    "diff_stat",
    "cost_usd",
    "num_turns",
    "duration_ms",
    "output",
    "error",
    "error_kind",
}
VIEW_FIELDS = {
    "id",
    "status",
    "created_at",
    "updated_at",
    "deadline",
    "idempotency_key",
    "result",
    "repo_full_name",
    "model",
    "metadata",
    "delivery",
}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"user_prompt": "Refactor the widget"}
    body.update(overrides)
    return body


def _key(value: str = "exec-1:node-1") -> dict[str, str]:
    return {**AUTH, "Idempotency-Key": value}


async def _create(env: Env, headers: dict[str, str] | None = None, **body: Any) -> Any:
    return await env.client.post("/jobs", json=_body(**body), headers=headers or _key())


class TestAuth:
    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": "Basic dGVzdA=="},
            {"Authorization": "Bearer "},
            {"x-api-key": "test-bearer-token"},
        ],
    )
    async def test_rejected_on_every_route(self, env: Env, headers: dict[str, str]) -> None:
        for method, path in (
            ("POST", "/jobs"),
            ("GET", "/jobs/abc"),
            ("POST", "/jobs/abc/cancel"),
        ):
            kwargs: dict[str, Any] = {"json": _body()} if path == "/jobs" else {}
            resp = await env.client.request(method, path, headers=headers, **kwargs)
            assert resp.status_code == 401
            assert resp.json() == {"detail": "invalid or missing bearer token"}
            assert resp.headers["www-authenticate"] == "Bearer"

    async def test_disabled_ingress_is_404(self, env: Env) -> None:
        env.client._transport.app.state.agent_node = None  # type: ignore[attr-defined]
        resp = await env.client.get("/jobs/abc", headers=AUTH)
        assert resp.status_code == 404


class TestCreate:
    async def test_new_job_is_202_with_queued_view(self, env: Env) -> None:
        before = datetime.now(UTC)
        resp = await _create(
            env, timeout_seconds=600, metadata={"node": "n1"}, model="claude-haiku-4-5"
        )
        assert resp.status_code == 202
        view = resp.json()
        assert set(view) == VIEW_FIELDS
        assert set(view["result"]) == RESULT_FIELDS
        assert all(v is None for v in view["result"].values())
        assert view["status"] == "queued"
        assert re.fullmatch(r"[0-9a-f]{32}", view["id"])
        assert view["idempotency_key"] == "exec-1:node-1"
        assert view["metadata"] == {"node": "n1"}
        assert view["model"] == "claude-haiku-4-5"
        assert view["delivery"] is None
        deadline = datetime.fromisoformat(view["deadline"])
        assert before + timedelta(seconds=599) <= deadline <= before + timedelta(seconds=602)

    async def test_effective_deadline_is_the_earlier_limit(self, env: Env) -> None:
        soon = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
        resp = await _create(env, timeout_seconds=3600, deadline=soon)
        assert resp.status_code == 202
        assert datetime.fromisoformat(resp.json()["deadline"]) == datetime.fromisoformat(soon)

    async def test_default_timeout_is_1800(self, env: Env) -> None:
        resp = await _create(env)
        deadline = datetime.fromisoformat(resp.json()["deadline"])
        created = datetime.fromisoformat(resp.json()["created_at"])
        assert deadline - created == timedelta(seconds=1800)

    async def test_task_is_enqueued_for_the_synthetic_session(self, env: Env) -> None:
        resp = await _create(env, system_prompt="Be terse.", model="claude-haiku-4-5")
        job_id = resp.json()["id"]
        task = await env.store.tasks.get(job_id)
        assert task is not None
        assert task.parent_session_id == f"agent-node:{job_id}"
        assert task.status == TaskStatus.PENDING
        md = task.metadata
        assert md["source"] == "agent_node_http"
        assert md["job_id"] == job_id
        assert md["idempotency_key"] == "exec-1:node-1"
        assert md["interactive"] is False
        assert md["model_id"] == "claude-haiku-4-5"
        assert md["objective"].startswith("Be terse.")
        assert "Refactor the widget" in md["objective"]
        assert md["timeout_seconds"] <= 1800
        # The webhook URL carries the suspension token: it stays out of the task record.
        assert WEBHOOK_URL not in task.model_dump_json()

    async def test_repo_job_runs_in_the_mapped_checkout(self, env: Env) -> None:
        resp = await _create(env, repo_full_name="acme/widgets", base_branch="develop")
        assert resp.status_code == 202
        assert resp.json()["repo_full_name"] == "acme/widgets"
        task = await env.store.tasks.get(resp.json()["id"])
        assert task is not None
        assert task.metadata["backend_params"]["directory"] == str(env.repo)
        objective = task.metadata["objective"]
        assert "acme/widgets" in objective
        assert str(env.repo) in objective
        assert "Base branch: develop" in objective
        assert "pull request" in objective  # explicitly told not to open one

    async def test_job_without_repo_gets_a_scratch_workspace(self, env: Env) -> None:
        resp = await _create(env)
        task = await env.store.tasks.get(resp.json()["id"])
        assert task is not None
        directory = task.metadata["backend_params"]["directory"]
        assert directory != str(env.repo)
        assert "workspaces" in directory

    async def test_unknown_fields_are_ignored(self, env: Env) -> None:
        resp = await _create(env, surprise="x")
        assert resp.status_code == 202

    async def test_without_idempotency_key_every_post_is_a_new_job(self, env: Env) -> None:
        a = await env.client.post("/jobs", json=_body(), headers=AUTH)
        b = await env.client.post("/jobs", json=_body(), headers=AUTH)
        assert a.status_code == b.status_code == 202
        assert a.json()["id"] != b.json()["id"]
        assert a.json()["idempotency_key"] is None

    async def test_github_token_is_accepted_but_never_stored_or_returned(self, env: Env) -> None:
        token = "ghp_abcdefghijklmnop123456"
        resp = await _create(env, github_token=token, webhook_url=WEBHOOK_URL)
        assert resp.status_code == 202
        assert token not in resp.text
        await env.complete(resp.json()["id"])
        for path in env.store._data_root.rglob("*"):  # type: ignore[attr-defined]
            if path.is_file():
                assert token not in path.read_text(errors="ignore"), path


class TestIdempotency:
    async def test_replay_is_200_with_header_and_no_new_run(self, env: Env) -> None:
        first = await _create(env, repo_full_name="acme/widgets")
        second = await _create(env, repo_full_name="acme/widgets")
        assert first.status_code == 202
        assert second.status_code == 200
        assert second.headers["idempotent-replayed"] == "true"
        assert "idempotent-replayed" not in first.headers
        assert second.json()["id"] == first.json()["id"]
        assert len(await env.store.tasks.list_by_status(TaskStatus.PENDING)) == 1

    async def test_replay_ignores_deadline_and_github_token(self, env: Env) -> None:
        d1 = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        d2 = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        first = await _create(env, deadline=d1, github_token="ghp_aaaaaaaaaaaa1")
        second = await _create(env, deadline=d2, github_token="ghp_bbbbbbbbbbbb2")
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]

    async def test_replay_returns_the_current_view(self, env: Env) -> None:
        first = await _create(env)
        job_id = first.json()["id"]
        await env.complete(job_id, "all done")
        replay = await _create(env)
        assert replay.status_code == 200
        assert replay.json()["status"] == "succeeded"
        assert replay.json()["result"]["output"] == "all done"

    async def test_same_key_different_body_is_409(self, env: Env) -> None:
        await _create(env)
        resp = await _create(env, user_prompt="Something else entirely")
        assert resp.status_code == 409
        body = resp.json()
        assert body["reason"] == "idempotency_key_reused_with_different_request"
        assert body["detail"] == "Idempotency-Key was already used with a different request"
        assert len(await env.store.tasks.list_by_status(TaskStatus.PENDING)) == 1

    async def test_earlier_deadline_on_unfinished_job_is_409(self, env: Env) -> None:
        await _create(env, timeout_seconds=3600)
        soon = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
        resp = await _create(env, timeout_seconds=3600, deadline=soon)
        assert resp.status_code == 409
        assert resp.json()["reason"] == "idempotency_deadline_earlier_than_job"

    @pytest.mark.parametrize(
        "key",
        ["x" * 256, " leading", "trailing ", "bad\x7fchar", ""],
    )
    async def test_malformed_key_is_400(self, env: Env, key: str) -> None:
        resp = await env.client.post(
            "/jobs", json=_body(), headers={**AUTH, "Idempotency-Key": key}
        )
        assert resp.status_code == 400

    async def test_repeated_header_is_400(self, env: Env) -> None:
        resp = await env.client.post(
            "/jobs",
            json=_body(),
            headers=[
                ("Authorization", AUTH["Authorization"]),
                ("Idempotency-Key", "a"),
                ("Idempotency-Key", "b"),
            ],
        )
        assert resp.status_code == 400
        assert "at most once" in resp.json()["detail"]

    async def test_max_length_key_is_accepted(self, env: Env) -> None:
        resp = await _create(env, headers=_key("k" * 255))
        assert resp.status_code == 202

    async def test_passed_deadline_is_422_unless_key_holds_a_finished_job(self, env: Env) -> None:
        past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        resp = await _create(env, deadline=past)
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"] == ["body", "deadline"]
        first = await _create(env, headers=_key("k2"))
        # Unfinished job: an earlier deadline is a tighter request, not a retry.
        assert (await _create(env, headers=_key("k2"), deadline=past)).status_code == 409
        await env.complete(first.json()["id"])
        replay = await _create(env, headers=_key("k2"), deadline=past)
        assert replay.status_code == 200
        assert replay.json()["id"] == first.json()["id"]


class TestRejections:
    async def test_files_are_422(self, env: Env) -> None:
        resp = await _create(env, files=[{"path": "a.txt", "content_base64": "YQ=="}])
        assert resp.status_code == 422
        assert resp.json() == {"detail": "input files are not supported"}
        assert await env.store.tasks.list_by_status(TaskStatus.PENDING) == []

    async def test_empty_files_list_is_fine(self, env: Env) -> None:
        assert (await _create(env, files=[])).status_code == 202

    async def test_unknown_repository_is_422(self, env: Env) -> None:
        resp = await _create(env, repo_full_name="someone/else")
        assert resp.status_code == 422
        assert "someone/else" in resp.json()["detail"]
        assert await env.store.tasks.list_by_status(TaskStatus.PENDING) == []

    async def test_concurrency_limit_is_503_with_retry_after(self, env: Env) -> None:
        assert (await _create(env, headers=_key("a"))).status_code == 202
        assert (await _create(env, headers=_key("b"))).status_code == 202
        resp = await _create(env, headers=_key("c"))
        assert resp.status_code == 503
        assert int(resp.headers["retry-after"]) > 0
        # Nothing was recorded for the refused request: a retry with the same key is a new job.
        await env.coordinator.cancel_task(
            (await env.store.tasks.list_by_status(TaskStatus.PENDING))[0].task_id
        )
        retry = await _create(env, headers=_key("c"))
        assert retry.status_code == 202

    async def test_model_outside_allowlist_is_422(self, env: Env) -> None:
        resp = await _create(env, model="gpt-9")
        assert resp.status_code == 422
        assert "not allowed" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "body",
        [
            {"user_prompt": ""},
            {},
            {"user_prompt": "x", "timeout_seconds": 5},
            {"user_prompt": "x", "timeout_seconds": 14401},
            {"user_prompt": "x", "timeout_seconds": 60.5},
            {"user_prompt": "x", "webhook_url": "http://example.com/hook"},
            {"user_prompt": "x", "deadline": "1700000000"},
            {"user_prompt": "x", "deadline": "2999-01-01T00:00:00Z"},
            {"user_prompt": "x", "repo_full_name": "../etc"},
            {"user_prompt": "x", "model": ""},
        ],
    )
    async def test_validation_errors_are_422(self, env: Env, body: dict[str, Any]) -> None:
        resp = await env.client.post("/jobs", json=body, headers=_key())
        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], list)

    async def test_loopback_http_webhook_is_allowed(self, env: Env) -> None:
        resp = await _create(env, webhook_url="http://127.0.0.1:9/hook")
        assert resp.status_code == 202

    async def test_validation_errors_never_echo_secrets(self, env: Env) -> None:
        resp = await env.client.post(
            "/jobs",
            json={
                "user_prompt": "",
                "webhook_secret": WEBHOOK_SECRET,
                "github_token": "bad token with spaces!",
            },
            headers=_key(),
        )
        assert resp.status_code == 422
        assert WEBHOOK_SECRET not in resp.text
        assert "bad token with spaces" not in resp.text


class TestGet:
    async def test_unknown_job_is_404(self, env: Env) -> None:
        resp = await env.client.get("/jobs/" + "0" * 32, headers=AUTH)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "job not found"}

    async def test_bad_id_shape_is_404(self, env: Env) -> None:
        assert (await env.client.get("/jobs/has.dot", headers=AUTH)).status_code == 404

    async def test_status_follows_the_task(self, env: Env) -> None:
        job_id = (await _create(env)).json()["id"]
        got = await env.client.get(f"/jobs/{job_id}", headers=AUTH)
        assert got.json()["status"] == "queued"
        await env.store.tasks.update_status(job_id, TaskStatus.RUNNING)
        assert (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()["status"] == "running"

    async def test_delivery_is_shown_once_a_webhook_exists(self, env: Env) -> None:
        job_id = (
            await _create(env, webhook_url=WEBHOOK_URL, webhook_secret=WEBHOOK_SECRET)
        ).json()["id"]
        assert (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()["delivery"] is None
        await env.complete(job_id)
        delivery = (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()["delivery"]
        assert delivery["state"] == "pending"
        assert delivery["attempts"] == 0
        assert WEBHOOK_URL not in str(delivery) and WEBHOOK_SECRET not in str(delivery)


class TestCompletionMapping:
    async def test_non_repo_success_carries_output_and_usage(self, env: Env) -> None:
        job_id = (await _create(env)).json()["id"]
        await env.complete(job_id, "the answer")
        view = (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()
        assert view["status"] == "succeeded"
        assert view["result"]["output"] == "the answer"
        assert view["result"]["cost_usd"] == 0.5
        assert view["result"]["num_turns"] == 3
        assert isinstance(view["result"]["duration_ms"], int)
        assert view["result"]["error"] is None and view["result"]["error_kind"] is None

    async def test_repo_job_without_new_commit_is_no_changes(self, env: Env) -> None:
        job_id = (await _create(env, repo_full_name="acme/widgets")).json()["id"]
        await env.complete(job_id)
        view = (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()
        assert view["status"] == "no_changes"
        assert view["result"]["commit_sha"] is None and view["result"]["branch"] is None

    async def test_repo_job_with_new_commit_reports_branch_and_sha(self, env: Env) -> None:
        job_id = (await _create(env, repo_full_name="acme/widgets")).json()["id"]
        git(env.repo, "checkout", "-q", "-b", f"agent-node/{job_id[:12]}")
        (env.repo / "b.txt").write_text("b\n")
        git(env.repo, "add", "-A")
        git(env.repo, "commit", "-q", "-m", "work")
        sha = git(env.repo, "rev-parse", "HEAD")
        await env.complete(job_id)
        view = (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()
        assert view["status"] == "succeeded"
        assert view["result"]["commit_sha"] == sha
        assert view["result"]["branch"] == f"agent-node/{job_id[:12]}"
        assert "b.txt" in view["result"]["diff_stat"]
        assert view["result"]["pr_url"] is None and view["result"]["pr_number"] is None

    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            ("claude run timed out", "timeout"),
            ("claude-agent-sdk run timed out", "timeout"),
            ("Task interrupted by process restart", "orphaned"),
            ("backend 'x' unavailable", "config"),
            ("backend crashed: RuntimeError: boom", "internal"),
            ("max_turns exhausted", "agent_failed"),
        ],
    )
    async def test_failed_task_maps_to_error_kind(self, env: Env, error: str, kind: str) -> None:
        job_id = (await _create(env)).json()["id"]
        task = await env.store.tasks.update_status(job_id, TaskStatus.FAILED, error=error)
        assert task is not None
        await env.service.on_task_completed(task)
        view = (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()
        assert view["status"] == "failed"
        assert view["result"]["error"] == error
        assert view["result"]["error_kind"] == kind

    async def test_expired_task_is_failed_timeout(self, env: Env) -> None:
        job_id = (await _create(env)).json()["id"]
        await env.store.tasks.update_status(job_id, TaskStatus.EXPIRED, error="Task expired")
        await env.service.reconcile()
        view = (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()
        assert view["status"] == "failed"
        assert view["result"]["error_kind"] == "timeout"

    async def test_job_past_its_deadline_is_stopped_and_timed_out(self, env: Env) -> None:
        job_id = (await _create(env, timeout_seconds=600, webhook_url=WEBHOOK_URL)).json()["id"]
        job = await env.store.agent_node.get_job(job_id)
        assert job is not None
        job.deadline = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await env.store.agent_node.save_job(job)
        await env.service.reconcile()
        view = (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()
        assert view["status"] == "failed"
        assert view["result"]["error_kind"] == "timeout"
        assert "queued" in view["result"]["error"]
        task = await env.store.tasks.get(job_id)
        assert task is not None and task.status == TaskStatus.CANCELLED

    async def test_task_finished_while_service_was_down_is_picked_up(self, env: Env) -> None:
        job_id = (await _create(env)).json()["id"]
        await env.store.tasks.update_status(
            job_id, TaskStatus.COMPLETED, result={"summary": "late", "usage": {}}
        )
        await env.service.reconcile()
        view = (await env.client.get(f"/jobs/{job_id}", headers=AUTH)).json()
        assert view["status"] == "succeeded" and view["result"]["output"] == "late"


class TestCancel:
    async def test_cancel_queued_job(self, env: Env) -> None:
        job_id = (
            await _create(env, webhook_url=WEBHOOK_URL, webhook_secret=WEBHOOK_SECRET)
        ).json()["id"]
        resp = await env.client.post(f"/jobs/{job_id}/cancel", headers=AUTH)
        assert resp.status_code == 200
        view = resp.json()
        assert view["status"] == "cancelled"
        assert view["result"]["error_kind"] == "cancelled"
        assert view["result"]["error"] == "cancelled while queued; the agent was not started"
        task = await env.store.tasks.get(job_id)
        assert task is not None and task.status == TaskStatus.CANCELLED
        await env.sender.deliver_due()
        assert len(env.deliveries) == 1

    async def test_cancel_running_job(self, env: Env) -> None:
        job_id = (
            await _create(env, webhook_url=WEBHOOK_URL, webhook_secret=WEBHOOK_SECRET)
        ).json()["id"]
        await env.store.tasks.update_status(job_id, TaskStatus.RUNNING)
        resp = await env.client.post(f"/jobs/{job_id}/cancel", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"
        assert resp.json()["result"]["error"] == "cancelled while running; the agent was stopped"

    async def test_cancel_is_idempotent_and_sends_one_webhook(self, env: Env) -> None:
        job_id = (
            await _create(env, webhook_url=WEBHOOK_URL, webhook_secret=WEBHOOK_SECRET)
        ).json()["id"]
        first = await env.client.post(f"/jobs/{job_id}/cancel", headers=AUTH)
        second = await env.client.post(f"/jobs/{job_id}/cancel", headers=AUTH)
        assert first.status_code == second.status_code == 200
        assert second.json()["status"] == "cancelled"
        await env.sender.deliver_due()
        await env.sender.deliver_due()
        assert len(env.deliveries) == 1

    async def test_cancel_finished_job_changes_nothing(self, env: Env) -> None:
        job_id = (
            await _create(env, webhook_url=WEBHOOK_URL, webhook_secret=WEBHOOK_SECRET)
        ).json()["id"]
        await env.complete(job_id, "fine")
        resp = await env.client.post(f"/jobs/{job_id}/cancel", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["status"] == "succeeded"
        assert resp.json()["result"]["output"] == "fine"
        await env.sender.deliver_due()
        assert len(env.deliveries) == 1  # the completion webhook only; no cancelled one

    async def test_cancel_unknown_job_is_404(self, env: Env) -> None:
        resp = await env.client.post("/jobs/" + "f" * 32 + "/cancel", headers=AUTH)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "job not found"}
