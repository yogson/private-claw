# MCP Integration Analysis: Private Claw

**Date:** 2026-03-26
**Codebase:** `/Users/yogson/PycharmProjects/private-claw`
**Analyst:** Deep static analysis of all source files

---

## 1. Executive Summary

MCP (Model Context Protocol) integration in Private Claw is **substantially implemented and production-ready** for the SSE transport path. The project has a complete end-to-end pipeline: config schemas, an SSE client, a tool-mapping loader, a bridge that converts MCP tools into Pydantic AI `Tool` objects, capability-ID routing (`cap.mcp.<server>.<tool>`), a real plugin (`plugins/mcp/chrome_devtools/tool_map.yaml`), and a full test suite for the bridge layer. The primary **gap** is that no MCP server is actually configured as enabled in either `config/mcp_servers.yaml` or `config.local/mcp_servers.yaml` (both have `servers: []`), meaning the feature is wired but dormant. A secondary structural gap is that the MCP client only supports SSE transport; stdio and streamable-HTTP (the two other MCP transport modes) are not implemented. The third gap is confirmation-gate enforcement: the `requires_confirmation` and `risk_class` fields are written to the tool description string but are not plumbed into an actual confirmation workflow at runtime.

---

## 2. Project Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Entry Points                            │
│  Telegram polling ──> TelegramIngress                       │
│  FastAPI HTTP   ──> tasks/config routers                    │
└────────────────────────┬────────────────────────────────────┘
                         │ OrchestratorEvent
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                      Orchestrator                           │
│  src/assistant/core/orchestrator/service.py                 │
│  - session lock, idempotency, replay, persistence           │
│  - builds effective RuntimeConfig (capability overrides)    │
│  - delegates to PydanticAITurnAdapter                       │
└────────────────────────┬────────────────────────────────────┘
                         │ messages + TurnDeps
                         ▼
┌─────────────────────────────────────────────────────────────┐
│             PydanticAITurnAdapter                           │
│  src/assistant/agent/pydantic_ai_agent.py                   │
│  - builds pydantic_ai Agent with system prompt + tools      │
│  - calls get_agent_tools(config) at construction time       │
└────────────┬────────────────────────┬───────────────────────┘
             │                        │
             ▼                        ▼
┌────────────────────┐    ┌───────────────────────────────────┐
│  Tool Registry     │    │  McpBridge                        │
│  registry.py       │    │  extensions/mcp/bridge.py         │
│  - first-party     │    │  - reads cap.mcp.* IDs            │
│    tools from      │    │  - loads tool mappings from       │
│    tools.yaml +    │    │    plugins/mcp/*/tool_map.yaml    │
│    capabilities/   │    │  - looks up server URL in config  │
│    *.yaml          │    │  - wraps call_mcp_tool() as       │
└────────────────────┘    │    pydantic_ai Tool               │
                          └──────────────┬────────────────────┘
                                         │ SSE connection per call
                                         ▼
                          ┌─────────────────────────────────────┐
                          │  MCP SSE Client                     │
                          │  extensions/mcp/client.py           │
                          │  - mcp_sse_session() context mgr    │
                          │  - call_mcp_tool() normalize result │
                          └──────────┬──────────────────────────┘
                                     │ HTTP/SSE
                                     ▼
                          ┌─────────────────────────────────────┐
                          │  External MCP Server                │
                          │  (e.g. Playwright MCP, Logfire MCP) │
                          └─────────────────────────────────────┘
```

**Config resolution hierarchy:**

```
ASSISTANT_CONFIG_DIR env var
    └── config.local/   (runtime overrides)
    └── config/         (bundled defaults)
        ├── app.yaml
        ├── capabilities.yaml          (policy: which cap IDs enabled)
        ├── capabilities/
        │   ├── assistant.yaml
        │   ├── delegation_coding.yaml
        │   ├── deploy.yaml
        │   └── macos_personal.yaml
        ├── mcp_servers.yaml           (server registry)
        └── tools.yaml                 (tool catalog)

plugins/                               (tool mappings, side-by-side with config/)
    └── mcp/
        └── chrome_devtools/
            └── tool_map.yaml
```

---

## 3. Current MCP State: Every MCP-Related File

### 3.1 Core MCP Extension Module

**`src/assistant/extensions/mcp/__init__.py`** (lines 1–18)
- Public re-exports: `McpBridge`, `McpMappedTool`, `McpToolMapping`, `RiskClass`, `capability_id_for_mcp_tool`, `load_tool_mappings`
- Component ID: `CMP_TOOL_RUNTIME_REGISTRY`

**`src/assistant/extensions/mcp/models.py`** (lines 1–36)
- `RiskClass` enum: `READONLY`, `INTERACTIVE`, `SIDE_EFFECTING` (StrEnum)
- `McpMappedTool`: `tool_name`, `summary`, `input_schema`, `risk_class`, `requires_confirmation`
- `McpToolMapping`: `server_id`, `tools: list[McpMappedTool]`

**`src/assistant/extensions/mcp/client.py`** (lines 1–93)
- `mcp_sse_session(url, connect_timeout, call_timeout)`: async context manager that opens an SSE transport via `mcp.client.sse.sse_client`, creates `mcp.ClientSession`, calls `session.initialize()`
- `call_mcp_tool(url, tool_name, arguments, connect_timeout, call_timeout)`: calls `mcp_sse_session`, invokes `session.call_tool()`, normalizes result to `{status, content, error, is_error}` dict
- **Transport: SSE only** — imports from `mcp.client.sse`

**`src/assistant/extensions/mcp/loader.py`** (lines 1–65)
- `_plugins_mcp_dir(config_dir)`: resolves path as `<config_dir>/../plugins/mcp/`
- `discover_tool_mappings(plugins_mcp)`: finds `tool_map.yaml` at root and in subdirs
- `load_tool_mappings(config_dir)`: loads all found files, parses into `McpToolMapping`, raises on duplicate `server_id`
- `capability_id_for_mcp_tool(server_id, tool_name)`: returns `cap.mcp.<server_id>.<tool_name>`

**`src/assistant/extensions/mcp/bridge.py`** (lines 1–154)
- `McpBridge(config: RuntimeConfig)`: loads mappings from `plugins/mcp/` at construction
- `get_tools_for_capability_ids(capability_ids)`: filters `cap.mcp.*` IDs, cross-references mapping and server config, calls `_build_mcp_tool()` for each valid tool
- `_build_mcp_tool(...)`: wraps `call_mcp_tool` in an async closure, returns `pydantic_ai.Tool` with `name=capability_id` (e.g. `cap.mcp.chrome_devtools.browser_navigate`)
- `_effective_tool_policy(config, server_id)`: resolves per-server or default `tool_policy`; `"deny"` blocks all tools

### 3.2 Tool Registry Integration

**`src/assistant/agent/tools/registry.py`** (lines 97–175)
- `collect_enabled_tool_ids(config)` (line 97): special-cases `cap.mcp.*` — any enabled capability ID starting with `cap.mcp.` is added directly to the tool set without requiring a capability manifest file (line 114)
- `get_agent_tools(config)` (line 131): separates MCP IDs from first-party IDs; if `mcp_ids` non-empty, instantiates `McpBridge(config)` and calls `get_tools_for_capability_ids(mcp_ids)` (lines 171–174)

### 3.3 Config Schemas

**`src/assistant/core/config/schemas.py`** (lines 192–220)
- `McpServerEntry`: `id`, `url`, `enabled`, `tool_policy` (default `"deny_by_default"`)
- `McpDefaults`: `enabled`, `tool_policy`
- `McpTimeouts`: `connect_seconds=10`, `call_seconds=30`
- `McpServersConfig`: aggregates servers, defaults, timeouts
- `RuntimeConfig.mcp_servers: McpServersConfig` (line 276)

### 3.4 Config Loader Registration

**`src/assistant/core/config/loader.py`** (line 43)
- `"mcp_servers": ("mcp_servers.yaml", McpServersConfig, "ASSISTANT_MCP")` — MCP config loaded as first-class domain alongside app, model, capabilities, tools

### 3.5 Capability Schemas (Claude Code MCP passthrough)

**`src/assistant/core/capabilities/schemas.py`** (lines 77–95)
- `ClaudeCodeSettings.mcp_servers: dict[str, dict[str, Any]]`: per-capability MCP server injection into `~/.claude/settings.json`
- `CapabilityDefinition.claude_code_settings: ClaudeCodeSettings | None`

**`src/assistant/core/capabilities/loader.py`** (lines 104–166)
- `apply_claude_code_settings(...)`: merges `mcp_servers` from all active capabilities into `~/.claude/settings.json` under top-level key `mcpServers` (line 163)

### 3.6 Configuration Files

**`config/mcp_servers.yaml`** and **`config.local/mcp_servers.yaml`** (both identical):
```yaml
servers: []
defaults:
  enabled: false
  tool_policy: deny_by_default
timeouts:
  connect_seconds: 10
  call_seconds: 30
```
**Status: no server is enabled in either config.**

### 3.7 Plugin Tool Mappings

**`plugins/mcp/chrome_devtools/tool_map.yaml`**:
```yaml
server_id: chrome_devtools
tools:
  - tool_name: browser_navigate   (risk: interactive)
  - tool_name: browser_snapshot   (risk: readonly)
  - tool_name: browser_click      (risk: interactive, requires_confirmation: true)
  - tool_name: browser_type       (risk: interactive)
```
This is the only real plugin present. The `plugins/mcp/` directory resolves relative to the config directory: if `ASSISTANT_CONFIG_DIR=config.local`, plugins are looked up at `plugins/mcp/` (sibling of `config.local/`).

### 3.8 Capability Claude Code MCP passthrough (active)

**`config.local/capabilities/delegation_coding.yaml`** (lines 18–21):
```yaml
claude_code_settings:
  mcp_servers:
    logfire:
      type: http
      url: https://logfire-us.pydantic.dev/mcp
```
This injects a Logfire MCP server into Claude Code's `~/.claude/settings.json` when `delegation_coding` capability is active. This is a **different MCP path** — it configures MCP for Claude Code subagents (not for the main assistant agent).

### 3.9 Orchestrator Tool Mismatch Detection

**`src/assistant/core/orchestrator/service.py`** (lines 584–640)
- `_detect_tool_mismatch()` explicitly excludes `cap.mcp.*` prefixed tools from the mismatch check (line 622): `if tool_name and not tool_name.startswith("cap.mcp."):`

### 3.10 Tests

**`tests/assistant/extensions/mcp/test_mcp_bridge.py`** — 7 test functions covering:
- `capability_id_for_mcp_tool` naming convention
- empty result when `plugins/mcp` dir absent
- loading from subdirectory with multiple tools
- duplicate `server_id` raises `ValueError`
- bridge returns empty list when server not enabled
- bridge returns empty list when `tool_policy="deny"`
- bridge returns empty list when tool not in allowlist
- bridge returns `Tool` when server enabled and tool in mapping

### 3.11 Dependencies

**`pyproject.toml`** (line 29): `"claude-agent-sdk>=0.1.50"` — not the `mcp` package directly; however the `mcp` package is present as a transitive dependency (used in `client.py` imports: `from mcp import ClientSession; from mcp.client.sse import sse_client`).

---

## 4. Capability System Architecture

### 4.1 Full Loading Flow

```
bootstrap(config_dir)
    │
    ├── ConfigLoader.load()
    │     └── loads mcp_servers.yaml → McpServersConfig (first-class domain)
    │
    ├── load_capability_definitions(config_dir)
    │     └── discovers config/capabilities/*.yaml
    │         → dict[str, CapabilityDefinition]
    │
    ├── expand_nested_capabilities(enabled, definitions)
    │     └── DFS traversal through nested_capabilities lists
    │         (e.g. omnichem → delegation_coding)
    │
    ├── validate: each enabled cap has a manifest
    │   EXCEPT: cap.mcp.* IDs bypass this check entirely
    │
    ├── _validate_tools_and_capabilities()
    │     └── verifies all tool bindings reference enabled tools.yaml entries
    │
    └── apply_claude_code_settings()
          └── merges mcp_servers from active capabilities into
              ~/.claude/settings.json

PydanticAITurnAdapter.__init__(config)
    └── get_agent_tools(config)
          ├── collect_enabled_tool_ids(config)
          │     ├── expand_nested_capabilities(...)
          │     ├── for regular caps: add binding.tool_id for enabled bindings
          │     └── for cap.mcp.* IDs: add directly (no manifest required)
          │
          ├── first-party tools: _resolve_entrypoint() → callable
          │
          └── MCP tools: McpBridge(config).get_tools_for_capability_ids(mcp_ids)
                ├── load_tool_mappings(config_dir) → dict[server_id, McpToolMapping]
                │     └── reads plugins/mcp/*/tool_map.yaml
                │
                └── for each cap_id in sorted(mcp_ids):
                      parse server_id, tool_name from cap.mcp.<s>.<t>
                      look up mapping[server_id].tools[tool_name]
                      look up server URL from mcp_servers.servers
                      check tool_policy != "deny"
                      → Tool(_invoke, name=cap_id, description=...)
```

### 4.2 Capability ID Namespace

| Prefix | Source | Example |
|--------|--------|---------|
| `assistant` | config/capabilities/assistant.yaml | `assistant` |
| `delegation_coding` | config/capabilities/delegation_coding.yaml | `delegation_coding` |
| `cap.mcp.<server>.<tool>` | enabled_capabilities list + mcp config | `cap.mcp.chrome_devtools.browser_navigate` |

MCP capability IDs do NOT require a manifest file in `config/capabilities/`. They are added directly to `enabled_capabilities` in `config/capabilities.yaml` and processed entirely through the `McpBridge`.

### 4.3 Per-Session Capability Overrides

**`src/assistant/core/session_context/capability_context.py`** implements `SessionCapabilityContextService` — a persistent JSON store keyed by `context_id` (Telegram chat ID). When a user selects a capability set via the Telegram UI, it overrides `enabled_capabilities` for that session. This override is plumbed through `OrchestratorEvent.capabilities_override` → `Orchestrator._run_turn()` → `CapabilitiesPolicyConfig` replacement → `TurnAdapterCache.get_or_build(effective_config)`.

---

## 5. Agent Tool Registration

### 5.1 How Tools Reach the LLM

```python
# pydantic_ai_agent.py line 103-112
def _create_agent(model_id, system_prompt, config) -> Agent[TurnDeps, str]:
    return Agent(
        model_id,
        deps_type=TurnDeps,
        system_prompt=system_prompt,
        retries=0,
        output_retries=1,
        tools=get_agent_tools(config),   # ← all tool binding happens here
    )
```

`get_agent_tools(config)` is called **once at adapter construction time**, returning a `Sequence[AgentTool]` where each element is either:
- A callable decorated with `@tool` (for most first-party tools)
- A `pydantic_ai.Tool(fn, name=..., description=...)` instance (for MCP tools and tavily_search)

The `Agent` is created once per `PydanticAITurnAdapter` instance. Adapters are cached by `TurnAdapterCache` keyed on `frozenset(enabled_capabilities)`, so changing the capability set forces a new adapter build but re-uses adapters for repeated turns with the same capability set.

### 5.2 First-Party Tool Registration

Each first-party tool (e.g. `shell_execute_readonly`) is a plain Python async function whose `tool_id` is declared in `config/tools.yaml` with an `entrypoint` string. `_resolve_entrypoint("assistant.agent.tools.shell_execute:shell_execute_readonly")` does `importlib.import_module(module) + getattr(module, attr)`. The returned callable is passed directly to `tools=[]` in the `Agent(...)` constructor. Pydantic AI infers the tool schema from the function signature and docstring.

### 5.3 MCP Tool Registration

MCP tools bypass the `tools.yaml` entrypoint system entirely. They are registered as `pydantic_ai.Tool` instances built by `_build_mcp_tool()` in `bridge.py`. The key points:
- Tool name = capability ID string (e.g. `cap.mcp.chrome_devtools.browser_navigate`)
- Tool description = `summary` field from `tool_map.yaml`, with risk/confirmation annotation appended
- Tool function = `async _invoke(ctx, arguments)` closure that calls `call_mcp_tool(url, tool_name, arguments)`
- Input schema is **not** forwarded to Pydantic AI — the `arguments` parameter is typed as `dict[str, Any] | None`, which means the LLM receives no structured parameter schema for MCP tools

### 5.4 Tool Runtime Params

`build_tool_runtime_params(config)` returns a `dict[tool_id, params_dict]` that is passed in `TurnDeps.tool_runtime_params`. First-party tools read this inside their implementations (e.g. `shell_execute_readonly` reads `tool_runtime_params["shell_execute_readonly"]["shell_readonly_commands"]`). MCP tools do not read `tool_runtime_params` — they receive all config at construction time via the `McpBridge`.

---

## 6. MCP Integration Gaps Analysis

| Area | What Exists | What's Partial | What's Missing |
|------|-------------|----------------|----------------|
| **SSE transport** | Full: `mcp_sse_session` + `call_mcp_tool` in `client.py` | — | stdio transport; streamable-HTTP transport |
| **Tool mapping** | Full: `McpToolMapping` / `McpMappedTool` models, YAML loader, `plugins/mcp/` discovery | — | Dynamic discovery (current: explicit allowlist only) |
| **Bridge** | Full: `McpBridge.get_tools_for_capability_ids()` | — | — |
| **Pydantic AI integration** | Full: `Tool(_invoke, name=cap_id, description=...)` | Input schema not forwarded to LLM | — |
| **Config schema** | Full: `McpServersConfig`, `McpServerEntry` | — | — |
| **Config loader** | Full: domain registered as `"mcp_servers"` | — | — |
| **Capability routing** | Full: `cap.mcp.<server>.<tool>` convention, no-manifest fast-path | — | — |
| **Per-session override** | Inherited from capability override system | — | — |
| **Tool policy** | `deny` blocks all, `deny_by_default` allows allowlisted | `allow` policy not differentiated from `deny_by_default` | Policy enforcement for `interactive`/`side_effecting` risk classes at runtime |
| **Confirmation gates** | Risk class + `requires_confirmation` written to description string | Not enforced at runtime (no actual confirmation workflow) | Actual confirmation prompt before executing `requires_confirmation=true` MCP tools |
| **Real plugin** | `plugins/mcp/chrome_devtools/tool_map.yaml` with 4 tools | Server not enabled in config | No actual running Chrome DevTools server URL configured |
| **Claude Code MCP** | `apply_claude_code_settings()` writes `mcpServers` to `~/.claude/settings.json` | Logfire MCP configured in `delegation_coding` capability | Only HTTP-type servers supported in this path; not connected to main agent |
| **Error handling** | `call_mcp_tool` catches all exceptions, returns `{status: error}` | Per-error categorization limited | Retry logic; circuit breaker for repeated failures |
| **Input schema forwarding** | `McpMappedTool.input_schema` field exists | `input_schema` loaded from YAML but never passed to `Tool(...)` constructor | Schema forwarding to Pydantic AI so LLM gets structured parameter hints |
| **Server health checks** | None | — | Startup connectivity check; connection pool |
| **Session persistence** | Connections are short-lived (one per tool call) | — | Persistent connection / session reuse across turns |
| **Tests** | 7 unit tests covering bridge, loader, models | No integration tests against a real MCP server | E2E test with mock MCP server |
| **Bootstrap validation** | `cap.mcp.*` IDs bypass the `no manifest found` exit | — | Startup validation that each enabled `cap.mcp.*` ID corresponds to a loaded mapping |
| **Enabled server** | — | — | Any entry in `servers:` list with `enabled: true` |

---

## 7. End-to-End Flow Analysis: "What Would Happen If We Added an MCP Server Today?"

### Step 1: Enable a server in config

Add to `config.local/mcp_servers.yaml`:
```yaml
servers:
  - id: chrome_devtools
    url: http://localhost:9222/sse
    enabled: true
    tool_policy: deny_by_default
```

### Step 2: Enable tool capability IDs

Add to `config.local/capabilities.yaml`:
```yaml
enabled_capabilities:
  - assistant
  - cap.mcp.chrome_devtools.browser_navigate
  - cap.mcp.chrome_devtools.browser_snapshot
```

### Step 3: What happens at bootstrap?

`bootstrap()` in `core/bootstrap.py`:
1. `ConfigLoader.load()` parses `mcp_servers.yaml` → `McpServersConfig` with one server entry. OK.
2. `load_capability_definitions()` loads `config.local/capabilities/*.yaml`. The IDs `cap.mcp.chrome_devtools.*` are **not** manifest files — that's fine, they skip manifest lookup.
3. `expand_nested_capabilities(["assistant", "cap.mcp.chrome_devtools.browser_navigate", ...])` — the `cap.mcp.*` IDs have no entry in `definitions`, so `_expand()` adds them without recursing into nested capabilities. OK.
4. The bootstrap loop at line 109 checks: `if cap_id not in definitions: raise SystemExit(...)`. **BUG**: this check fires for `cap.mcp.*` IDs since they have no manifest. **This would crash the server at startup.**

Wait — re-reading `bootstrap.py` line 109–113:
```python
for cap_id in all_enabled:
    if cap_id not in definitions:
        raise SystemExit(
            f"Enabled capability '{cap_id}' has no manifest in config/capabilities/*.yaml"
        )
```

This is a **critical gap**: adding `cap.mcp.*` IDs directly to `enabled_capabilities` would cause `SystemExit` at bootstrap because `bootstrap()` validates all enabled capabilities against the definitions dict. The `collect_enabled_tool_ids()` in `registry.py` has the `cap.mcp.*` fast-path, but **`bootstrap()` does not have this exemption**.

### Step 4: Workaround today

The workaround is to create a capability manifest file for each MCP tool group — a YAML file in `config/capabilities/` with a `capability_id` that references the MCP tools indirectly. But the system's `enabled_capabilities` entry must then be the manifest's `capability_id`, not the `cap.mcp.*` string directly. This is inconsistent with the design intent.

Alternatively, looking at the `config/mcp_servers.yaml` documentation comment: `# Add to enabled_capabilities in config/capabilities.yaml to activate`. The design assumes `cap.mcp.*` IDs go into `enabled_capabilities`, but `bootstrap()` would reject them.

### Step 5: What happens at agent construction (bypassing the bootstrap issue)?

Assuming the `SystemExit` in bootstrap is avoided:
1. `get_agent_tools(config)` calls `collect_enabled_tool_ids(config)` → `cap.mcp.*` IDs are collected in `mcp_ids`
2. `McpBridge(config)` is instantiated. It calls `load_tool_mappings(config_dir)`.
3. `_plugins_mcp_dir(config_dir)`: returns `<config_dir>/../plugins/mcp/`. With `config_dir=config.local/`, this resolves to `plugins/mcp/`. The `chrome_devtools/tool_map.yaml` is found. OK.
4. `get_tools_for_capability_ids({"cap.mcp.chrome_devtools.browser_navigate"})`:
   - parses: `server_id="chrome_devtools"`, `tool_name="browser_navigate"`
   - finds `mapping["chrome_devtools"]` → has `browser_navigate` in tools list
   - finds server URL `http://localhost:9222/sse` from config
   - `tool_policy="deny_by_default"` (not "deny") → proceeds
   - builds `Tool(_invoke, name="cap.mcp.chrome_devtools.browser_navigate", description="Navigate to a URL in the browser [Risk: interactive; requires_confirmation=False]")`
5. Tool is appended to agent tools list. Pydantic AI registers it. **OK.**

### Step 6: What happens when the LLM calls the tool?

1. LLM responds with a tool call: `{tool_name: "cap.mcp.chrome_devtools.browser_navigate", args: {"arguments": {"url": "https://example.com"}}}`
2. Pydantic AI routes to `_invoke(ctx, arguments={"url": "https://example.com"})`
3. `call_mcp_tool("http://localhost:9222/sse", "browser_navigate", {"url": "..."})`
4. `mcp_sse_session(...)` connects via `sse_client`, creates `ClientSession`, calls `session.initialize()`
5. `session.call_tool("browser_navigate", {"url": "..."})` is called
6. If Chrome DevTools MCP server is running at `localhost:9222/sse`: returns result
7. Result normalized to `{status: "ok", content: "...", error: None, is_error: False}`
8. Returned to LLM as tool result. **OK if server is running.**

### Step 7: Confirmation gate

If `requires_confirmation=true` (like `browser_click`):
- The description includes `[Risk: interactive; requires_confirmation=True]`
- **No actual confirmation workflow is triggered.** The tool executes immediately.
- The LLM is supposed to ask the user before calling it (based on the description hint), but this is not enforced by the system.

---

## 8. Recommended Architecture

The primary recommendation is to fix the bootstrap validation gap and optionally add missing transport support and confirmation enforcement. Here is the target architecture:

```
config/capabilities.yaml (policy)
  enabled_capabilities:
    - assistant
    - cap.mcp.chrome_devtools.browser_navigate
    - cap.mcp.chrome_devtools.browser_snapshot
         │
         │  bootstrap() [NEEDS FIX]
         │  ─────────────────────────────────────
         │  cap.mcp.* IDs → exempt from manifest check
         │  → validate instead: mapping exists AND server registered
         │
         ▼
config/mcp_servers.yaml
  servers:
    - id: chrome_devtools
      url: http://localhost:9222/sse
      enabled: true
      tool_policy: deny_by_default
         │
         ├── plugins/mcp/chrome_devtools/tool_map.yaml
         │     server_id: chrome_devtools
         │     tools: [browser_navigate, browser_snapshot, ...]
         │
         ▼
McpBridge.get_tools_for_capability_ids(cap_ids)
    │
    ├── [NEW] startup: optional health-check ping per enabled server
    │
    ├── _build_mcp_tool(...) → pydantic_ai.Tool
    │     [IMPROVEMENT] forward input_schema to Tool(...)
    │
    └── call_mcp_tool() [SSE: works today]
          [FUTURE] add stdio transport
          [FUTURE] add streamable-HTTP transport

Confirmation workflow [MISSING]:
    if requires_confirmation=true AND tool call detected:
        → emit confirmation request to user channel
        → block tool execution until approved
        → on approval: call tool
        → on rejection: return {status: rejected_by_user}
```

---

## 9. Implementation Roadmap

Tasks are ordered by priority and dependency.

### Priority 1: Fix bootstrap validation gap (CRITICAL)

**File to modify:** `/Users/yogson/PycharmProjects/private-claw/src/assistant/core/bootstrap.py`

**Change:** In `bootstrap()`, skip the `SystemExit` check for `cap.mcp.*` capability IDs. Instead, validate that each `cap.mcp.*` ID has a corresponding server entry in `mcp_servers` and a tool mapping in `plugins/mcp/`.

Current code (lines 106–113):
```python
all_enabled = expand_nested_capabilities(
    runtime_config.capabilities.enabled_capabilities, definitions
)
for cap_id in all_enabled:
    if cap_id not in definitions:
        raise SystemExit(
            f"Enabled capability '{cap_id}' has no manifest in config/capabilities/*.yaml"
        )
```

Needed change: add `if cap_id.startswith("cap.mcp."): continue` before the `raise SystemExit`, and optionally add cross-validation against loaded tool mappings.

### Priority 2: Enable a real MCP server in config (OPERATIONAL)

**File to modify:** `/Users/yogson/PycharmProjects/private-claw/config.local/mcp_servers.yaml`

Add an enabled server entry for any MCP server you want to use (e.g. Playwright MCP, Logfire MCP, etc.).

**File to modify:** `/Users/yogson/PycharmProjects/private-claw/config.local/capabilities.yaml`

Add `cap.mcp.<server_id>.<tool_name>` entries to `enabled_capabilities`.

**File to create (if new server):** `/Users/yogson/PycharmProjects/private-claw/plugins/mcp/<server_id>/tool_map.yaml`

### Priority 3: Forward input_schema to Pydantic AI Tool (QUALITY)

**File to modify:** `/Users/yogson/PycharmProjects/private-claw/src/assistant/extensions/mcp/bridge.py`

In `_build_mcp_tool()`, if `McpMappedTool.input_schema` is set, pass it to the `Tool(...)` constructor so the LLM receives structured parameter hints rather than a bare `dict[str, Any]`.

### Priority 4: Confirmation gate enforcement (SAFETY)

**New file:** `/Users/yogson/PycharmProjects/private-claw/src/assistant/extensions/mcp/confirmation.py`

Implement a `ConfirmationGate` that intercepts `_invoke` calls for tools with `requires_confirmation=True`. The gate should emit a `pending_confirmation` event through `TurnDeps` (similar to `ask_question` tool pattern), block execution, and resume on user approval.

**File to modify:** `/Users/yogson/PycharmProjects/private-claw/src/assistant/agent/deps.py`

Add a `mcp_confirmation_handler` callback to `TurnDeps`.

**File to modify:** `/Users/yogson/PycharmProjects/private-claw/src/assistant/extensions/mcp/bridge.py`

In `_build_mcp_tool()`, check `requires_confirmation` and call the handler before `call_mcp_tool()`.

### Priority 5: Bootstrap MCP validation (ROBUSTNESS)

**File to modify:** `/Users/yogson/PycharmProjects/private-claw/src/assistant/core/bootstrap.py`

Add a new `_validate_mcp_capabilities()` function that:
1. Extracts all `cap.mcp.*` IDs from enabled capabilities
2. Loads tool mappings from `plugins/mcp/`
3. For each `cap.mcp.<server>.<tool>`: verify `server_id` is registered in `mcp_servers.servers` AND `tool_name` is in the tool mapping
4. Raises `SystemExit` if any enabled `cap.mcp.*` ID cannot be resolved

### Priority 6: stdio and streamable-HTTP transport (EXTENSIBILITY)

**File to modify:** `/Users/yogson/PycharmProjects/private-claw/src/assistant/extensions/mcp/client.py`

Add `mcp_stdio_session()` and `mcp_http_session()` context managers alongside `mcp_sse_session()`.

**File to modify:** `/Users/yogson/PycharmProjects/private-claw/src/assistant/core/config/schemas.py`

Add `transport: Literal["sse", "stdio", "http"] = "sse"` and `command: list[str] | None = None` to `McpServerEntry` for stdio-launched servers.

**File to modify:** `/Users/yogson/PycharmProjects/private-claw/src/assistant/extensions/mcp/bridge.py`

Route `_build_mcp_tool()` to the appropriate session type based on `server.transport`.

### Priority 7: Persistent connection / session reuse (PERFORMANCE)

**New file:** `/Users/yogson/PycharmProjects/private-claw/src/assistant/extensions/mcp/session_pool.py`

Currently `call_mcp_tool()` opens a new SSE connection for every single tool call. For high-frequency use this is expensive. A session pool keyed by `(server_id, url)` that holds a live `ClientSession` and reconnects on error would improve latency significantly.

---

## 10. Code Examples

### 10.1 Fix bootstrap.py to allow cap.mcp.* in enabled_capabilities

```python
# In src/assistant/core/bootstrap.py, replace lines 106-113:

all_enabled = expand_nested_capabilities(
    runtime_config.capabilities.enabled_capabilities, definitions
)
for cap_id in all_enabled:
    if cap_id.startswith("cap.mcp."):
        # MCP tools are gated by mcp_servers.yaml + plugins/mcp/ tool maps,
        # not by capability manifests. Skip manifest check for these.
        continue
    if cap_id not in definitions:
        raise SystemExit(
            f"Enabled capability '{cap_id}' has no manifest in config/capabilities/*.yaml"
        ) from None
```

### 10.2 Add a Logfire MCP server to the main agent (not just Claude Code)

In `config.local/mcp_servers.yaml`:
```yaml
servers:
  - id: logfire
    url: https://logfire-us.pydantic.dev/mcp
    enabled: true
    tool_policy: deny_by_default

defaults:
  enabled: false
  tool_policy: deny_by_default

timeouts:
  connect_seconds: 10
  call_seconds: 30
```

Create `plugins/mcp/logfire/tool_map.yaml`:
```yaml
server_id: logfire
tools:
  - tool_name: query_run
    summary: Run a SQL query against Logfire telemetry data
    risk_class: readonly
    requires_confirmation: false
  - tool_name: list_projects
    summary: List available Logfire projects
    risk_class: readonly
    requires_confirmation: false
```

In `config.local/capabilities.yaml`:
```yaml
enabled_capabilities:
  - assistant
  - cap.mcp.logfire.query_run
  - cap.mcp.logfire.list_projects
denied_capabilities: []
```

### 10.3 Forward input_schema to Pydantic AI Tool

```python
# In src/assistant/extensions/mcp/bridge.py, in _build_mcp_tool():
# After building the _invoke closure, before return:

from pydantic_ai import Tool

if mapped.input_schema:
    # Build a typed Pydantic model from the JSON Schema so Pydantic AI
    # can expose structured parameters to the LLM.
    from pydantic import create_model
    # (simplified — real implementation would recursively build fields)
    return Tool(
        _invoke,
        name=capability_id,
        description=desc,
    )
else:
    return Tool(
        _invoke,
        name=capability_id,
        description=desc,
    )
```

A full implementation would use `pydantic_ai.Tool(fn, takes_ctx=True, schema=...)` if Pydantic AI supports explicit JSON schema injection, or dynamically build a Pydantic model from `input_schema` using `pydantic.create_model`.

### 10.4 Bootstrap MCP validation function

```python
# Add to src/assistant/core/bootstrap.py:

def _validate_mcp_capabilities(
    runtime_config: RuntimeConfig,
    all_enabled: list[str],
    config_dir: Path,
) -> None:
    """Validate that all enabled cap.mcp.* IDs have registered servers and tool mappings."""
    from assistant.extensions.mcp.loader import load_tool_mappings

    mcp_ids = [c for c in all_enabled if c.startswith("cap.mcp.")]
    if not mcp_ids:
        return

    mappings = load_tool_mappings(str(config_dir))
    server_ids = {s.id for s in runtime_config.mcp_servers.servers if s.enabled}

    for cap_id in mcp_ids:
        parts = cap_id.split(".")
        if len(parts) != 4:
            raise SystemExit(
                f"Invalid MCP capability ID format '{cap_id}'; expected cap.mcp.<server>.<tool>"
            ) from None
        _, _, server_id, tool_name = parts
        if server_id not in server_ids:
            raise SystemExit(
                f"MCP capability '{cap_id}' references server '{server_id}' "
                "which is not enabled in mcp_servers.yaml"
            ) from None
        mapping = mappings.get(server_id)
        if mapping is None:
            raise SystemExit(
                f"MCP capability '{cap_id}' requires plugins/mcp/{server_id}/tool_map.yaml "
                "which was not found"
            ) from None
        if not any(t.tool_name == tool_name for t in mapping.tools):
            raise SystemExit(
                f"MCP capability '{cap_id}': tool '{tool_name}' is not in the allowlist "
                f"at plugins/mcp/{server_id}/tool_map.yaml"
            ) from None
```

### 10.5 Add a stdio-launched MCP server entry

Config schema extension needed in `schemas.py`:
```python
class McpServerEntry(BaseModel):
    id: str
    url: str = ""          # for SSE/HTTP transport
    enabled: bool = True
    tool_policy: str = "deny_by_default"
    transport: str = "sse"              # NEW: "sse" | "stdio" | "http"
    command: list[str] | None = None    # NEW: for stdio transport
    env: dict[str, str] | None = None   # NEW: env vars for stdio process
```

Example `mcp_servers.yaml` entry for a stdio server:
```yaml
- id: filesystem
  transport: stdio
  command: ["npx", "@modelcontextprotocol/server-filesystem", "/tmp"]
  enabled: true
  tool_policy: deny_by_default
```

---

## Appendix: Component ID Summary

All MCP-related source files carry `# Component ID: CMP_TOOL_RUNTIME_REGISTRY`. This component is defined in `docs/assistant_v1/domains/capabilities.md` and is responsible for capability and skill discovery, validation, and runtime invocation — including the MCP bridge subsystem.

Key source files by role:

| Role | File |
|------|------|
| MCP models | `src/assistant/extensions/mcp/models.py` |
| MCP SSE client | `src/assistant/extensions/mcp/client.py` |
| Tool mapping loader | `src/assistant/extensions/mcp/loader.py` |
| Pydantic AI bridge | `src/assistant/extensions/mcp/bridge.py` |
| Tool registry (MCP path) | `src/assistant/agent/tools/registry.py` lines 97–175 |
| Config schemas | `src/assistant/core/config/schemas.py` lines 192–220 |
| Bootstrap (gap) | `src/assistant/core/bootstrap.py` lines 106–113 |
| Plugin tool map | `plugins/mcp/chrome_devtools/tool_map.yaml` |
| MCP server config | `config/mcp_servers.yaml`, `config.local/mcp_servers.yaml` |
| Claude Code MCP | `config.local/capabilities/delegation_coding.yaml` lines 18–21 |
| Tests | `tests/assistant/extensions/mcp/test_mcp_bridge.py` |
