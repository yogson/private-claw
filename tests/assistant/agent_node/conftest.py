"""Shared fixtures for the agent-node ingress tests."""

import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest_asyncio
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from assistant.agent_node.service import AgentNodeService
from assistant.agent_node.webhook import WebhookSender
from assistant.api.routers import agent_node as agent_node_router
from assistant.core.config.schemas import (
    AgentNodeConfig,
    AgentNodeRepositoryConfig,
    AppConfig,
    CapabilitiesPolicyConfig,
    McpServersConfig,
    MemoryConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
    ToolDefinition,
    ToolsConfig,
)
from assistant.store.facade import StoreFacade
from assistant.store.models import TaskStatus
from assistant.subagents.contracts import DelegationResult, DelegationRun
from assistant.subagents.coordinator import DelegationCoordinator
from assistant.subagents.interfaces import DelegationBackendAdapterInterface

TOKEN = "test-bearer-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WEBHOOK_URL = "https://automation.example/api/webhooks/resume/tok123"
WEBHOOK_SECRET = "s3cret-signing-key"


class FakeBackend(DelegationBackendAdapterInterface):
    @property
    def backend_id(self) -> str:
        return "claude_code_streaming"

    async def execute(self, request: DelegationRun) -> DelegationResult:
        return DelegationResult(ok=True, output_text="ok")


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def runtime_config(
    data_root: Path, agent_node: AgentNodeConfig, *, max_concurrent: int
) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root=str(data_root), timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id="claude-sonnet-4-5",
            model_allowlist=["claude-sonnet-4-5", "claude-haiku-4-5"],
        ),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=[], denied_capabilities=[]),
        tools=ToolsConfig(
            tools=[
                ToolDefinition(
                    tool_id="delegate_subagent_task",
                    entrypoint="assistant.agent.tools.delegate_subagent_task:delegate_subagent_task",
                    default_params={"delegation_max_concurrent_tasks": max_concurrent},  # type: ignore[arg-type]
                )
            ]
        ),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
        agent_node=agent_node,
    )


@dataclass
class Env:
    client: httpx.AsyncClient
    service: AgentNodeService
    store: StoreFacade
    coordinator: DelegationCoordinator
    sender: WebhookSender
    repo: Path
    deliveries: list[httpx.Request] = field(default_factory=list)
    webhook_status: list[int] = field(default_factory=lambda: [200])

    async def complete(self, job_id: str, summary: str = "done") -> None:
        """Mark the task finished the way the coordinator would, then notify the service."""
        await self.store.tasks.update_status(job_id, TaskStatus.RUNNING)
        task = await self.store.tasks.update_status(
            job_id,
            TaskStatus.COMPLETED,
            result={"summary": summary, "usage": {"total_cost_usd": 0.5, "num_turns": 3}},
        )
        assert task is not None
        await self.service.on_task_completed(task)


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    repo = make_repo(tmp_path / "checkout")
    cfg = AgentNodeConfig(
        enabled=True,
        auth_token=TOKEN,
        repositories={"acme/widgets": AgentNodeRepositoryConfig(path=str(repo))},
    )
    rc = runtime_config(tmp_path / "data", cfg, max_concurrent=2)
    store = StoreFacade(data_root=tmp_path / "data")
    await store.initialize()
    # Worker loop is not started: tasks stay PENDING until a test moves them.
    coordinator = DelegationCoordinator(store=store, config=rc, backends=[FakeBackend()])
    deliveries: list[httpx.Request] = []
    holder: dict[str, Env] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        deliveries.append(request)
        statuses = holder["env"].webhook_status
        code = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        return httpx.Response(code)

    sender = WebhookSender(store.agent_node, transport=httpx.MockTransport(_handler))
    service = AgentNodeService(
        config=cfg,
        runtime_config=rc,
        store=store.agent_node,
        task_store=store.tasks,
        coordinator=coordinator,
        sender=sender,
        data_root=tmp_path / "data",
    )
    app = FastAPI()
    app.include_router(agent_node_router.router)
    app.add_exception_handler(
        RequestValidationError,
        agent_node_router.request_validation_exception_handler,  # type: ignore[arg-type]
    )
    app.state.agent_node = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        e = Env(
            client=client,
            service=service,
            store=store,
            coordinator=coordinator,
            sender=sender,
            repo=repo,
            deliveries=deliveries,
        )
        holder["env"] = e
        yield e
    await store.shutdown()
