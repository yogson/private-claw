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


@pytest.mark.parametrize("key", ["1", "~", "!!binary c2VsZl9lZGl0"])
async def test_non_string_top_level_key_is_a_rejection_not_an_exception(
    edit_service: CapabilityEditService, confirmations: ConfirmationService, key: str
) -> None:
    # Reviewer repro S5: a TypeError used to escape the tool call.
    ctx = _ctx(edit_service, confirmations)
    result = await _propose(ctx, old="prompt: |", new=f"{key}: x\nprompt: |")
    assert result["status"] == "rejected_invalid"
    assert "top-level keys must be strings" in result["reason"]
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


async def test_unexpected_service_errors_never_escape_the_tools(
    edit_service: CapabilityEditService,
    confirmations: ConfirmationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reviewer S6: RecursionError used to fail the whole turn.
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RecursionError("depth exceeded near TOKEN=ghp_secret")

    ctx = _ctx(edit_service, confirmations)
    for name in ("read", "history", "propose_edit", "propose_rollback"):
        monkeypatch.setattr(edit_service, name, boom)
    results = [
        capability_read(ctx, "editable"),
        capability_history(ctx, "editable"),
        await capability_propose_edit(ctx, "editable", "a", "b", "r", "x"),
        await capability_propose_rollback(ctx, "editable", "h", "r", "x"),
    ]
    for result in results:
        assert result["status"] == "rejected_invalid"
        assert "RecursionError" in result["reason"] and "ghp_secret" not in result["reason"]


@pytest.mark.parametrize("depth", [100, 500, 1000, 5000])
async def test_deeply_nested_proposal_is_rejected_by_the_tool(
    edit_service: CapabilityEditService, confirmations: ConfirmationService, depth: int
) -> None:
    ctx = _ctx(edit_service, confirmations)
    result = await _propose(ctx, new="PROMPT-NEW\nx: " + "[" * depth + "]" * depth)
    assert result["status"] == "rejected_invalid"


async def test_resolution_record_never_quotes_another_manifests_yaml(
    edit_service: CapabilityEditService,
    confirmations: ConfirmationService,
    config_dir: Path,
) -> None:
    # Reviewer repro S8, end to end: the [[CONFIRMATION_RESOLVED]] record is replayed to
    # the model, so it must not carry PyYAML's excerpt of a manifest the agent cannot read.
    from structlog.testing import capture_logs

    from assistant.core.confirmation.history import format_resolution_text

    proposed = await _propose(_ctx(edit_service, confirmations))
    (config_dir / "capabilities" / "other.yaml").write_text(
        "capability_id: other\nclaude_code_settings:\n  mcp_servers:\n    gh:\n"
        "      command: x\n      env:\n        TOKEN: ghpLEAKEDSECRET: [\n"
    )
    with capture_logs() as logs:
        resolved = await confirmations.resolve(
            proposed["confirmation_id"], ConfirmationDecision.APPROVE
        )
    assert resolved.request is not None and resolved.request.result is not None
    assert resolved.request.result.status == "reload_rejected"
    record = format_resolution_text(resolved.request)
    assert "[other.yaml] YAML parse error: ScannerError" in record
    assert "ghpLEAKEDSECRET" not in record and "TOKEN" not in record
    assert "ghpLEAKEDSECRET" not in repr(logs)


async def test_prompt_proposal_carries_the_full_prompt_change_as_an_attachment(
    edit_service: CapabilityEditService, confirmations: ConfirmationService
) -> None:
    result = await _propose(_ctx(edit_service, confirmations), new="PROMPT-NEW\n  second line")
    pending = confirmations.pending_for_session(SESSION)
    assert pending is not None and pending.confirmation_id == result["confirmation_id"]
    (attachment,) = pending.attachments
    assert attachment.parts == ["@@ -1,2 +1,3 @@\n- PROMPT-OLD\n+ PROMPT-NEW\n+ second line\n  "]
    assert "PROMPT-NEW" not in pending.summary_for_user


# --- B1 (fifth review of #64): an attachment edge case never fails the turn ---------

_BLOCK_MANIFEST = "capability_id: editable\nself_edit: true\nprompt: |\n  intro\n  OLDLINE\n"


@pytest.mark.parametrize("k", [3662, 3663, 3664, 3696, 3697, 3698])
async def test_reviewer_lengths_become_a_pending_confirmation(
    edit_service: CapabilityEditService,
    confirmations: ConfirmationService,
    config_dir: Path,
    k: int,
) -> None:
    (config_dir / "capabilities" / "editable.yaml").write_text(_BLOCK_MANIFEST)
    result = await _propose(_ctx(edit_service, confirmations), new="N" * k, old="OLDLINE")
    assert result["status"] == "pending_confirmation"
    pending = confirmations.pending_for_session(SESSION)
    assert pending is not None
    (attachment,) = pending.attachments
    assert all(part.strip() for part in attachment.parts)
    assert "N" * k in "".join(attachment.parts).replace("\n+…", "")


@pytest.mark.parametrize("tool_name", ["capability_propose_edit", "capability_propose_rollback"])
async def test_a_draft_that_fails_validation_is_rejected_invalid(
    edit_service: CapabilityEditService,
    confirmations: ConfirmationService,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    from assistant.agent.tools import capability_edit as tools_module
    from assistant.core.confirmation import ConfirmationAttachment

    def invalid_draft(*_args: Any, **_kwargs: Any) -> ConfirmationDraft:
        ConfirmationAttachment(title="t", parts=["  "])  # raises ValidationError
        raise AssertionError("unreachable")

    ctx = _ctx(edit_service, confirmations, tool_name=tool_name)
    read = capability_read(ctx, "editable")
    proposal = edit_service.propose_edit(
        "editable", "PROMPT-OLD", "PROMPT-NEW", "r", read["base_sha256"]
    )
    monkeypatch.setattr(edit_service, "propose_rollback", lambda *_a: proposal)
    monkeypatch.setattr(tools_module, "build_capability_edit_draft", invalid_draft)
    if tool_name == "capability_propose_edit":
        result = await _propose(ctx)
    else:
        result = await capability_propose_rollback(ctx, "editable", "h", "r", read["base_sha256"])
    assert result["status"] == "rejected_invalid"
    assert "ValidationError" in result["reason"] and "nothing was proposed" in result["reason"]
    assert confirmations.pending_for_session(SESSION) is None


async def test_random_prompt_edits_never_raise_out_of_the_tool(tmp_path: Path) -> None:
    import random

    import yaml

    rng = random.Random(645)
    alphabet = "ab Пр 🧠"

    def line() -> str:
        roll = rng.random()
        if roll < 0.35:
            return ""
        if roll < 0.6:
            return "".join(rng.choices(alphabet, k=rng.randint(1, 8))).strip() or "x"
        return "N" * rng.randint(3600, 3705) + rng.choice(["", "я", "🧠"])

    statuses: set[str] = set()
    for i in range(40):
        caps = tmp_path / f"c{i}" / "capabilities"
        caps.mkdir(parents=True)
        old = "\n".join(line() for _ in range(rng.randint(1, 5))) + "\n"
        new = "\n".join(line() for _ in range(rng.randint(1, 5))) + "\n" * rng.randint(1, 3)
        raw = yaml.safe_dump(
            {"capability_id": "editable", "self_edit": True, "prompt": old},
            allow_unicode=True,
            width=1 << 20,
        )
        (caps / "editable.yaml").write_text(raw)
        service = CapabilityEditService(
            CapabilityCatalog(caps.parent, min_refresh_interval=0), max_prompt_parts=50
        )
        confirmations = ConfirmationService(
            ConfirmationHandlerRegistry([CapabilityEditHandler(service)])
        )
        ctx = _ctx(service, confirmations)
        replacement = yaml.safe_dump(
            {"capability_id": "editable", "self_edit": True, "prompt": new},
            allow_unicode=True,
            width=1 << 20,
        )
        read = capability_read(ctx, "editable")
        result = await capability_propose_edit(
            ctx, "editable", raw, replacement, "r", read["base_sha256"]
        )
        statuses.add(result["status"])
        pending = confirmations.pending_for_session(SESSION)
        if pending is not None:
            assert all(p.strip() for a in pending.attachments for p in a.parts)
    assert statuses <= {"pending_confirmation", "rejected_invalid"}
    assert "pending_confirmation" in statuses
