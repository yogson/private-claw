"""capability_* agent tools: thin wrappers that turn proposals into confirmations.

Also covers the end-to-end flow: tool call → confirmation → approve → manifest on disk →
the next turn's system prompt (built from the new catalog revision) has the new text.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from assistant.agent.adapter_cache import TurnAdapterCache
from assistant.agent.tools.capability_edit import (
    capability_history,
    capability_propose_edit,
    capability_propose_rollback,
    capability_read,
)
from assistant.agent.tools.deps import TurnDeps
from assistant.agent.tools.registry import _resolve_entrypoint
from assistant.core.capabilities.catalog import CapabilityCatalog
from assistant.core.capabilities.edit_service import CapabilityEditService
from assistant.core.capabilities.validation import validate_policy_bindings
from assistant.core.config.loader import ConfigLoader
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
    ToolsConfig,
)
from assistant.core.confirmation import (
    CapabilityEditHandler,
    ConfirmationDecision,
    ConfirmationDraft,
    ConfirmationHandlerRegistry,
    ConfirmationRequestResult,
    ConfirmationService,
    ConfirmationStatus,
)

SESSION = "tg:1"
MANIFEST = "capability_id: editable\nself_edit: true\nprompt: |\n  PROMPT-OLD\n"


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "editable.yaml").write_text(MANIFEST)
    (caps / "locked.yaml").write_text("capability_id: locked\nprompt: x\n")
    return tmp_path


@pytest.fixture()
def catalog(config_dir: Path) -> CapabilityCatalog:
    return CapabilityCatalog(config_dir, min_refresh_interval=0)


@pytest.fixture()
def edit_service(catalog: CapabilityCatalog) -> CapabilityEditService:
    return CapabilityEditService(catalog)


@pytest.fixture()
def confirmations(edit_service: CapabilityEditService) -> ConfirmationService:
    return ConfirmationService(ConfirmationHandlerRegistry([CapabilityEditHandler(edit_service)]))


def _ctx(
    edit_service: CapabilityEditService | None,
    service: ConfirmationService | None,
    tool_name: str = "capability_propose_edit",
) -> MagicMock:
    ctx = MagicMock()
    ctx.tool_call_id = "tc-1"
    ctx.tool_name = tool_name
    requester = None
    if service is not None:

        async def requester(draft: ConfirmationDraft) -> ConfirmationRequestResult:
            return await service.request(SESSION, draft)

    ctx.deps = TurnDeps(confirmation_requester=requester, capability_edit_service=edit_service)
    return ctx


async def _propose(ctx: MagicMock, new: str = "PROMPT-NEW", **kw: Any) -> dict[str, Any]:
    read = capability_read(ctx, "editable")
    return await capability_propose_edit(
        ctx,
        capability_id="editable",
        old_string=kw.get("old", "PROMPT-OLD"),
        new_string=new,
        reason="user asked",
        base_sha256=kw.get("base", read["base_sha256"]),
    )


def test_read_returns_raw_yaml_and_hash(edit_service: CapabilityEditService) -> None:
    result = capability_read(_ctx(edit_service, None), "editable")
    assert result["status"] == "ok"
    assert result["raw_yaml"] == MANIFEST
    assert result["self_edit"] is True and result["revision"] == 1


def test_read_of_a_locked_manifest_lists_editable_ones(edit_service: CapabilityEditService) -> None:
    result = capability_read(_ctx(edit_service, None), "locked")
    assert result["status"] == "not_editable"
    assert result["editable_capabilities"] == ["editable"]


async def test_tools_without_the_service_are_unavailable() -> None:
    ctx = _ctx(None, None)
    assert capability_read(ctx, "editable")["status"] == "unavailable"
    assert capability_history(ctx, "editable")["status"] == "unavailable"
    result = await capability_propose_edit(ctx, "editable", "a", "b", "r", "x")
    assert result["status"] == "unavailable"


async def test_propose_without_a_requester_is_unavailable(
    edit_service: CapabilityEditService,
) -> None:
    result = await _propose(_ctx(edit_service, None))
    assert result["status"] == "unavailable"


async def test_propose_creates_a_pending_confirmation(
    edit_service: CapabilityEditService, confirmations: ConfirmationService, config_dir: Path
) -> None:
    result = await _propose(_ctx(edit_service, confirmations))

    assert result["status"] == "pending_confirmation"
    assert result["deduplicated"] is False
    pending = confirmations.pending_for_session(SESSION)
    assert pending is not None and pending.confirmation_id == result["confirmation_id"]
    assert pending.kind == "capability_edit" and pending.origin_tool_call_id == "tc-1"
    assert (config_dir / "capabilities" / "editable.yaml").read_text() == MANIFEST


async def test_proposals_run_off_the_event_loop_thread(
    edit_service: CapabilityEditService,
    confirmations: ConfirmationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []
    for name in ("propose_edit", "propose_rollback"):
        original = getattr(edit_service, name)

        def spy(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            seen.append(threading.get_ident())
            return _original(*args, **kwargs)

        monkeypatch.setattr(edit_service, name, spy)
    ctx = _ctx(edit_service, confirmations)
    await _propose(ctx)
    await capability_propose_rollback(ctx, "editable", "20260101T000000000000Z__deadbeef", "r", "x")
    assert len(seen) == 2 and loop_thread not in seen


async def test_invalid_and_stale_proposals_create_nothing(
    edit_service: CapabilityEditService, confirmations: ConfirmationService
) -> None:
    ctx = _ctx(edit_service, confirmations)
    assert (await _propose(ctx, old="missing"))["status"] == "rejected_invalid"
    assert (await _propose(ctx, base="0" * 64))["status"] == "stale"
    assert (await _propose(ctx, old="self_edit: true", new="self_edit: false"))[
        "status"
    ] == "rejected_invalid"
    assert confirmations.pending_for_session(SESSION) is None


async def test_second_different_proposal_is_blocked(
    edit_service: CapabilityEditService, confirmations: ConfirmationService
) -> None:
    ctx = _ctx(edit_service, confirmations)
    first = await _propose(ctx)
    second = await _propose(ctx, new="PROMPT-OTHER")
    assert second["status"] == "another_confirmation_pending"
    assert second["pending_confirmation_id"] == first["confirmation_id"]


@pytest.fixture()
def _anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=["editable"]),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


@pytest.mark.usefixtures("_anthropic_key")
async def test_end_to_end_edit_then_rollback_reach_the_next_turns_prompt(
    edit_service: CapabilityEditService,
    confirmations: ConfirmationService,
    catalog: CapabilityCatalog,
    config_dir: Path,
) -> None:
    config = _runtime_config()
    cache = TurnAdapterCache("anthropic:claude-3-5-sonnet-20241022", 256, config, catalog.current())
    assert "PROMPT-OLD" in cache.get_or_build(config, catalog.refresh()).system_prompt

    proposed = await _propose(_ctx(edit_service, confirmations))
    resolved = await confirmations.resolve(
        proposed["confirmation_id"], ConfirmationDecision.APPROVE
    )
    assert resolved.request is not None and resolved.request.status == ConfirmationStatus.APPROVED

    adapter = cache.get_or_build(config, catalog.refresh())
    assert adapter.capability_revision == 2
    assert "PROMPT-NEW" in adapter.system_prompt and "PROMPT-OLD" not in adapter.system_prompt

    ctx = _ctx(edit_service, confirmations, tool_name="capability_propose_rollback")
    history = capability_history(ctx, "editable")
    assert history["status"] == "ok" and len(history["entries"]) == 1
    rollback = await capability_propose_rollback(
        ctx,
        capability_id="editable",
        history_id=history["entries"][0]["history_id"],
        reason="undo",
        base_sha256=capability_read(ctx, "editable")["base_sha256"],
    )
    assert rollback["status"] == "pending_confirmation"
    await confirmations.resolve(rollback["confirmation_id"], ConfirmationDecision.APPROVE)

    adapter = cache.get_or_build(config, catalog.refresh())
    assert adapter.capability_revision == 3
    assert "PROMPT-OLD" in adapter.system_prompt
    assert (config_dir / "capabilities" / "editable.yaml").read_text() == MANIFEST


# ---------------------------------------------------------------------------
# tracked config
# ---------------------------------------------------------------------------


def test_tracked_capability_admin_manifest_binds_the_edit_tools() -> None:
    root = Path(__file__).resolve().parents[4] / "config"
    runtime_config = ConfigLoader(config_dir=root).load()
    policy = runtime_config.capabilities.model_copy(
        update={"enabled_capabilities": ["capability_admin"], "denied_capabilities": []}
    )
    runtime_config = runtime_config.model_copy(update={"capabilities": policy})
    definitions = CapabilityCatalog(root).current().definitions

    assert not validate_policy_bindings(runtime_config, definitions, _resolve_entrypoint)
    admin = definitions["capability_admin"]
    assert {b.tool_id for b in admin.tools} == {
        "capability_read",
        "capability_propose_edit",
        "capability_history",
        "capability_propose_rollback",
    }
    assert admin.self_edit is False
