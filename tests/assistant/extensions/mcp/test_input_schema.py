"""Tests for input_schema forwarding to Pydantic AI tools (P3)."""

from pathlib import Path

from assistant.core.config.schemas import (
    McpDefaults,
    McpServerEntry,
    McpServersConfig,
    McpTimeouts,
    RuntimeConfig,
)
from assistant.extensions.mcp.bridge import McpBridge, _build_input_model


def test_build_input_model_returns_none_for_empty_schema() -> None:
    assert _build_input_model("cap.mcp.test.tool", None) is None
    assert _build_input_model("cap.mcp.test.tool", {}) is None


def test_build_input_model_returns_none_for_no_properties() -> None:
    assert _build_input_model("cap.mcp.test.tool", {"type": "object"}) is None


def test_build_input_model_creates_model_with_fields() -> None:
    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to navigate to"},
            "timeout": {"type": "integer", "description": "Timeout in seconds"},
        },
        "required": ["url"],
    }
    model = _build_input_model("cap.mcp.test.navigate", schema)
    assert model is not None
    assert "url" in model.__annotations__
    assert "timeout" in model.__annotations__
    # Check model name is sanitised
    assert "cap_mcp_test_navigate" in model.__name__


def test_build_input_model_required_vs_optional() -> None:
    schema = {
        "type": "object",
        "properties": {
            "required_field": {"type": "string"},
            "optional_field": {"type": "string", "default": "hello"},
        },
        "required": ["required_field"],
    }
    model = _build_input_model("cap.mcp.test.tool", schema)
    assert model is not None
    fields = model.model_fields
    assert fields["required_field"].is_required()
    assert not fields["optional_field"].is_required()


def test_bridge_builds_tool_with_input_schema(tmp_path: Path) -> None:
    """Tool with input_schema should produce typed tool (not generic dict)."""
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        MemoryConfig,
        ModelConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolsConfig,
    )

    # Set up plugin with input_schema
    plugins_mcp = tmp_path / "plugins" / "mcp" / "test_server"
    plugins_mcp.mkdir(parents=True)
    (plugins_mcp / "tool_map.yaml").write_text(
        """
server_id: test_server
tools:
  - tool_name: do_thing
    summary: Do a thing
    risk_class: readonly
    input_schema:
      type: object
      properties:
        name:
          type: string
          description: The name
        count:
          type: integer
          description: How many
      required:
        - name
"""
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    config = RuntimeConfig(
        app=AppConfig(data_root=str(tmp_path), timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(
            default_model_id="claude-3-5-haiku", model_allowlist=["claude-3-5-haiku"]
        ),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=["assistant"]),
        tools=ToolsConfig(tools=[]),
        mcp_servers=McpServersConfig(
            servers=[
                McpServerEntry(
                    id="test_server",
                    url="http://localhost:9999/sse",
                    enabled=True,
                    tool_policy="deny_by_default",
                )
            ],
            defaults=McpDefaults(),
            timeouts=McpTimeouts(),
        ),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
        config_dir=config_dir,
    )
    bridge = McpBridge(config)
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.test_server.do_thing"})
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "cap_mcp_test_server_do_thing"
    assert "Do a thing" in (tool.description or "")
