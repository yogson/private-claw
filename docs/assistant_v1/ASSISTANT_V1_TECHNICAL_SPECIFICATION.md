# ASSISTANT V1 Technical Specification

## Overview

This document defines technical contracts and implementation requirements for Personal AI Assistant v1. Scope includes Telegram interaction, file-system long-term memory, scheduled automations, manifest-based capabilities/skills, and sub-agent spawning with explicit model selection.

Primary implementation context:
- Single-user, single-host runtime
- Python with `uv`
- FastAPI operational/API surface
- Pydantic AI with Anthropic-first model strategy
- No database in v1

## Document Boundaries

This document is the cross-domain specification and contract source for v1.

Boundary governance and anti-duplication policy are defined in:
- `docs/assistant_v1/COMPONENT_BOUNDARIES.md`

Domain-specific behavior, detailed policies, and implementation contracts are owned by:
- `docs/assistant_v1/domains/agent.md`
- `docs/assistant_v1/domains/telegram.md`
- `docs/assistant_v1/domains/memory.md`
- `docs/assistant_v1/domains/capabilities.md`
- `docs/assistant_v1/domains/subagents.md`
- `docs/assistant_v1/domains/store.md`
- `docs/assistant_v1/domains/observability.md`
- `docs/assistant_v1/domains/api.md`
- `docs/assistant_v1/domains/admin.md`

Capability identifier catalog is owned by:
- `docs/assistant_v1/CAPABILITY_CATALOG.md`

Ownership rule:
- Keep cross-domain contracts and global invariants here.
- Keep component-owned behavior in domain documents.

Cross-domain precedence:
- Decomposition strategy is owned by `docs/assistant_v1/ASSISTANT_V1_ARCHITECTURE_ANALYSIS.md`.
- Delivery sequencing and quality gates are owned by `docs/assistant_v1/ASSISTANT_V1_IMPLEMENTATION_PLAN.md`.
- Capability IDs are owned by `docs/assistant_v1/CAPABILITY_CATALOG.md`.
- Sub-agent field-level contract schema is owned by `docs/assistant_v1/SUBAGENT_ORCHESTRATION_CONTRACT.md`.

## System Interfaces

### External Interfaces

| Interface ID | Interface Name | Direction | Purpose |
|---|---|---|---|
| `CMP_CHANNEL_TELEGRAM_ADAPTER` | Telegram Bot API | Inbound/Outbound | User messaging, media intake, response delivery |
| `CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER` | Anthropic API | Outbound | Main-agent and sub-agent inference |
| `CMP_TOOL_WEB_RESEARCH` | Web search provider endpoint | Outbound | Research queries and retrieval |
| `CMP_TOOL_CALENDAR_CONNECTOR` | Calendar provider API or local calendar CLI bridge | Inbound/Outbound | Calendar query and event actions |
| `CMP_TOOL_MACOS_AUTOMATION` | macOS CLI/automation capabilities | Local | Notes/Reminders operations via controlled command bridge |
| `CMP_TOOL_MCP_BRIDGE` | External MCP servers (for example chrome-devtools MCP) | Inbound/Outbound | Tool discovery and invocation via capability registry policy gates |

### Internal Interfaces

| Interface ID | Producer | Consumer | Purpose |
|---|---|---|---|
| `INT_ORCH_EVENT_INPUT` | Telegram/API/Scheduler adapters | `CMP_CORE_AGENT_ORCHESTRATOR` | Normalized inbound event contract |
| `INT_ORCH_CONTEXT_BUILD` | `CMP_CORE_AGENT_ORCHESTRATOR` | `CMP_MEMORY_FILESYSTEM_STORE` | Retrieve relevant memory context |
| `INT_SKILL_EXECUTE` | `CMP_CORE_AGENT_ORCHESTRATOR` | `CMP_SKILL_RUNTIME_ENGINE` | Skill invocation and result contract |
| `INT_TOOL_EXECUTE` | Skill engine/orchestrator | `CMP_TOOL_RUNTIME_REGISTRY` | Capability invocation under permissions |
| `INT_SUBAGENT_SPAWN` | `CMP_CORE_AGENT_ORCHESTRATOR` | `CMP_AGENT_SUBAGENT_COORDINATOR` | Sub-agent task spawning with explicit model |
| `INT_STORE_SESSION_RW` | Agent/Channel/Scheduler components | `CMP_STORE_STATE_FACADE` | Session and runtime state persistence |
| `INT_STORE_TASK_RW` | Sub-agent/Scheduler components | `CMP_STORE_STATE_FACADE` | Task state persistence and lifecycle updates |
| `INT_STORE_IDEMPOTENCY` | Telegram/API adapters | `CMP_STORE_STATE_FACADE` | Duplicate ingress event detection and registration |
| `INT_STORE_LOCK` | Agent/Sub-agent/Scheduler components | `CMP_STORE_STATE_FACADE` | Lock acquire/release with TTL |
| `INT_MEMORY_INDEX_UPDATE` | Memory/Scheduler components | `CMP_MEMORY_FILESYSTEM_STORE` | Index update/rebuild/repair operations |
| `INT_CHANNEL_RESPONSE` | `CMP_CORE_AGENT_ORCHESTRATOR` | Channel adapters | Outbound normalized response contract |
| `INT_OBS_TRACE_EVENT` | All runtime components | `CMP_OBSERVABILITY_LOGGING` | Structured trace/audit emission |

### Normalized Event Contract (`INT_ORCH_EVENT_INPUT`)

Event type enumeration:
- `user_text_message`
- `user_voice_message`
- `user_attachment_message`
- `user_callback_query`
- `scheduler_trigger`
- `system_control_event`

Common required fields:
- `event_id`
- `event_type`
- `source` (telegram, scheduler, api, system)
- `session_id`
- `user_id`
- `created_at`
- `trace_id`

Common optional fields:
- `text`
- `attachments`
- `metadata`
- `idempotency_key`

Voice/attachment metadata:
- `voice`: `file_id`, `duration_seconds`, `transcript_text?`, `transcript_confidence?`
- `attachment`: `file_id`, `mime_type`, `file_size_bytes`, `caption?`
- `callback_query`: `callback_id`, `callback_data`, `origin_message_id`, `ui_version`
  - session-resume callbacks should carry signed action metadata including `action=resume_session` and `target_session_id`.

Scheduler-originated fields:
- `job_id`
- `trigger_kind` (reminder, maintenance, monitor_check)
- `scheduled_for`
- `attempt_number`

Validation requirements:
- Required fields must be present for all event types.
- Event-specific fields must match allowed schema for the event type.
- Duplicate `idempotency_key` from same source must be rejected.

### Outbound Channel Response Contract (`INT_CHANNEL_RESPONSE`)

Required fields:
- `response_id`
- `channel` (telegram)
- `session_id`
- `trace_id`
- `message_type` (`text`, `interactive`)

Text response payload:
- `text`
- `parse_mode?`

Interactive response payload:
- `text`
- `ui_kind` (`inline_keyboard` | `reply_keyboard`)
- `actions`: array of button definitions
  - `label`
  - `callback_id` (required for `inline_keyboard`; optional for `reply_keyboard`)
  - `callback_data` (required for `inline_keyboard`; optional for `reply_keyboard`)
  - `style?`
- `ui_version` (for `inline_keyboard`)

For `ui_kind: "reply_keyboard"`:
- Button label is sent as user text when tapped; no callback.
- `callback_id` and `callback_data` may be empty.
- `ui_version` is not required.

For `ui_kind: "inline_keyboard"`:
- Callbacks are used; `callback_id` and `callback_data` must be set.
- `callback_data` must be signed or otherwise tamper-evident.

Validation and safety:
- Interactive responses must include deterministic `callback_id` values.
- `callback_data` must be signed or otherwise tamper-evident.
- Callback handlers must validate `ui_version` to avoid stale action execution.

Telegram command menu requirements:
- Runtime bootstrap must register Telegram command metadata for `/new`, `/reset`, `/sessions`.
- Runtime bootstrap should set Telegram chat menu button to `MenuButtonCommands` for consistent command discovery UX.
- Command-menu bootstrap failures should be non-fatal and logged; update polling must continue.

## Data Structures

### Canonical Model Families

This specification defines cross-domain model families and ownership only.
Detailed field-level schemas are owned by each domain document.

| Model Family | Ownership Document | Purpose |
|---|---|---|
| Memory artifacts and indexes | `docs/assistant_v1/domains/memory.md` | Long-term recall and deterministic retrieval |
| Capability/skill manifests and policy config | `docs/assistant_v1/domains/capabilities.md` | Runtime extension model and safety enforcement |
| Scheduler jobs and audit records | `docs/assistant_v1/domains/agent.md` + scheduler implementation docs | Timed and maintenance execution |
| Sub-agent runtime task models | `docs/assistant_v1/domains/subagents.md` | Delegation lifecycle and async execution records |
| Store/session/idempotency/lock models | `docs/assistant_v1/domains/store.md` | Persistence, deduplication, and coordination |

### Scheduling Models

| Model ID | Path Pattern | Required Fields | Purpose |
|---|---|---|---|
| `CMP_DATA_MODEL_SCHEDULE_JOB` | `runtime/scheduler/jobs/*.json` | `job_id`, `kind`, `timezone`, `next_run_at`, `status` | Scheduled executions |
| `CMP_DATA_MODEL_SCHEDULE_AUDIT` | `runtime/scheduler/history/*.json` | `job_id`, `ran_at`, `result`, `error_code?` | Execution history |

### Sub-agent Models

Sub-agent model schemas and runtime lifecycle persistence are owned by:
- `docs/assistant_v1/domains/subagents.md`

Task model distinction remains global and mandatory:
- `CMP_DATA_MODEL_TASK_MEMORY`: user-facing task/reminder knowledge in long-term memory.
- `CMP_DATA_MODEL_SUBAGENT_TASK`: runtime execution task state for background workers.
- Relationship rule: sub-agent execution may update task memory, but these models are never interchangeable.

### Store Models

Store model schemas and filesystem/lock semantics are owned by:
- `docs/assistant_v1/domains/store.md`

Store facade component decomposition remains canonical:
- `CMP_STORE_STATE_FACADE`: public store abstraction and routing layer.
- `CMP_STORE_SESSION_PERSISTENCE`: session log append/read/replay paths.
- `CMP_STORE_TASK_PERSISTENCE`: sub-agent/scheduler task state persistence.
- `CMP_STORE_IDEMPOTENCY_LEDGER`: idempotency key registration and lookup.
- `CMP_STORE_LOCK_COORDINATOR`: distributed/local lock semantics with TTL.

## Business Logic Specifications

### Core Request Processing

1. Inbound event normalized into `INT_ORCH_EVENT_INPUT`.
2. Orchestrator resolves intent and policy context.
3. Memory retrieval performed by type-sensitive query strategy.
4. Execution route selected: direct model, skill+capability chain, or sub-agent delegation.
5. Response returned to channel and memory updated as needed.
6. Observability/audit records emitted with correlation IDs.

Telegram voice input note:
- For v1, voice handling performs synchronous transcription via a Telegram MTProto user-client worker.
- The channel adapter calls the worker during intake and maps successful result to `voice.transcript_text` before `INT_ORCH_EVENT_INPUT` handoff.
- Bot API remains the inbound/outbound transport; transcription calls are MTProto-only.
- No external speech-to-text service is required in baseline architecture.
- If transcription fails/times out/is unsupported, event intake proceeds with `voice.transcript_text=null` and failure reason in channel metadata/audit logs.

### Session Lifecycle Policy

- Session creation:
  - If inbound event has unknown `session_id`, create session with initial metadata and greeting eligibility state.
- Session initialization:
  - Persist initial session record before first response write.
- Active session policy:
  - Single user may have multiple active sessions keyed by channel/chat context.
- User-selectable resume policy:
  - Telegram flow may present latest resumable sessions for the current user/chat and accept callback selection.
  - Selected `target_session_id` becomes active for subsequent turns in that chat context.
  - Resume selection must fail closed if callback signature, user scope, or chat scope validation fails.
- Session TTL/expiry:
  - Sessions remain resumable by default; inactivity archival threshold is configurable.
- Restart behavior:
  - Runtime must reload session logs from store and continue from last persisted event.
- Concurrency:
  - Session-level lock required for turn writes and replay snapshots.

### Memory Write Policy

Global invariants:
- Write only category-aligned memory artifacts: profile, preference, project, task, fact, summary.
- Require confidence threshold for persistent preference/fact updates.
- Ensure idempotent updates using stable `memory_id`.

Detailed write/consolidation behavior is owned by:
- `docs/assistant_v1/domains/memory.md`

### Memory Retrieval and Indexing Policy (No Vector Search)

Global invariants:
- v1 retrieval is deterministic and index-driven; no vector databases or embedding search are required.
- Retrieval must remain auditable and bounded by per-turn context limits.

Detailed retrieval/index policy, scoring, top-K caps, and optional small-model gates are owned by:
- `docs/assistant_v1/domains/memory.md`

### Scheduler Behavior

- Single configured timezone globally applied.
- Job kinds: one-off reminder, recurring reminder, maintenance/consolidation task.
- Failed jobs move to retry state with bounded retry strategy.
- Missed-run policy executes once at next availability when within max lateness threshold.

### Capability and Skill Runtime

Global invariants:
- Registry discovers manifests at startup and validates schemas.
- Capability invocation only proceeds when capability policy allows requested action.
- All external side effects generate audit events.
- Model-visible tool definitions are injected dynamically per turn from a ranked capability shortlist; full catalog is not injected by default.
- External MCP tool invocations must be mapped to capability IDs and pass the same policy/confirmation gates as first-party capabilities.
- Capability side effects must be triggered only by provider-native tool calls; model text/JSON output must not be interpreted as executable tool intent.

Detailed manifest schemas, dependency rules, and runtime policy handling are owned by:
- `docs/assistant_v1/domains/capabilities.md`
- `docs/assistant_v1/CAPABILITY_CATALOG.md`

### Sub-agent Spawning Contract

Sub-agent spawning is available only through `INT_SUBAGENT_SPAWN`.
Sub-agent execution is asynchronous and background-task oriented; the main agent receives immediate acknowledgement and continues turn completion.

Global invariants:
- Every spawn must include explicit `model_id`.
- Capability scope must be a subset of parent-permitted capabilities.
- Task lifecycle must be persisted and auditable.
- TTL/timeout/heartbeat controls are mandatory.

Detailed request/result schemas, lifecycle semantics, and coordinator rules are owned by:
- `docs/assistant_v1/domains/subagents.md`

## Validation and Constraints

### Input Validation Rules

| Input | Rule | Valid Example | Invalid Example |
|---|---|---|---|
| Telegram user ID | Must match configured allowlist | `123456789` in allowlist | Unknown ID |
| Time expression | Must resolve to single configured timezone | `tomorrow 09:00` | Ambiguous timezone expression |
| Memory metadata `type` | Must be one of defined memory categories | `preference` | `prefs` |
| Capability ID | Must exist in capability registry | `cap.macos.calendar.read` | `calendar.root_access` |
| Sub-agent `model_id` | Must be in model allowlist | `anthropic.claude-sonnet-*` | Arbitrary free-form string |

### Structural Constraints

- All persisted artifacts must include creation/update timestamps in ISO format.
- Path writes must remain inside configured application data root.
- Shell/macOS capabilities must execute only allowlisted command templates.
- Sub-agent invocations must carry parent trace ID.
- Sub-agent tasks must persist state transitions atomically.
- Long-running sub-agent tasks must either heartbeat or transition terminally.

### Limits

- Maximum concurrent sub-agents per parent request: configurable hard limit.
- Maximum concurrent background sub-agent tasks globally: configurable hard limit.
- Maximum task TTL: configurable hard limit by task type.
- Maximum stale heartbeat window before forced expiry/failure.
- Maximum scheduler jobs: configurable soft cap with rejection behavior.
- Maximum attachment size: channel-configured threshold with graceful refusal.
- Maximum memory update batch per interaction: bounded for latency control.
- Maximum injected memory artifacts per turn: enforced by category top-K caps.

## Error Handling

### Error Classes

| Error ID | Scenario | Behavior |
|---|---|---|
| `CMP_ERROR_CHANNEL_UNAUTHORIZED` | Telegram sender not allowlisted | Reject and log audit warning |
| `CMP_ERROR_MEMORY_WRITE_FAILED` | File write/serialization error | Retry once then return degraded response |
| `CMP_ERROR_MEMORY_INDEX_LOOKUP_FAILED` | Memory index read/query failure | Fallback to direct file scan, emit warning |
| `CMP_ERROR_MEMORY_INDEX_CORRUPTED` | Index file corrupted or checksum mismatch | Mark degraded, trigger rebuild, continue with fallback retrieval |
| `CMP_ERROR_CAPABILITY_MANIFEST_PARSE_FAILED` | Capability/skill manifest invalid | Skip load, report startup/runtime diagnostics |
| `CMP_ERROR_CONFIG_VALIDATION_FAILED` | Required config missing or invalid at startup | Fail fast startup with actionable validation report |
| `CMP_ERROR_STORE_LOCK_TIMEOUT` | Lock not acquired before timeout | Abort operation or retry with backoff policy |
| `CMP_ERROR_TOOL_FORBIDDEN` | Capability policy denies capability action | Reject action and notify user |
| `CMP_ERROR_SUBAGENT_POLICY_BLOCK` | Invalid model/capability/budget | Reject spawn, continue with fallback strategy |
| `CMP_ERROR_SUBAGENT_EXPIRED` | Task exceeded TTL | Mark task expired and emit completion event |
| `CMP_ERROR_SUBAGENT_STALE_HEARTBEAT` | Worker heartbeat missing beyond threshold | Fail or expire task and release worker slot |
| `CMP_ERROR_PROVIDER_UNAVAILABLE` | Model provider outage/timeouts | Retry with bounded backoff; return graceful failure |
| `CMP_ERROR_SCHEDULER_EXECUTION` | Scheduled job failed | Record failure, schedule retry or mark failed |

### Recovery Strategies

- Prefer bounded retries for transient network/provider failures.
- For persistent failures, degrade to simpler response path and surface actionable message.
- Always emit structured error events for diagnosability.

## Performance Requirements

### Response and Throughput Targets

- Standard text query p95 response target: under 8 seconds.
- Capability-assisted query p95 target: under 15 seconds.
- Sub-agent delegated task first response/ack target: under 2 seconds.
- Scheduler trigger latency target: under 30 seconds from planned run time.

### Resource Targets

- Single-host operation with bounded CPU/memory footprint under moderate personal usage.
- Memory retrieval and update operations should remain predictable with linear file growth controls.
- Log/audit output volume should remain manageable for local retention windows.

## Implementation Details

### Configuration Domains

- `config/app.yaml`: runtime mode, data root, timezone.
- `config/channel.telegram.yaml`: bot token, polling settings, allowlist.
- `config/model.yaml`: default model, model allowlist, routing/budget policies.
- `config/capabilities.yaml`: capability/skill/sub-agent capability rules.
- `config/mcp_servers.yaml`: MCP server connection registry and server-level default tool policy.
- `config/scheduler.yaml`: polling cadence, retry policy, lateness thresholds.
- `config/store.yaml`: backend selection, lock TTL, atomic write settings, idempotency retention.

Environment variable naming convention:
- `ASSISTANT_<DOMAIN>_<KEY>` (uppercase snake case), for example:
  - `ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN`
  - `ASSISTANT_MODEL_DEFAULT_ID`
  - `ASSISTANT_STORE_LOCK_TTL_SECONDS`

Configuration schema baseline:

| File | Required Fields | Optional Fields | Example Defaults |
|---|---|---|---|
| `config/app.yaml` | `runtime_mode`, `data_root`, `timezone` | `log_level` | `runtime_mode=prod`, `timezone=UTC` |
| `config/channel.telegram.yaml` | `bot_token`, `allowlist` | `poll_timeout_seconds`, `poll_interval_seconds`, `startup_drop_pending_updates`, `mtproto_api_id`, `mtproto_api_hash`, `transcription_timeout_seconds`, `throttle_max_per_minute` | `poll_timeout_seconds=30`, `poll_interval_seconds=0` |
| `config/model.yaml` | `default_model_id`, `model_allowlist` | `quality_routing`, `max_tokens_default` | `quality_routing=quality_first` |
| `config/capabilities.yaml` | `allowed_capabilities` | `denied_capabilities`, `command_allowlist` | `denied_capabilities=[]` |
| `config/mcp_servers.yaml` | `servers` | `defaults`, `timeouts` | `defaults.enabled=true` |
| `config/scheduler.yaml` | `tick_seconds`, `retry_policy` | `max_lateness_seconds` | `tick_seconds=10` |
| `config/store.yaml` | `backend`, `lock_ttl_seconds`, `atomic_write` | `idempotency_retention_seconds` | `backend=filesystem`, `atomic_write=true` |

Validation policy:
- Startup must fail when any required field is absent or invalid.
- Optional fields must receive documented defaults.
- Environment overrides must be validated using the same schema rules.

Retrieval-related configuration keys:
- `retrieval_llm_threshold_count` (int, optional, default: 20)
- `retrieval_llm_ambiguity_delta` (float, optional, default: 0.05)
- `retrieval_context_token_budget` (int, optional, default: 1200)
- `retrieval_small_model_id` (string, optional)
- `retrieval_small_model_timeout_seconds` (int, optional, default: 5)

### Identifier Namespace Mapping

- `CMP_*`: architecture component IDs used for design traceability.
- `cap.*`: runtime capability IDs used for invocation and policy enforcement.
- Mapping rule: manifests should reference both where applicable:
  - `component_id` (implementation ownership),
  - `capability_id` (runtime authorization unit).

### Store Backend Strategy

Global invariants:
- v1 backend is filesystem.
- Store interfaces must remain backend-agnostic for future adapter swap.

Detailed backend and atomic write semantics are owned by:
- `docs/assistant_v1/domains/store.md`

### Security Requirements

- Telegram allowlist enforcement for all inbound user interactions.
- Admin endpoints protected by shared secret or token.
- Sensitive configuration values loaded from environment variables.
- Avoid logging secrets and sensitive payload contents.

### Observability Requirements

- Every request receives a correlation ID.
- Capability and sub-agent actions produce audit entries.
- Scheduler runs and failures are traceable with consistent event schema.

## API Specification (Operational)

### Required Endpoint Groups

Endpoint groups remain mandatory for v1:
- health
- memory admin
- capability/skill admin
- scheduler admin
- sub-agent audit

Detailed endpoint contracts and admin UX ownership:
- `docs/assistant_v1/domains/api.md`
- `docs/assistant_v1/domains/admin.md`

### Authentication and Authorization

- Single-admin token/shared secret for v1 admin surface.
- Role model can remain single-role in v1 but endpoint guards must be explicit.

### Rate Limiting and Throttling

- Telegram ingress should apply per-minute throttling to avoid accidental loops.
- Sub-agent spawn endpoint/path must enforce strict per-request and per-hour limits.

