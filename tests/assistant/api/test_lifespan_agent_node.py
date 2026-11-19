"""Lifespan wiring of the agent-node ingress (works with Telegram disabled)."""

from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from structlog.testing import capture_logs

from assistant.agent_node.service import AgentNodeService
from assistant.api.lifespan import _lifespan
from assistant.core.config.schemas import AgentNodeConfig, AgentNodeRepositoryConfig
from assistant.store.models import TaskRecord, TaskStatus
from assistant.subagents.coordinator import DelegationCoordinator
from tests.assistant.api.test_telegram_lifecycle import _runtime_config
from tests.assistant.capability_helpers import StaticCatalog, snapshot_of


def _patched(config: object) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(
        patch(
            "assistant.api.lifespan.bootstrap_with_catalog",
            return_value=(config, StaticCatalog(snapshot_of({}))),
        )
    )
    stack.enter_context(patch("assistant.api.lifespan.configure_logging", return_value="x"))
    stack.enter_context(patch("assistant.api.lifespan.configure_logfire", return_value=False))
    return stack


def _config(tmp_path: Path, agent_node: AgentNodeConfig | None) -> object:
    config = _runtime_config(allowlist=[1])
    config.telegram.enabled = False
    config.app.data_root = str(tmp_path / "data")
    config.agent_node = agent_node
    return config


async def test_absent_agent_node_config_leaves_the_ingress_off(tmp_path: Path) -> None:
    app = FastAPI()
    with _patched(_config(tmp_path, None)):
        async with _lifespan(app):
            assert app.state.agent_node is None
            assert app.state.delegation_coordinator is None


@pytest.mark.parametrize("token", ["", "   ", " padded", "padded\n", "tökén"])
async def test_unusable_token_keeps_ingress_off_and_does_not_abort_startup(
    tmp_path: Path, token: str
) -> None:
    app = FastAPI()
    cfg = AgentNodeConfig(enabled=True, auth_token=token)
    assert cfg.auth_token_problem() is not None
    with _patched(_config(tmp_path, cfg)), capture_logs() as logs:
        async with _lifespan(app):
            assert app.state.agent_node is None
    warning = next(e for e in logs if e["event"] == "agent_node.disabled")
    assert "auth_token" in warning["reason"]


def test_plain_token_is_fine() -> None:
    assert AgentNodeConfig(enabled=True, auth_token="abc-123_XYZ").auth_token_problem() is None


async def test_enabled_builds_service_and_routes_completions_by_source(tmp_path: Path) -> None:
    app = FastAPI()
    cfg = AgentNodeConfig(
        enabled=True,
        auth_token="tok",
        repositories={"o/r": AgentNodeRepositoryConfig(path=str(tmp_path))},
    )
    with _patched(_config(tmp_path, cfg)):
        async with _lifespan(app):
            service = app.state.agent_node
            assert isinstance(service, AgentNodeService)
            coordinator = app.state.delegation_coordinator
            assert isinstance(coordinator, DelegationCoordinator)
            # The coordinator's completion callback is the dispatcher: an ingress task
            # is finalized, a non-ingress task is ignored (Telegram is off here).
            created = await service.create_job(
                body_dump={"user_prompt": "x"},
                user_prompt="x",
                system_prompt=None,
                repo_full_name=None,
                base_branch="main",
                timeout_seconds=None,
                requested_deadline=None,
                model=None,
                metadata={},
                webhook_url=None,
                webhook_secret=None,
                idempotency_key=None,
            )
            store = app.state.store
            task = await store.tasks.update_status(
                created.view.id,
                TaskStatus.COMPLETED,
                result={"summary": "s", "usage": {}},
            )
            assert task is not None
            await coordinator._completion_callback(task)  # noqa: SLF001
            view = await service.get_view(created.view.id)
            assert view is not None and view.status.value == "succeeded"
            now = datetime.now(UTC)
            stray = TaskRecord(
                task_id="stray",
                parent_session_id="tg:1",
                task_type="delegation",
                status=TaskStatus.COMPLETED,
                created_at=now,
                updated_at=now,
            )
            await coordinator._completion_callback(stray)  # noqa: SLF001  (no handler: no error)
