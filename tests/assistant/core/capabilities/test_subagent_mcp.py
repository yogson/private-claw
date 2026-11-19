"""Per-run sub-agent MCP servers built from an explicit capability set."""

from typing import Any

from assistant.core.capabilities.schemas import CapabilityDefinition, ClaudeCodeSettings
from assistant.core.capabilities.subagent_mcp import (
    McpToolFilter,
    SubagentMcp,
    build_subagent_mcp,
    claude_code_spec,
    resolve_run_mcp,
)
from assistant.core.config.schemas import McpServerEntry, McpServersConfig


def _defs(*definitions: CapabilityDefinition) -> dict[str, CapabilityDefinition]:
    return {d.capability_id: d for d in definitions}


MCP = McpServersConfig(
    servers=[
        McpServerEntry(
            id="opgraph",
            transport="streamable-http",
            url="https://og.example/mcp",
            headers={"Authorization": "Bearer x"},
        ),
        McpServerEntry(
            id="finance",
            transport="stdio",
            command="/bin/fin",
            args=["serve"],
            env={"K": "v"},
            include_tools=["rate", "convert"],
            confirm_tools=["convert"],
        ),
        McpServerEntry(id="events", transport="sse", url="https://ev.example/sse"),
        McpServerEntry(id="off", transport="sse", url="https://off.example", enabled=False),
    ]
)


class TestClaudeCodeSpec:
    def test_transports(self) -> None:
        by_id = {e.id: e for e in MCP.servers}
        assert claude_code_spec(by_id["opgraph"]) == {
            "type": "http",
            "url": "https://og.example/mcp",
            "headers": {"Authorization": "Bearer x"},
        }
        assert claude_code_spec(by_id["finance"]) == {
            "type": "stdio",
            "command": "/bin/fin",
            "args": ["serve"],
            "env": {"K": "v"},
        }
        assert claude_code_spec(by_id["events"]) == {"type": "sse", "url": "https://ev.example/sse"}


class TestBuild:
    def test_both_server_kinds_with_nested_capabilities(self) -> None:
        definitions = _defs(
            CapabilityDefinition(
                capability_id="opergraphs",
                mcp_servers=["opgraph"],
                nested_capabilities=["coding"],
            ),
            CapabilityDefinition(
                capability_id="coding",
                claude_code_settings=ClaudeCodeSettings(
                    mcp_servers={"logfire": {"type": "http", "url": "https://lf"}}
                ),
            ),
        )
        mcp = build_subagent_mcp(["opergraphs"], definitions, MCP)
        assert set(mcp.servers) == {"opgraph", "logfire"}
        assert mcp.servers["logfire"] == {"type": "http", "url": "https://lf"}
        assert mcp.tool_filters == {}

    def test_tool_restrictions_carry_over(self) -> None:
        definitions = _defs(CapabilityDefinition(capability_id="fin", mcp_servers=["finance"]))
        mcp = build_subagent_mcp(["fin"], definitions, MCP)
        assert mcp.tool_filters["finance"] == McpToolFilter(
            include=frozenset({"rate", "convert"}), deny=frozenset({"convert"})
        )
        assert mcp.denial_reason("mcp__finance__rate") is None
        assert mcp.denial_reason("mcp__finance__convert") is not None  # needs confirmation
        assert mcp.denial_reason("mcp__finance__other") is not None  # not included
        assert mcp.denial_reason("Bash") is None
        assert mcp.denial_reason("mcp__unrelated__x") is None

    def test_unknown_disabled_and_missing_are_skipped(self) -> None:
        definitions = _defs(
            CapabilityDefinition(capability_id="c", mcp_servers=["ghost", "off", "events"])
        )
        mcp = build_subagent_mcp(["c", "no_manifest"], definitions, MCP)
        assert set(mcp.servers) == {"events"}

    def test_empty_capability_list_gives_no_servers(self) -> None:
        assert build_subagent_mcp([], {}, MCP) == SubagentMcp()


class TestResolveRunMcp:
    DEFAULT: dict[str, dict[str, Any]] = {"global": {"type": "http", "url": "https://g"}}

    def test_default_servers_when_run_names_no_capabilities(self) -> None:
        mcp = resolve_run_mcp({}, self.DEFAULT, None)
        assert mcp.servers == self.DEFAULT

    def test_named_capabilities_replace_the_default(self) -> None:
        calls: list[list[str]] = []

        def _resolver(ids: Any) -> SubagentMcp:
            calls.append(list(ids))
            return SubagentMcp(servers={"own": {"type": "sse", "url": "https://o"}})

        mcp = resolve_run_mcp({"mcp_capabilities": ["a"]}, self.DEFAULT, _resolver)
        assert set(mcp.servers) == {"own"}
        assert calls == [["a"]]

    def test_empty_list_means_no_servers(self) -> None:
        mcp = resolve_run_mcp({"mcp_capabilities": []}, self.DEFAULT, lambda ids: SubagentMcp())
        assert mcp.servers == {}

    def test_no_resolver_gives_no_servers(self) -> None:
        assert resolve_run_mcp({"mcp_capabilities": ["a"]}, self.DEFAULT, None) == SubagentMcp()
