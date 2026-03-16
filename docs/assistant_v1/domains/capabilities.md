# Capabilities Domain

## Migration (v1 Refactor)

**Breaking change**: Capabilities are now defined in `config/capabilities/*.yaml` (prompt + toolset), not in `plugins/capabilities/`. Tools are configured in `config/tools.yaml`.

- **Old**: `config/capabilities.yaml` had `allowed_capabilities`, `shell_readonly_commands`, `command_allowlist`.
- **New**: `config/capabilities.yaml` has `enabled_capabilities`, `denied_capabilities` only. Tool params (e.g. `shell_readonly_commands`) live in `config/tools.yaml` and can be overridden per capability in `config/capabilities/<id>.yaml`.
- **Removed**: `plugins/capabilities/` directory and plugin manifest loading at startup.

## Purpose

Define the manifest-driven capability and skill ecosystem for v1, including discovery, policy enforcement, and safe runtime invocation.

## Owned Components

- `CMP_TOOL_RUNTIME_REGISTRY`
- `CMP_SKILL_RUNTIME_ENGINE`

## Scope

- Discover capability and skill manifests at startup.
- Validate manifest schema and dependency constraints.
- Register and route runtime invocations.
- Enforce capability permissions and command allowlists.
- Expose model-visible tool descriptors and execute provider-native tool calls.
- Support first-party capabilities in v1:
  - macOS personal integration (Notes, Reminders, Calendar),
  - GitHub CLI (`gh`) integration,
  - web search and content fetch,
  - Telegram voice transcript extraction capability from channel metadata,
  - memory management operations (including `memory_propose_update` proposal tool).

## Canonical Models

| Model ID | File Pattern | Required Fields | Purpose |
|---|---|---|---|
| `CMP_DATA_MODEL_TOOL_DEFINITION` | `config/tools.yaml` | `tool_id`, `entrypoint`, `enabled`, `default_params` | Tool registration and global defaults |
| `CMP_DATA_MODEL_CAPABILITY_DEFINITION` | `config/capabilities/*.yaml` | `capability_id`, `prompt`, `tools` | Capability = prompt + toolset + per-tool overrides |
| `CMP_DATA_MODEL_CAPABILITY_POLICY` | `config/capabilities.yaml` | `enabled_capabilities`, `denied_capabilities` | Operator policy: which capability manifests are active |
| `CMP_DATA_MODEL_SKILL_MANIFEST` | `plugins/skills/*/manifest.yaml` | `skill_id`, `version`, `entrypoint`, `required_capabilities`, `capabilities` | Skill registration and dependencies |
| `CMP_DATA_MODEL_MCP_SERVER_REGISTRY` | `config/mcp_servers.yaml` | `server_id`, `transport`, `enabled`, `connection` | External MCP server registration and connection policy |
| `CMP_DATA_MODEL_MCP_TOOL_MAPPING` | `plugins/mcp/*/tool_map.yaml` | `server_id`, `tools`, `capability_id_pattern`, `risk_class` | MCP tool-to-capability mapping and descriptor synthesis |

Tool definition schema (`config/tools.yaml`):
- `tool_id`: stable identifier (e.g. `shell_execute_readonly`, `memory_search`).
- `entrypoint`: `<python_module>:<callable_name>` format.
- `enabled`: bool, default true. Acts as a global kill-switch: if false, the tool is never exposed regardless of capability bindings.
- `default_params`: optional shell_readonly_commands, command_allowlist, timeouts.

## Capability-First Activation Model

`config/tools.yaml` is the canonical catalog of **all** available tools (typically enabled by default). Capabilities select which subset the agent can use:

- **assistant** (`config/capabilities/assistant.yaml`): baseline capability with general-purpose tools (memory, ask, shell readonly, web search). Does not include higher-risk tools like `shell_execute_allowlisted`.
- **Add-on capabilities** (e.g. `deploy`, `github-ops`): extend the toolset for specific needs. Example: `deploy` adds `shell_execute_allowlisted` with a `command_allowlist` for `gh`, `git`, etc.

To restrict the general-purpose agent from using `gh` while allowing a specialized "github ops" agent: keep `shell_execute_allowlisted` enabled in `tools.yaml`, omit it from `assistant`, and add it only in a `github-ops` capability. Enable `assistant` for the general agent; enable `assistant` + `github-ops` for the specialized one.

### macOS Personal Capability

The **macos_personal** capability (`config/capabilities/macos_personal.yaml`) exposes Notes and Reminders tools. It is opt-in and disabled by default.

**Tool IDs** (in `config/tools.yaml`):

- `macos_notes_read` – list recent Notes (name, body)
- `macos_notes_write` – create a Note (title, body)
- `macos_reminders_read` – list Reminders (name, body, due_date)
- `macos_reminders_write` – create a Reminder (title, body, list_name, due_date)

**Activation**: Add `macos_personal` to `enabled_capabilities` in `config/capabilities.yaml`:

```yaml
enabled_capabilities:
  - assistant
  - macos_personal
denied_capabilities: []
```

**Platform**: Tools return `rejected_platform` on non-macOS (non-darwin) systems. On macOS, they invoke AppleScript via `osascript` with bounded timeouts.

Capability definition schema (`config/capabilities/*.yaml`):
- `capability_id`: stable identifier (e.g. `assistant`, `omnichem-deploy`).
- `prompt`: optional system prompt fragment appended for this capability.
- `tools`: list of `{tool_id, enabled, params_override}` bindings.
- `tool_overrides`: optional per-tool param overrides keyed by tool_id.
- Discovery: load all `*.yaml` under `config/capabilities/`; duplicate `capability_id` is a startup error.

## Tool Status and Params Merge Rules

This section defines how runtime tool availability and parameters are resolved from:
- `config/capabilities.yaml` (policy),
- `config/capabilities/<id>.yaml` (capability tool bindings/overrides),
- `config/tools.yaml` (tool definitions/default params).

Resolution flow:
1. Start from policy:
   - include only `enabled_capabilities`,
   - exclude any capability listed in `denied_capabilities`.
2. Collect `tool_id` values from each surviving capability manifest where binding `enabled=true`.
3. Keep only tools that also exist in `config/tools.yaml` with `enabled=true`.
4. Resolve tool callable from `entrypoint`.

Status (enabled/disabled) semantics:
- A tool is executable only if ALL are true:
  - capability is enabled by policy and not denied,
  - capability tool binding exists and `enabled=true`,
  - global tool definition exists and `enabled=true`.
- If a capability references a tool that is missing or disabled in `config/tools.yaml`, bootstrap fails (fail-fast).

Parameter merge semantics:
- Base params come from `config/tools.yaml` -> `tools[].default_params`.
- Capability overrides are then applied for each enabled capability in `enabled_capabilities` order.
- Within one capability:
  - inline `tools[].params_override` is applied first,
  - `tool_overrides[tool_id]` is applied after it (therefore wins on key conflicts).
- If multiple capabilities override the same tool/key, later capabilities in `enabled_capabilities` win.

List handling:
- `command_allowlist` is replaced by override value (not deep-merged item-by-item).
- `shell_readonly_commands` is replaced by override value (not union-merged).

Skill manifest schema requirements:
- `skill_id`: stable identifier.
- `entrypoint`: `<python_module>:<callable_name>`.
- `required_capabilities`: list of capability IDs required for execution.
- `capabilities`: optional additional capabilities requested dynamically.
- `dependency_resolution`: all `required_capabilities` must be registered and enabled at load time; unresolved dependencies block skill activation.
- Discovery pattern: load manifests from `plugins/skills/*/manifest.yaml`; duplicate `skill_id` values are startup errors.

Capability policy schema (`config/capabilities.yaml`):
- `enabled_capabilities`: list of capability IDs to enable (must have a manifest in `config/capabilities/*.yaml`).
- `denied_capabilities`: list of capability IDs to block (overrides enabled).

MCP server registry schema requirements (`config/mcp_servers.yaml`):
- `server_id`: stable identifier, lower snake case.
- `transport`: one of `stdio`, `http`.
- `enabled`: bool.
- `connection`: transport-specific connection config (command/args/env_refs for stdio, base_url/headers for http).
- `tool_policy`: optional server-level default policy (`allow`, `deny`, `confirm`).

MCP tool mapping schema requirements (`plugins/mcp/*/tool_map.yaml`):
- `server_id`: must reference a registered MCP server.
- `capability_id_pattern`: must resolve to `cap.mcp.<server>.<tool>`.
- `tools`: explicit allowlisted tool definitions with:
  - `tool_name`,
  - `summary`,
  - `input_schema`,
  - `risk_class` (`readonly`, `interactive`, `side_effecting`),
  - `requires_confirmation` (bool override).
- `descriptor_overrides` (optional): custom `safety_notes` or examples per mapped tool.

## Dynamic Activation Model (Normative)

The system must separate **runtime registry availability** from **model-visible tool context**.

- Runtime registry may discover and validate all manifests at startup.
- The model must not receive all tool definitions by default.
- Per turn, only shortlisted capability descriptors are injected into model context.
- Capability execution must originate from provider-native tool calls; plain text payloads must not be treated as tool execution requests.

This is the required activation flow:

1. Build candidate set from intent signals:
   - user request text/entities,
   - memory intent-to-capability mappings,
   - optional skill `required_capabilities`.
2. Query catalog/registry and rank candidates (`top_n` small, recommended 1-3).
3. Enforce policy gates before model exposure:
   - remove denied capabilities,
   - keep only allowlisted capabilities for current context.
4. Load tool descriptors for survivors only.
5. Inject descriptor subset into model context and execute.
6. If no viable capability remains:
   - ask targeted clarification, or
   - return actionable refusal/guidance for capability registration.

For MCP-backed capabilities, steps 2-4 must additionally enforce:
- server connectivity and health checks,
- tool-level allowlisting from MCP mapping config,
- risk-class confirmation gates before descriptor injection.

## Catalog Query Contract

`CMP_TOOL_RUNTIME_REGISTRY` must provide deterministic catalog-query operations:

- `list_capabilities(filters)`:
  - filters by domain, side_effecting/read_only, and policy eligibility.
- `resolve_candidates(intent, entities, top_n)`:
  - returns ranked capability IDs with score + reason.
- `get_tool_descriptors(capability_ids)`:
  - returns compact LLM-facing descriptors for context injection.

Response records must include:
- `capability_id`,
- `score`,
- `selection_reason`,
- `policy_state` (`allowed`, `denied`, `requires_confirmation`).

## Tool Context Injection Rules

- Keep descriptor payload compact and deterministic.
- Include only fields needed for safe invocation: name, arguments, constraints, confirmation requirement.
- Do not inject raw implementation internals or unrestricted shell command text.
- Include safety-critical notes verbatim when `requires_confirmation=true`.
- Maintain audit metadata of which descriptors were injected for each turn.

For MCP descriptors:
- include `mcp_server` and `mcp_tool` metadata for traceability,
- never inject raw MCP transport credentials or command/env internals.

## Example: Reminder Request

For a user turn like "set a reminder for tomorrow at 9 to pay rent":

1. Intent resolves to reminder creation.
2. Catalog query returns candidates (for example `cap.macos.reminders.write`).
3. Policy gate confirms capability is allowed in current runtime.
4. Inject only reminder-related descriptor(s) into context.
5. Execute capability with normalized arguments.
6. Persist audit: candidate scores, chosen capability, descriptor IDs injected, execution outcome.

## Inputs

- Capability/skill manifests and runtime configuration policies.
- MCP server registry and tool mapping configs.
- Invocation requests from orchestrator and skills.

## Outputs

- Capability and skill execution results.
- MCP invocation results normalized to capability result contract.
- Policy decision events and execution audits.

## Constraints

- All side-effecting capabilities must pass explicit capability checks.
- Shell and macOS automation commands must use allowlisted patterns.
- External MCP tools must be explicitly allowlisted by server and tool name.
- Capability failures must return normalized errors without crashing orchestrator.
- High-risk operational procedures (for example, production deployments) must not rely on memory retrieval alone.

## External MCP Capability Bridge (Normative)

External MCP servers are integrated through `CMP_TOOL_RUNTIME_REGISTRY` as capability providers, not as direct orchestrator dependencies.

Required bridge behavior:
- Discover enabled MCP servers from `config/mcp_servers.yaml` at startup.
- Establish MCP client sessions and fetch tool metadata.
- Map allowlisted MCP tools to internal capability IDs (`cap.mcp.<server>.<tool>`).
- Synthesize or load capability descriptors for mapped MCP tools.
- Apply capability policy gates before model exposure and invocation.
- Normalize MCP responses/errors into standard capability result/error format.

Risk and policy handling:
- `readonly` MCP tools may run without confirmation when policy allows.
- `interactive` and `side_effecting` MCP tools must enforce confirmation policy before execution.
- Server-level deny policy must block all mapped tools regardless of per-tool configuration.

Audit requirements for MCP invocations:
- record `capability_id`, `mcp_server`, `mcp_tool`,
- record policy state and confirmation decision,
- record normalized inputs (or redacted hash for sensitive payloads),
- record execution outcome and latency.

Example mapping (chrome-devtools):
- `cap.mcp.chrome_devtools.browser_navigate`
- `cap.mcp.chrome_devtools.browser_snapshot`
- `cap.mcp.chrome_devtools.browser_click`
- `cap.mcp.chrome_devtools.browser_type`

## Capability vs Memory Boundary

Capabilities and memory serve different purposes and must not be treated as interchangeable.

- Capabilities encode deterministic, repeatable procedures with policy checks, confirmation gates, timeout behavior, retries, and auditability.
- Memory stores contextual facts and routing hints (for example, project defaults, aliases, preferences, and recent decisions).
- Memory may guide capability selection, but memory content must not directly execute side-effecting multi-step procedures.

### Capability-Required Cases

A user-provided procedure must be represented as a capability (or capability template + project config) when any of the following are true:

- The workflow is side-effecting and high risk (for example, production deploy, release tagging, infrastructure changes).
- The workflow has ordered dependent steps (for example, update files -> push tag -> wait for build -> update deployment manifest -> push).
- The workflow requires external-state waits or gate checks (for example, CI completion, status checks).
- The workflow must be repeatable with stable behavior across sessions.
- The workflow requires clear audit logs and policy enforcement.

### Memory-Only Cases

Memory-only representation is acceptable for low-risk context:

- User preferences (for example, concise response style).
- Stable factual metadata (for example, repository alias, default branch name).
- Ephemeral task context that does not trigger privileged or destructive actions.

### Promotion and Versioning Rules

- If a memory-stored procedure is reused or considered safety-critical, promote it to a capability before autonomous execution.
- On first capture of a procedural workflow, require user confirmation to save as a capability.
- When procedure semantics change, create a new capability version; do not silently mutate prior behavior.
- Memory should store mapping metadata, such as: `intent=deploy_to_prod`, `project=<id>`, `capability_id=<cap.deploy.prod.vN>`, and default input values.

### Execution Contract

- Intent resolution may start from memory, but execution must be delegated to a registered capability runtime.
- If no capability exists for a high-risk intent, the system must request confirmation or refuse autonomous execution with an actionable error.
- Capability invocation records must include resolved inputs, policy decisions, and outcome status for post-action review.

## Risks

- Unsafe command execution without strict policy control.
- Manifest dependency mismatch or capability drift.
- Overreliance on memory for operational runbooks can cause step omission, order drift, and inconsistent safety checks.

## Done Criteria

- Manifests load deterministically and invalid manifests are rejected.
- Allowed capability actions execute successfully with audit logs.
- Blocked actions produce policy errors with clear diagnostics.

