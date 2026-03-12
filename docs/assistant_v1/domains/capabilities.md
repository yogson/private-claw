# Capabilities Domain

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
- Support first-party capabilities in v1:
  - macOS personal integration (Notes, Reminders, Calendar),
  - GitHub CLI (`gh`) integration,
  - web search and content fetch,
  - Telegram voice transcript extraction capability from channel metadata,
  - memory management operations (including `memory_propose_update` proposal tool).

## Canonical Models

| Model ID | File Pattern | Required Fields | Purpose |
|---|---|---|---|
| `CMP_DATA_MODEL_TOOL_MANIFEST` | `plugins/capabilities/*/manifest.yaml` | `capability_id`, `version`, `capabilities`, `entrypoint`, `permissions` | Capability registration and policy |
| `CMP_DATA_MODEL_CAPABILITY_DESCRIPTOR` | `plugins/capabilities/*/tool_descriptor.yaml` | `capability_id`, `summary`, `input_schema`, `safety_notes` | LLM-facing runtime tool description for dynamic activation |
| `CMP_DATA_MODEL_SKILL_MANIFEST` | `plugins/skills/*/manifest.yaml` | `skill_id`, `version`, `entrypoint`, `required_capabilities`, `capabilities` | Skill registration and dependencies |
| `CMP_DATA_MODEL_CAPABILITY_POLICY` | `config/capabilities.yaml` | `allowed_capabilities`, `denied_capabilities`, `command_allowlist` | Runtime safety policy |
| `CMP_DATA_MODEL_MCP_SERVER_REGISTRY` | `config/mcp_servers.yaml` | `server_id`, `transport`, `enabled`, `connection` | External MCP server registration and connection policy |
| `CMP_DATA_MODEL_MCP_TOOL_MAPPING` | `plugins/mcp/*/tool_map.yaml` | `server_id`, `tools`, `capability_id_pattern`, `risk_class` | MCP tool-to-capability mapping and descriptor synthesis |

Capability manifest schema requirements:
- `capability_id`: string, `cap.<domain>.<action>` naming convention.
- `version`: semantic version string.
- `entrypoint`: `<python_module>:<callable_name>` format.
- `capabilities`: list of concrete capability IDs granted by this manifest.
- `permissions` object:
  - `read_only` (bool),
  - `side_effecting` (bool),
  - `requires_confirmation` (bool),
  - `timeout_seconds` (int).
- Discovery pattern: load manifests from `plugins/capabilities/*/manifest.yaml`; duplicate `capability_id` values are startup errors.

Skill manifest schema requirements:
- `skill_id`: stable identifier.
- `entrypoint`: `<python_module>:<callable_name>`.
- `required_capabilities`: list of capability IDs required for execution.
- `capabilities`: optional additional capabilities requested dynamically.
- `dependency_resolution`: all `required_capabilities` must be registered and enabled at load time; unresolved dependencies block skill activation.
- Discovery pattern: load manifests from `plugins/skills/*/manifest.yaml`; duplicate `skill_id` values are startup errors.

Capability policy schema requirements (`config/capabilities.yaml`):
- `allowed_capabilities`: list of allowlisted capability IDs.
- `denied_capabilities`: list of blocked capability IDs.
- `command_allowlist`: list of allowed command templates with:
  - `id`,
  - `command_pattern`,
  - `allowed_args_pattern`,
  - `max_timeout_seconds`.

Capability descriptor schema requirements (`plugins/capabilities/*/tool_descriptor.yaml`):
- `capability_id`: must match a registered manifest capability.
- `summary`: concise capability purpose in model-friendly text.
- `input_schema`: JSON-schema-like argument contract for runtime invocation.
- `safety_notes`: short constraints/confirmation conditions to preserve in prompt context.
- `examples` (optional): 1-3 compact invocation examples for disambiguation.

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

