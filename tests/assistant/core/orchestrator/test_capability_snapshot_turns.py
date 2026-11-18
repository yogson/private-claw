"""Orchestrator turns use exactly one capability snapshot and follow manifest edits live."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from assistant.agent.adapter_cache import TurnAdapterCache
from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
from assistant.core.capabilities.catalog import CapabilityCatalog
from assistant.core.capabilities.snapshot import CapabilitySnapshot
from assistant.core.config.schemas import (
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
from assistant.core.confirmation import ConfirmationHandlerRegistry, ConfirmationService
from assistant.core.events.models import EventSource, EventType, OrchestratorEvent
from assistant.core.orchestrator import Orchestrator
from assistant.core.orchestrator import service as orchestrator_service
from assistant.store.models import SessionRecordType

_MODEL = "claude-3-5-sonnet-20241022"


def _config(config_dir: Path) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id=_MODEL,
            model_allowlist=[_MODEL],
            max_tokens_default=256,
            prompt_trace_enabled=True,
        ),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=["assistant"]),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(lock_ttl_seconds=30, idempotency_retention_seconds=86400),
        memory=MemoryConfig(api_key="test"),
        config_dir=config_dir,
    )


def _event(event_id: str) -> OrchestratorEvent:
    return OrchestratorEvent(
        event_id=event_id,
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:1",
        user_id="1",
        created_at=datetime.now(UTC),
        trace_id=f"trace-{event_id}",
        text="hello",
    )


def _write_manifest(config_dir: Path, prompt: str) -> None:
    caps = config_dir / "capabilities"
    caps.mkdir(parents=True, exist_ok=True)
    (caps / "assistant.yaml").write_text(f"capability_id: assistant\nprompt: {prompt}\n")


def _store() -> MagicMock:
    store = MagicMock()
    store.sessions.replay_for_turn = AsyncMock(return_value=[])
    store.sessions.append = AsyncMock()
    store.sessions.get_next_sequence = AsyncMock(return_value=0)
    return store


def _session_factory() -> MagicMock:
    factory = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=None)
    factory.resume = AsyncMock(return_value=ctx)
    factory.persist_state = AsyncMock(return_value=True)
    return factory


def _idempotency() -> MagicMock:
    svc = MagicMock()
    svc.build_key = lambda s, e: f"{s}:{e}"
    svc.check_and_register = AsyncMock(return_value=(False, None))
    return svc


@pytest.fixture()
def prompts_seen(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record the system prompt of the adapter each turn runs on; never call the model."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen: list[str] = []

    async def fake_run_turn(
        self: PydanticAITurnAdapter, **kwargs: Any
    ) -> tuple[str, list[Any], None]:
        seen.append(self.system_prompt)
        return "ok", [], None

    with patch.object(PydanticAITurnAdapter, "run_turn", fake_run_turn):
        yield seen


def _orchestrator(
    config: RuntimeConfig, catalog: CapabilityCatalog
) -> tuple[Orchestrator, MagicMock, TurnAdapterCache]:
    store = _store()
    cache = TurnAdapterCache(
        model_id=f"anthropic:{_MODEL}",
        max_tokens=256,
        base_config=config,
        snapshot=catalog.current(),
    )
    orch = Orchestrator(
        store=store,
        config=config,
        idempotency=_idempotency(),
        session_factory=_session_factory(),
        adapter_cache=cache,
        capability_catalog=catalog,
    )
    return orch, store, cache


def _prompt_trace(store: MagicMock) -> dict[str, Any]:
    records = store.sessions.append.call_args_list[0].args[0]
    (summary,) = [r for r in records if r.record_type == SessionRecordType.TURN_SUMMARY]
    trace: dict[str, Any] = summary.payload["capability_audit"]["prompt_trace"]
    return trace


async def test_external_edit_is_used_on_next_turn(tmp_path: Path, prompts_seen: list[str]) -> None:
    _write_manifest(tmp_path, "PROMPT-V1")
    catalog = CapabilityCatalog(tmp_path, min_refresh_interval=0)
    orch, store, cache = _orchestrator(_config(tmp_path), catalog)

    await orch.execute_turn(_event("e1"))
    assert _prompt_trace(store)["capabilities"]["revision"] == 1

    _write_manifest(tmp_path, "PROMPT-V2")  # e.g. an editor or git pull; no restart
    store.sessions.append.reset_mock()
    await orch.execute_turn(_event("e2"))

    assert "PROMPT-V1" in prompts_seen[0]
    assert "PROMPT-V2" in prompts_seen[1]
    trace = _prompt_trace(store)
    assert trace["capabilities"]["revision"] == 2
    assert trace["capabilities"]["active"] == ["assistant"]
    sha = catalog.current().files["assistant"].sha256
    assert trace["capabilities"]["manifest_sha256"] == {"assistant": sha}
    assert cache.size == 1  # the revision-1 adapter was evicted


async def test_broken_manifest_on_disk_does_not_break_the_turn(
    tmp_path: Path, prompts_seen: list[str]
) -> None:
    """Regression: a broken file used to raise from build_tool_runtime_params on every turn."""
    _write_manifest(tmp_path, "PROMPT-GOOD")
    catalog = CapabilityCatalog(tmp_path, min_refresh_interval=0)
    orch, _, _ = _orchestrator(_config(tmp_path), catalog)
    (tmp_path / "capabilities" / "assistant.yaml").write_text("capability_id: [broken\n")

    result = await orch.execute_turn(_event("e1"))

    assert result is not None and result.text == "ok"
    assert "PROMPT-GOOD" in prompts_seen[0]
    assert catalog.current().blocking_errors


async def test_every_consumer_of_a_turn_sees_one_snapshot(
    tmp_path: Path, prompts_seen: list[str]
) -> None:
    _write_manifest(tmp_path, "PROMPT")
    catalog = CapabilityCatalog(tmp_path, min_refresh_interval=0)
    orch, _, cache = _orchestrator(_config(tmp_path), catalog)
    _write_manifest(tmp_path, "PROMPT-EDITED")  # pending change, picked up at turn start

    seen: dict[str, CapabilitySnapshot] = {}
    real_refresh = catalog.refresh
    real_get_or_build = cache.get_or_build
    real_params = orchestrator_service.build_tool_runtime_params
    real_mismatch = orch._detect_tool_mismatch

    def spy_refresh() -> CapabilitySnapshot:
        seen["refresh"] = real_refresh()
        return seen["refresh"]

    def spy_get_or_build(config: RuntimeConfig, snapshot: CapabilitySnapshot) -> Any:
        seen["adapter"] = snapshot
        return real_get_or_build(config, snapshot)

    def spy_params(config: RuntimeConfig, snapshot: CapabilitySnapshot) -> Any:
        seen["tool_params"] = snapshot
        return real_params(config, snapshot)

    def spy_mismatch(records: Any, config: RuntimeConfig, snapshot: CapabilitySnapshot) -> Any:
        seen["mismatch"] = snapshot
        return real_mismatch(records, config, snapshot)

    refresh_mock = MagicMock(side_effect=spy_refresh)
    with (
        patch.object(catalog, "refresh", refresh_mock),
        patch.object(cache, "get_or_build", spy_get_or_build),
        patch.object(orchestrator_service, "build_tool_runtime_params", spy_params),
        patch.object(orch, "_detect_tool_mismatch", spy_mismatch),
    ):
        await orch.execute_turn(_event("e1"))

    assert refresh_mock.call_count == 1
    assert {k: v.revision for k, v in seen.items()} == {
        "refresh": 2,
        "adapter": 2,
        "tool_params": 2,
        "mismatch": 2,
    }
    assert len({id(v) for v in seen.values()}) == 1  # the very same snapshot object
    assert "PROMPT-EDITED" in prompts_seen[0]


async def test_catalog_backed_turn_registers_memory_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_propose_update bound by a catalog manifest still goes through confirmations."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    caps = tmp_path / "capabilities"
    caps.mkdir(parents=True)
    (caps / "assistant.yaml").write_text(
        "capability_id: assistant\nprompt: PROMPT\n"
        "tools:\n  - tool_id: memory_propose_update\n    enabled: true\n"
    )
    config = _config(tmp_path).model_copy(
        update={
            "tools": ToolsConfig(
                tools=[
                    ToolDefinition(
                        tool_id="memory_propose_update",
                        entrypoint=(
                            "assistant.agent.tools.memory_propose_update:memory_propose_update"
                        ),
                    )
                ]
            )
        }
    )
    catalog = CapabilityCatalog(tmp_path, min_refresh_interval=0)
    store = _store()
    cache = TurnAdapterCache(
        model_id=f"anthropic:{_MODEL}",
        max_tokens=256,
        base_config=config,
        snapshot=catalog.current(),
    )
    confirmations = ConfirmationService(ConfirmationHandlerRegistry())
    orch = orchestrator_service.Orchestrator(
        store=store,
        config=config,
        idempotency=_idempotency(),
        session_factory=_session_factory(),
        adapter_cache=cache,
        capability_catalog=catalog,
        confirmation_service=confirmations,
    )
    # The turn's refresh sees the same revision, so it reuses this cached adapter.
    adapter = cache.get_or_build(config, catalog.current())
    tool_names: list[list[str]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool_names.append([t.name for t in info.function_tools])
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="memory_propose_update",
                        args={
                            "intent_id": "i1",
                            "action": "upsert",
                            "memory_type": "preferences",
                            "reason": "explicit ask",
                            "source": "explicit_user_request",
                            "requires_user_confirmation": True,
                            "candidate": {"body_markdown": "Likes tea."},
                        },
                        tool_call_id="tool-1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Proposed.")])

    with adapter._agent.override(model=FunctionModel(respond)):
        result = await orch.execute_turn(_event("e1"))

    assert result is not None and result.text == "Proposed."
    assert "memory_propose_update" in tool_names[0]
    assert cache.size == 1
    assert [c.kind for c in result.confirmations] == ["memory_update"]
    assert confirmations.pending_for_session("tg:1") is result.confirmations[0]
    trace = _prompt_trace(store)
    assert trace["capabilities"]["revision"] == 1
    assert trace["capabilities"]["active"] == ["assistant"]
