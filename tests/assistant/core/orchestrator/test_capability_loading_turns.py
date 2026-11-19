"""capability_load / capability_unload through real orchestrator turns."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from assistant.agent.adapter_cache import TurnAdapterCache
from assistant.agent.deps import TurnDeps
from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
from assistant.agent.system_prompt_builder import INDEX_HEADING
from assistant.core.capabilities.schemas import CapabilityDefinition, CapabilityToolBinding
from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesPolicyConfig,
    CompactionConfig,
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
from assistant.core.events.models import EventSource, EventType, OrchestratorEvent
from assistant.core.orchestrator import Orchestrator
from assistant.core.session_context import SessionCapabilityContextService
from assistant.store.facade import StoreFacade
from assistant.store.models import SessionRecord, SessionRecordType
from tests.assistant.capability_helpers import StaticCatalog, snapshot_of

_MODEL = "claude-3-5-sonnet-20241022"
_HERE = "tests.assistant.core.orchestrator.test_capability_loading_turns"
SESSION = "tg:1:abc"

type Responder = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


async def notes_read(ctx: RunContext[TurnDeps]) -> str:
    """Read the user's notes."""
    return "NOTE: buy milk"


def _defs(*, notes_loadable: bool = True) -> dict[str, CapabilityDefinition]:
    return {
        "assistant": CapabilityDefinition(capability_id="assistant", prompt="PROMPT-assistant"),
        "notes": CapabilityDefinition(
            capability_id="notes",
            description="Apple Notes",
            loadable=notes_loadable,
            prompt="PROMPT-notes",
            tools=[CapabilityToolBinding(tool_id="notes_read")],
        ),
        "deploy": CapabilityDefinition(
            capability_id="deploy", description="Deploy", loadable=True, prompt="PROMPT-deploy"
        ),
    }


def _config(tmp_path: Path, *, compaction: bool = False) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root=str(tmp_path), timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id=_MODEL,
            model_allowlist=[_MODEL],
            max_tokens_default=256,
            prompt_trace_enabled=True,
        ),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=["assistant"]),
        tools=ToolsConfig(
            tools=[
                ToolDefinition(tool_id="notes_read", entrypoint=f"{_HERE}:notes_read"),
                ToolDefinition(
                    tool_id="capability_load",
                    entrypoint="assistant.agent.tools.capability_loading:capability_load",
                ),
                ToolDefinition(
                    tool_id="capability_unload",
                    entrypoint="assistant.agent.tools.capability_loading:capability_unload",
                ),
            ]
        ),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(lock_ttl_seconds=30, idempotency_retention_seconds=86400),
        memory=MemoryConfig(api_key="test"),
        compaction=CompactionConfig(enabled=compaction, keep_recent_turns=0),
        config_dir=tmp_path,
    )


def _event(event_id: str, session_id: str = SESSION) -> OrchestratorEvent:
    return OrchestratorEvent(
        event_id=event_id,
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=session_id,
        user_id="1",
        created_at=datetime.now(UTC),
        trace_id=f"trace-{event_id}",
        text="hello",
    )


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


class Harness:
    """Orchestrator over a real store + persisted capability context, model faked per turn."""

    def __init__(self, tmp_path: Path, catalog: StaticCatalog, *, compaction: bool = False) -> None:
        self.config = _config(tmp_path, compaction=compaction)
        self.catalog = catalog
        self.store = StoreFacade(data_root=tmp_path, enable_runtime_manager=False)
        self.context_path = tmp_path / "capability_context.json"
        self.responder: Responder = _text("ok")
        self.prompts: list[str] = []
        self.tool_names: list[list[str]] = []
        real_iter = Agent.iter
        model = FunctionModel(self._respond)

        def fake_iter(agent: Agent[Any, Any], *args: Any, **kwargs: Any) -> Any:
            kwargs["model"] = model
            return real_iter(agent, *args, **kwargs)

        self._iter_patch = patch.object(Agent, "iter", fake_iter)
        self._iter_patch.start()
        self.restart()

    def restart(self) -> None:
        """A new process: new orchestrator, cache and context service on the same files."""
        self.context = SessionCapabilityContextService(self.context_path)
        self.cache = TurnAdapterCache(
            model_id=f"anthropic:{_MODEL}",
            max_tokens=256,
            base_config=self.config,
            snapshot=self.catalog.current(),
        )
        self.orch = Orchestrator(
            store=self.store,
            config=self.config,
            idempotency=_idempotency(),
            session_factory=_session_factory(),
            adapter_cache=self.cache,
            capability_catalog=self.catalog,
            capability_context=self.context,
        )
        real = self.cache.get_or_build

        def get_or_build(config: RuntimeConfig, snapshot: Any) -> PydanticAITurnAdapter:
            adapter = real(config, snapshot)
            self.prompts.append(adapter.system_prompt)
            return adapter

        self.cache.get_or_build = get_or_build  # type: ignore[method-assign]

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.tool_names.append(sorted(t.name for t in info.function_tools))
        return self.responder(messages, info)

    async def turn(self, event_id: str, session_id: str = SESSION) -> str:
        result = await self.orch.execute_turn(_event(event_id, session_id))
        assert result is not None
        return result.text

    async def trace(self, session_id: str = SESSION) -> dict[str, Any]:
        records = await self.store.sessions.read_session(session_id)
        summaries = [r for r in records if r.record_type == SessionRecordType.TURN_SUMMARY]
        trace: dict[str, Any] = summaries[-1].payload["capability_audit"]["prompt_trace"]
        return trace

    def close(self) -> None:
        self._iter_patch.stop()


def _text(text: str) -> Responder:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=text)])

    return respond


def _tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [
        p
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ]


def _script(*calls: tuple[str, dict[str, Any]]) -> Responder:
    """Call the given tools one per model request, then answer with the last tool result."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = _tool_returns(messages[-1:])
        done = len(_tool_returns(messages))
        if done < len(calls):
            name, args = calls[done]
            return ModelResponse(
                parts=[ToolCallPart(tool_name=name, args=args, tool_call_id=f"call-{done}")]
            )
        last = returns[-1].content if returns else ""
        return ModelResponse(parts=[TextPart(content=f"done: {last}")])

    return respond


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    h = Harness(tmp_path, StaticCatalog(snapshot_of(_defs())))
    yield h
    h.close()


async def test_loaded_tools_are_callable_in_the_same_turn(harness: Harness) -> None:
    harness.responder = _script(
        ("capability_load", {"capability_id": "notes", "reason": "user asked for notes"}),
        ("notes_read", {}),
    )

    text = await harness.turn("e1")

    assert text == "done: NOTE: buy milk"
    assert "notes_read" not in harness.tool_names[0]
    assert "notes_read" in harness.tool_names[1]  # next model request of the same turn
    assert harness.context.get_loaded_capabilities(SESSION) == ["notes"]
    records = await harness.store.sessions.read_session(SESSION)
    (load_result,) = [
        r.payload
        for r in records
        if r.record_type == SessionRecordType.TOOL_RESULT
        and r.payload.get("tool_name") == "capability_load"
    ]
    assert load_result["result"]["status"] == "loaded"
    trace = await harness.trace()
    assert trace["capabilities"]["loading_events"] == [
        {
            "event": "loaded",
            "capability_id": "notes",
            "activated": ["notes"],
            "reason": "user asked for notes",
        }
    ]
    # The trace reflects the set the turn started with.
    assert trace["capabilities"]["loaded"] == []


async def test_load_result_carries_prompt_tools_and_availability(harness: Harness) -> None:
    captured: list[Any] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = _tool_returns(messages)
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="capability_load",
                        args={"capability_id": "notes", "reason": "notes"},
                        tool_call_id="c1",
                    )
                ]
            )
        captured.append(returns[0].content)
        return ModelResponse(parts=[TextPart(content="ok")])

    harness.responder = respond
    await harness.turn("e1")

    (result,) = captured
    assert result["status"] == "loaded"
    assert result["prompt"] == "PROMPT-notes"
    assert result["tools"] == ["notes_read"]
    assert result["tools_available"] == "next_step"
    assert "next step" in result["note"]


async def test_loaded_capability_is_in_the_prompt_from_the_next_turn(harness: Harness) -> None:
    harness.responder = _script(("capability_load", {"capability_id": "notes", "reason": "r"}))
    await harness.turn("e1")
    harness.responder = _text("ok")

    await harness.turn("e2")

    first, second = harness.prompts
    assert "PROMPT-notes" not in first and "- notes — Apple Notes" in first
    assert "PROMPT-notes" in second and "- notes —" not in second
    assert second.startswith(first[: first.index(INDEX_HEADING)])  # base prefix unchanged
    assert "notes_read" in harness.tool_names[-1]
    trace = await harness.trace()
    assert trace["capabilities"]["base"] == ["assistant"]
    assert trace["capabilities"]["loaded"] == ["notes"]
    assert trace["capabilities"]["active"] == ["assistant", "notes"]
    assert trace["capabilities"]["index"] == ["deploy"]


async def test_unload_removes_the_capability_from_the_next_turn(harness: Harness) -> None:
    harness.context.set_loaded_capabilities(SESSION, ["notes"])
    harness.responder = _script(("capability_unload", {"capability_id": "notes"}))

    text = await harness.turn("e1")

    assert "unloaded" in text
    assert harness.context.get_loaded_capabilities(SESSION) == []
    harness.responder = _text("ok")
    await harness.turn("e2")
    assert "PROMPT-notes" in harness.prompts[0]
    assert "PROMPT-notes" not in harness.prompts[1]
    assert "notes_read" not in harness.tool_names[-1]
    assert harness.cache.size == 2  # back on the pre-warmed default adapter (cache hit)


async def test_rejected_loads_do_not_change_the_session(harness: Harness) -> None:
    harness.responder = _script(
        ("capability_load", {"capability_id": "ghost", "reason": "r"}),
        ("capability_load", {"capability_id": "assistant", "reason": "r"}),
        ("capability_unload", {"capability_id": "assistant"}),
    )
    await harness.turn("e1")

    assert harness.context.get_loaded_capabilities(SESSION) == []
    events = (await harness.trace())["capabilities"]["loading_events"]
    assert [(e["event"], e["status"]) for e in events] == [
        ("load_rejected", "unknown"),
        ("load_rejected", "already_active"),
        ("unload_rejected", "base_capability"),
    ]


async def test_loaded_set_survives_a_restart(harness: Harness) -> None:
    harness.responder = _script(("capability_load", {"capability_id": "notes", "reason": "r"}))
    await harness.turn("e1")

    harness.restart()
    harness.responder = _text("ok")
    await harness.turn("e2")

    assert "PROMPT-notes" in harness.prompts[-1]
    assert "notes_read" in harness.tool_names[-1]


async def test_loaded_set_survives_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    h = Harness(tmp_path, StaticCatalog(snapshot_of(_defs())), compaction=True)
    try:
        h.responder = _script(("capability_load", {"capability_id": "notes", "reason": "r"}))
        await h.turn("e1")
        h.responder = _text("ok")
        await h.turn("e2")
        assert h.orch._compaction_service is not None
        with patch.object(
            h.orch._compaction_service, "summarize", AsyncMock(return_value="SUMMARY")
        ):
            assert await h.orch.compact_session(SESSION, "trace", "1") is True
        records = await h.store.sessions.read_session(SESSION)
        assert not any(r.payload.get("tool_name") == "capability_load" for r in records)

        await h.turn("e3")

        assert "PROMPT-notes" in h.prompts[-1]
        assert "notes_read" in h.tool_names[-1]
    finally:
        h.close()


async def test_sessions_do_not_share_loaded_capabilities(harness: Harness) -> None:
    harness.responder = _script(("capability_load", {"capability_id": "notes", "reason": "r"}))
    await harness.turn("e1", session_id="tg:1:one")
    harness.responder = _text("ok")

    await harness.turn("e2", session_id="tg:1:two")

    assert "PROMPT-notes" not in harness.prompts[-1]
    assert "notes_read" not in harness.tool_names[-1]
    assert harness.context.get_loaded_capabilities("tg:1:two") == []


async def test_live_reload_unflagging_a_loaded_capability_ignores_it(harness: Harness) -> None:
    harness.context.set_loaded_capabilities(SESSION, ["notes"])
    harness.catalog.snapshot = snapshot_of(_defs(notes_loadable=False), revision=2)

    with structlog.testing.capture_logs() as logs:
        await harness.turn("e1")

    assert "PROMPT-notes" not in harness.prompts[-1]
    assert "notes_read" not in harness.tool_names[-1]
    assert harness.context.get_loaded_capabilities(SESSION) == ["notes"]  # kept, inactive
    warnings = [e for e in logs if e["event"] == "capability.loaded_ignored"]
    assert [(w["capability_id"], w["reason"]) for w in warnings] == [("notes", "not_loadable")]
    trace = await harness.trace()
    assert trace["capabilities"]["loaded_ignored"] == {"notes": "not_loadable"}
    assert trace["capabilities"]["index"] == ["deploy"]


async def test_live_reload_deleting_a_loaded_manifest_ignores_it(harness: Harness) -> None:
    harness.context.set_loaded_capabilities(SESSION, ["notes"])
    defs = _defs()
    del defs["notes"]
    harness.catalog.snapshot = snapshot_of(defs, revision=2)

    await harness.turn("e1")

    assert "PROMPT-notes" not in harness.prompts[-1]
    assert (await harness.trace())["capabilities"]["loaded_ignored"] == {"notes": "unknown"}


def _tool_call_record(tool_name: str, sequence: int = 1) -> SessionRecord:
    return SessionRecord(
        session_id=SESSION,
        sequence=sequence,
        event_id=f"evt-{sequence}",
        turn_id="t1",
        timestamp=datetime.now(UTC),
        record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
        payload={"tool_name": tool_name, "tool_call_id": f"c{sequence}", "arguments": {}},
    )


def test_mismatch_check_counts_loaded_capability_tools(harness: Harness) -> None:
    records = [_tool_call_record("notes_read"), _tool_call_record("capability_load", 2)]
    snapshot = harness.catalog.current()
    event = _event("e1")

    assert harness.orch._detect_tool_mismatch(records, harness.config, snapshot) is not None
    harness.context.set_loaded_capabilities(SESSION, ["notes"])
    effective = harness.orch._effective_config(event, snapshot)
    assert effective is not None
    assert harness.orch._detect_tool_mismatch(records, effective, snapshot) is None


def test_mismatch_check_ignores_capability_loading_tools(harness: Harness) -> None:
    snapshot = snapshot_of({"assistant": _defs()["assistant"]})
    records = [_tool_call_record("capability_load"), _tool_call_record("capability_unload", 2)]
    assert harness.orch._detect_tool_mismatch(records, harness.config, snapshot) is None


def test_effective_config_is_none_without_override_or_loaded_set(harness: Harness) -> None:
    assert harness.orch._effective_config(_event("e1"), harness.catalog.current()) is None


def test_effective_config_combines_override_and_loaded_set(harness: Harness) -> None:
    harness.context.set_loaded_capabilities(SESSION, ["notes"])
    event = _event("e1").model_copy(update={"capabilities_override": ["deploy"]})
    cfg = harness.orch._effective_config(event, harness.catalog.current())
    assert cfg is not None
    assert cfg.capabilities.enabled_capabilities == ["deploy"]
    assert cfg.capabilities.session_loaded_capabilities == ["notes"]
    assert (
        cfg.capabilities.max_loaded_capabilities
        == harness.config.capabilities.max_loaded_capabilities
    )
