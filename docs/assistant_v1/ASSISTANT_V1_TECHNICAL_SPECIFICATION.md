# ASSISTANT V1 Technical Specification

## Overview

This document defines technical contracts and implementation requirements for Personal AI Assistant v1. Scope includes Telegram interaction, file-system long-term memory, scheduled automations, manifest-based capabilities/skills, and sub-agent spawning with explicit model selection.

Primary implementation context:
- Single-user, single-host runtime
- Python with `uv`
- FastAPI operational/API surface
- Pydantic AI with Anthropic-first model strategy
- No database in v1

## System Interfaces

### External Interfaces

| Interface ID | Interface Name | Direction | Purpose |
|---|---|---|---|
| `CMP_CHANNEL_TELEGRAM_ADAPTER` | Telegram Bot API | Inbound/Outbound | User messaging, media intake, response delivery |
| `CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER` | Anthropic API | Outbound | Main-agent and sub-agent inference |
| `CMP_TOOL_WEB_RESEARCH` | Web search provider endpoint | Outbound | Research queries and retrieval |
| `CMP_TOOL_CALENDAR_CONNECTOR` | Calendar provider API or local calendar CLI bridge | Inbound/Outbound | Calendar query and event actions |
| `CMP_TOOL_MACOS_AUTOMATION` | macOS CLI/automation capabilities | Local | Notes/Reminders operations via controlled command bridge |

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
| `INT_STORE_IDEMPOTENCY` | `CMP_CHANNEL_TELEGRAM_ADAPTER` | `CMP_STORE_STATE_FACADE` | Duplicate update detection and registration |
| `INT_STORE_LOCK` | Agent/Sub-agent/Scheduler components | `CMP_STORE_STATE_FACADE` | Lock acquire/release with TTL |
| `INT_MEMORY_INDEX_UPDATE` | Memory/Scheduler components | `CMP_MEMORY_FILESYSTEM_STORE` | Index update/rebuild/repair operations |
| `INT_CHANNEL_RESPONSE` | `CMP_CORE_AGENT_ORCHESTRATOR` | Channel adapters | Outbound normalized response contract |
| `INT_OBS_TRACE_EVENT` | All runtime components | `CMP_OBSERVABILITY_LOGGING` | Structured trace/audit emission |

### Normalized Event Contract (`INT_ORCH_EVENT_INPUT`)

Event type enumeration:
- `user_text_message`
- `user_voice_message`
- `user_attachment_message`
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

Scheduler-originated fields:
- `job_id`
- `trigger_kind` (reminder, maintenance, monitor_check)
- `scheduled_for`
- `attempt_number`

Validation requirements:
- Required fields must be present for all event types.
- Event-specific fields must match allowed schema for the event type.
- Duplicate `idempotency_key` from same source must be rejected.

## Data Structures

### Memory Models

Memory persisted as markdown files with YAML frontmatter by memory type.

| Model ID | Path Pattern | Required Metadata | Purpose |
|---|---|---|---|
| `CMP_DATA_MODEL_PROFILE_MEMORY` | `memory/profile/*.md` | `memory_id`, `type=profile`, `tags`, `entities`, `priority`, `confidence`, `last_used_at`, `updated_at`, `source` | Personal profile facts |
| `CMP_DATA_MODEL_PREFERENCES_MEMORY` | `memory/preferences/*.md` | `memory_id`, `type=preference`, `tags`, `entities`, `priority`, `confidence`, `last_used_at`, `updated_at` | User preferences and defaults |
| `CMP_DATA_MODEL_PROJECT_MEMORY` | `memory/projects/*.md` | `memory_id`, `type=project`, `status`, `tags`, `entities`, `priority`, `confidence`, `last_used_at`, `updated_at` | Project-specific notes and progress |
| `CMP_DATA_MODEL_TASK_MEMORY` | `memory/tasks/*.md` | `memory_id`, `type=task`, `status`, `due_at?`, `tags`, `entities`, `priority`, `confidence`, `last_used_at`, `updated_at` | Actionable tasks/reminders |
| `CMP_DATA_MODEL_FACT_MEMORY` | `memory/facts/*.md` | `memory_id`, `type=fact`, `scope`, `tags`, `entities`, `priority`, `confidence`, `last_used_at`, `updated_at` | Stable factual knowledge |
| `CMP_DATA_MODEL_CONVERSATION_SUMMARY` | `memory/summaries/*.md` | `summary_id`, `window_start`, `window_end`, `tags`, `entities`, `priority`, `confidence`, `last_used_at`, `updated_at` | Conversation compaction and recall |
| `CMP_DATA_MODEL_MEMORY_INDEX_TYPE` | `runtime/memory_indexes/index_by_type.json` | `type`, `memory_ids`, `updated_at` | Candidate generation by memory category |
| `CMP_DATA_MODEL_MEMORY_INDEX_TAG` | `runtime/memory_indexes/index_by_tag.json` | `tag`, `memory_ids`, `updated_at` | Candidate generation by semantic tags |
| `CMP_DATA_MODEL_MEMORY_INDEX_ENTITY` | `runtime/memory_indexes/index_by_entity.json` | `entity`, `memory_ids`, `updated_at` | Candidate generation by named entities |
| `CMP_DATA_MODEL_MEMORY_INDEX_PROJECT` | `runtime/memory_indexes/index_by_project.json` | `project_key`, `memory_ids`, `updated_at` | Project/session-focused candidate retrieval |
| `CMP_DATA_MODEL_MEMORY_INDEX_RECENCY` | `runtime/memory_indexes/index_by_recency.json` | `memory_ids`, `updated_at` | Recency-prioritized memory ranking |

### Capability/Skill Plugin Models

| Model ID | File Pattern | Required Fields | Purpose |
|---|---|---|---|
| `CMP_DATA_MODEL_TOOL_MANIFEST` | `plugins/capabilities/*/manifest.yaml` | `capability_id`, `version`, `capabilities`, `entrypoint`, `permissions` | Capability registration and policy |
| `CMP_DATA_MODEL_SKILL_MANIFEST` | `plugins/skills/*/manifest.yaml` | `skill_id`, `version`, `entrypoint`, `required_capabilities`, `capabilities` | Skill registration and dependencies |
| `CMP_DATA_MODEL_CAPABILITY_POLICY` | `config/capabilities.yaml` | `allowed_capabilities`, `denied_capabilities`, `command_allowlist` | Runtime safety policy |

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

### Scheduling Models

| Model ID | Path Pattern | Required Fields | Purpose |
|---|---|---|---|
| `CMP_DATA_MODEL_SCHEDULE_JOB` | `runtime/scheduler/jobs/*.json` | `job_id`, `kind`, `timezone`, `next_run_at`, `status` | Scheduled executions |
| `CMP_DATA_MODEL_SCHEDULE_AUDIT` | `runtime/scheduler/history/*.json` | `job_id`, `ran_at`, `result`, `error_code?` | Execution history |

### Sub-agent Models

| Model ID | Path Pattern | Required Fields | Purpose |
|---|---|---|---|
| `CMP_DATA_MODEL_SUBAGENT_REQUEST` | Runtime object | `task_id`, `objective`, `model_id`, `budget`, `allowed_capabilities` | Parent request contract |
| `CMP_DATA_MODEL_SUBAGENT_RESULT` | Runtime object | `task_id`, `status`, `artifacts`, `usage`, `duration_ms` | Sub-agent completion output |
| `CMP_DATA_MODEL_SUBAGENT_TASK` | `runtime/subagents/tasks/*.json` | `task_id`, `task_type`, `status`, `created_at`, `expires_at`, `model_id`, `allowed_capabilities` | Persisted background task state |
| `CMP_DATA_MODEL_SUBAGENT_HEARTBEAT` | `runtime/subagents/heartbeats/*.json` | `task_id`, `last_heartbeat_at`, `worker_id`, `status` | Liveness tracking for long-running tasks |
| `CMP_DATA_MODEL_SUBAGENT_AUDIT` | `runtime/subagents/history/*.json` | `task_id`, `parent_trace_id`, `model_id`, `budget_used`, `status` | Auditable delegation records |

Task model distinction:
- `CMP_DATA_MODEL_TASK_MEMORY`: user-facing task/reminder knowledge in long-term memory.
- `CMP_DATA_MODEL_SUBAGENT_TASK`: runtime execution task state for background workers.
- Relationship rule: sub-agent execution may update task memory, but these models are never interchangeable.

### Store Models

| Model ID | Path Pattern | Required Fields | Purpose |
|---|---|---|---|
| `CMP_DATA_MODEL_SESSION_LOG` | `runtime/sessions/*.jsonl` | `session_id`, `event_id`, `timestamp`, `role`, `content` | Per-session turn history and metadata |
| `CMP_DATA_MODEL_IDEMPOTENCY_RECORD` | `runtime/idempotency/*.json` | `key`, `source`, `created_at`, `ttl_seconds` | Duplicate event prevention |
| `CMP_DATA_MODEL_LOCK_RECORD` | `runtime/locks/*.lock` | `lock_key`, `owner_id`, `acquired_at`, `expires_at` | Session/task lock coordination |
| `CMP_DATA_MODEL_STORE_RECOVERY_MARKER` | `runtime/recovery/*.json` | `component`, `last_scan_at`, `status` | Startup consistency and recovery tracking |

Store facade component decomposition:
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
- For v1, voice handling uses Telegram-provided transcript text when available.
- No external transcription service is required in baseline architecture.
- If no transcript is available, adapter responds with: "I could not extract voice text from Telegram. Please resend as text or try another voice message."
- If transcript is partial/low confidence, adapter includes extracted text and asks for user confirmation before executing high-impact actions.

### Session Lifecycle Policy

- Session creation:
  - If inbound event has unknown `session_id`, create session with initial metadata and greeting eligibility state.
- Session initialization:
  - Persist initial session record before first response write.
- Active session policy:
  - Single user may have multiple active sessions keyed by channel/chat context.
- Session TTL/expiry:
  - Sessions remain resumable by default; inactivity archival threshold is configurable.
- Restart behavior:
  - Runtime must reload session logs from store and continue from last persisted event.
- Concurrency:
  - Session-level lock required for turn writes and replay snapshots.

### Memory Write Policy

- Write only category-aligned memory artifacts: profile, preference, project, task, fact, summary.
- Require confidence threshold for persistent preference/fact updates.
- Ensure idempotent updates using stable `memory_id`.
- Periodic consolidation merges stale/duplicate records into canonical entries.

### Memory Retrieval and Indexing Policy (No Vector Search)

- v1 retrieval is deterministic and index-driven; no vector databases or embedding search are required.
- Candidate generation must use index lookups by `type`, `tag`, `entity`, and `project`.
- Ranking must use transparent weighted scoring:
  - entity match,
  - tag/type match,
  - recency decay,
  - priority,
  - confidence.
- Retrieval output must apply per-category top-K caps before prompt injection.
- Retrieval operations must emit audit records with selected memory IDs and scoring metadata.
- Index updates occur both:
  - synchronously on memory write/update,
  - asynchronously via scheduled reconciliation jobs.
- Index rebuild/repair must trigger on:
  - startup integrity check failure,
  - checksum mismatch,
  - missing index file,
  - index version mismatch.
- Corruption handling:
  - mark index as degraded,
  - rebuild from source memory artifacts,
  - emit recovery audit event.

Suggested default top-K caps per request:
- profile: up to 2
- preferences: up to 3
- projects/tasks: up to 4
- facts: up to 3
- summaries: up to 1

### Scheduler Behavior

- Single configured timezone globally applied.
- Job kinds: one-off reminder, recurring reminder, maintenance/consolidation task.
- Failed jobs move to retry state with bounded retry strategy.
- Missed-run policy executes once at next availability when within max lateness threshold.

### Capability and Skill Runtime

- Registry discovers manifests at startup and validates schemas.
- Capability invocation only proceeds when capability policy allows requested action.
- Skill execution composes deterministic capability steps and may request sub-agent work.
- All external side effects generate audit events.

### Sub-agent Spawning Contract

Sub-agent spawning is available only through `INT_SUBAGENT_SPAWN`.
Detailed contract baseline is documented in `SUBAGENT_ORCHESTRATION_CONTRACT.md`.
Sub-agent execution is asynchronous and background-task oriented; the main agent receives immediate acknowledgement and continues turn completion.

Required request fields:
- `task_id`: unique parent-scoped identifier
- `objective`: concise task objective
- `model_id`: explicit allowed model identifier
- `max_tokens`: upper token budget
- `timeout_seconds`: hard wall clock limit
- `allowed_capabilities`: explicit capability allowlist
- `result_format`: expected output schema identifier

Coordinator rules:
- Reject spawn if `model_id` is not allowlisted.
- Reject spawn when parent task budget would be exceeded.
- Enforce max concurrent sub-agents per parent request.
- Enforce capability subset relative to parent permission scope.
- Enforce TTL (`expires_at`) and max runtime for each task.
- Require heartbeat updates for long-running tasks and expire stale tasks.
- Emit start/update/final audit events with usage metrics.

Task lifecycle states:
- `queued`, `running`, `waiting`, `completed`, `failed`, `timed_out`, `expired`, `cancelled`

Task behavior requirements:
- `queued` tasks are persisted before worker dispatch.
- `running` tasks must update heartbeat within configured interval.
- `waiting` state is used for monitor-style polling tasks.
- `expired` tasks are terminal when TTL is reached.
- Completion must trigger event/callback path for follow-up notification or action.

Result fields:
- `status`: completed, failed, timed_out, expired, cancelled
- `summary`: concise result text
- `artifacts`: structured attachments or references
- `usage`: token and duration data
- `safety_flags`: policy and guardrail outcomes

Async acknowledgement fields:
- `task_id`
- `accepted` (true/false)
- `rejection_reason` (when rejected)
- `expires_at`
- `next_check_at` (optional for monitor tasks)

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
- `config/channel.telegram.yaml`: bot token, webhook mode, allowlist.
- `config/model.yaml`: default model, model allowlist, routing/budget policies.
- `config/capabilities.yaml`: capability/skill/sub-agent capability rules.
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
| `config/channel.telegram.yaml` | `bot_token`, `allowlist` | `webhook_url`, `polling_interval_seconds` | `polling_interval_seconds=2` |
| `config/model.yaml` | `default_model_id`, `model_allowlist` | `quality_routing`, `max_tokens_default` | `quality_routing=quality_first` |
| `config/capabilities.yaml` | `allowed_capabilities` | `denied_capabilities`, `command_allowlist` | `denied_capabilities=[]` |
| `config/scheduler.yaml` | `tick_seconds`, `retry_policy` | `max_lateness_seconds` | `tick_seconds=10` |
| `config/store.yaml` | `backend`, `lock_ttl_seconds`, `atomic_write` | `idempotency_retention_seconds` | `backend=filesystem`, `atomic_write=true` |

Validation policy:
- Startup must fail when any required field is absent or invalid.
- Optional fields must receive documented defaults.
- Environment overrides must be validated using the same schema rules.

### Identifier Namespace Mapping

- `CMP_*`: architecture component IDs used for design traceability.
- `cap.*`: runtime capability IDs used for invocation and policy enforcement.
- Mapping rule: manifests should reference both where applicable:
  - `component_id` (implementation ownership),
  - `capability_id` (runtime authorization unit).

### Store Backend Strategy

- v1 backend: filesystem store adapters only.
- Required abstraction interfaces:
  - `SessionStoreInterface`
  - `TaskStoreInterface`
  - `IdempotencyStoreInterface`
  - `LockProviderInterface`
- Future backend option: Redis adapter implementing the same interfaces without changing domain logic.

Atomic write pattern (filesystem backend):
- Write content to temp file in same directory.
- Flush and fsync temp file.
- Atomic rename temp file to target path.
- Release lock only after successful rename.
- On failure:
  - keep prior target file untouched,
  - log failure and preserve temp artifact for cleanup/recovery scan.

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

- Health:
  - Liveness and readiness checks.
- Memory admin:
  - Browse memory documents by category and date.
  - Trigger memory consolidation.
- Capability/skill admin:
  - List loaded manifests and current enable/disable status.
  - Toggle capability/skill availability (subject to policy).
- Scheduler admin:
  - List, create, disable, and inspect scheduled jobs.
- Sub-agent audit:
  - List recent sub-agent runs and policy outcomes.

### Authentication and Authorization

- Single-admin token/shared secret for v1 admin surface.
- Role model can remain single-role in v1 but endpoint guards must be explicit.

### Rate Limiting and Throttling

- Telegram ingress should apply per-minute throttling to avoid accidental loops.
- Sub-agent spawn endpoint/path must enforce strict per-request and per-hour limits.

