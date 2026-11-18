"""Cross-file validators shared by bootstrap (fail-fast) and catalog reloads (LKG)."""

from pathlib import Path

from assistant.core.capabilities.catalog import CapabilityCatalog
from assistant.core.capabilities.schemas import CapabilityDefinition, CapabilityToolBinding
from assistant.core.capabilities.validation import make_policy_validator, validate_policy_bindings
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


def _config(enabled: list[str], denied: list[str] | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="m", model_allowlist=["m"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=enabled, denied_capabilities=denied or []
        ),
        tools=ToolsConfig(
            tools=[
                ToolDefinition(tool_id="ok", entrypoint="builtins:len"),
                ToolDefinition(tool_id="off", entrypoint="builtins:len", enabled=False),
                ToolDefinition(tool_id="broken", entrypoint="no.such.module:fn"),
            ]
        ),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


def _cap(cap_id: str, *tools: str) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=cap_id, tools=[CapabilityToolBinding(tool_id=t) for t in tools]
    )


def _resolve(entrypoint: str) -> object:
    module, attr = entrypoint.split(":")
    return getattr(__import__(module), attr)


def _messages(config: RuntimeConfig, defs: dict[str, CapabilityDefinition]) -> list[str]:
    return [i.message for i in validate_policy_bindings(config, defs, _resolve) if i.blocking]


def test_valid_bindings_pass() -> None:
    assert _messages(_config(["a"]), {"a": _cap("a", "ok")}) == []


def test_unknown_disabled_and_unresolvable_tools_are_reported() -> None:
    msgs = _messages(_config(["a"]), {"a": _cap("a", "ghost", "off", "broken")})
    assert any("'ghost' which is not in tools.yaml" in m for m in msgs)
    assert any("'off' which is disabled in tools.yaml" in m for m in msgs)
    assert any("'broken'" in m and "failed to resolve" in m for m in msgs)


def test_only_the_active_set_is_checked() -> None:
    defs = {"a": _cap("a", "ok"), "b": _cap("b", "ghost")}
    assert _messages(_config(["a", "b"], denied=["b"]), defs) == []
    assert _messages(_config(["a"]), defs) == []


def test_missing_enabled_manifest_is_a_warning() -> None:
    issues = validate_policy_bindings(_config(["a", "gone"]), {"a": _cap("a", "ok")})
    assert [(i.kind, "gone" in i.message) for i in issues] == [("warning", True)]


def test_catalog_reload_rejects_bad_binding_and_allows_deleting_enabled(tmp_path: Path) -> None:
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "a.yaml").write_text("capability_id: a\ntools:\n  - tool_id: ok\n")
    (caps / "b.yaml").write_text("capability_id: b\n")
    catalog = CapabilityCatalog(
        tmp_path, min_refresh_interval=0, validator=make_policy_validator(_config(["a", "b"]))
    )
    (caps / "a.yaml").write_text("capability_id: a\ntools:\n  - tool_id: ghost\n")
    snap = catalog.refresh()
    assert snap.revision == 1
    assert "not in tools.yaml" in snap.blocking_errors[0].message

    (caps / "a.yaml").write_text("capability_id: a\ntools:\n  - tool_id: ok\n")
    (caps / "b.yaml").unlink()
    snap = catalog.refresh()
    assert snap.revision == 2  # deleting an enabled manifest is allowed at runtime...
    assert set(snap.definitions) == {"a"}
    assert [i.kind for i in snap.errors] == ["warning"]  # ...but reported
