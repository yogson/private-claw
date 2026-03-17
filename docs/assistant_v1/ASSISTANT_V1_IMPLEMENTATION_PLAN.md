# ASSISTANT V1 Implementation Plan

## Overview

This roadmap defines the phased delivery of Personal AI Assistant v1 for single-user, single-host deployment. Implementation prioritizes a stable orchestration core, filesystem memory durability, safe extensibility, and pragmatic operations.

Core principles:
- Keep each phase bounded and testable.
- Enforce component-ID traceability across tasks and deliverables.
- Minimize coupling by introducing clear interfaces before feature expansion.
- Maintain deployment simplicity for `tmux`-managed runtime.
- Use `docs/assistant_v1/project_tracker.json` as the authoritative tracker for this feature scope.

Boundary governance requirement:
- All phase deliverables must follow `docs/assistant_v1/COMPONENT_BOUNDARIES.md`.
- If a phase changes ownership/contracts, update canonical owner docs first, then referencing docs.

## Parallel Development

Parallel opportunities after Phase 1 contracts are stable:
- Phase 2 store/runtime state work and Phase 3 Telegram channel work can proceed in parallel on shared interface contracts.
- Phase 4 memory and Phase 5 capability/sub-agent runtime can proceed in parallel once store/session contracts are stable.
- Scheduler endpoints in Phase 6 can start as read-only while job execution internals are finalized.

Parallel constraints:
- Config apply/hot-reload semantics must be defined before expanding admin mutating controls.
- Sub-agent spawn policy implementation depends on orchestrator, capability policy, and store task lifecycle contracts.
- Scheduler integration depends on store task persistence and memory maintenance contracts.

## Phase Details

### Phase 1: Core, Config System, and Admin Config Control Plane

- Risk Level: MEDIUM
- Priority: CRITICAL
- Objectives:
  - Establish runtime foundation and canonical configuration domains.
  - Deliver admin-first operational control for safe config management.
- Tasks:
  1. Implement application bootstrap with strict config loading/validation for `config/app.yaml`, `config/channel.telegram.yaml`, `config/model.yaml`, `config/capabilities.yaml`, `config/mcp_servers.yaml`, `config/scheduler.yaml`, `config/store.yaml` (`CMP_CORE_AGENT_ORCHESTRATOR`, `CMP_OBSERVABILITY_LOGGING`).
  2. Implement environment override projection with provenance metadata (`file`, `env_override`, `default`) and startup fail-fast validation policy (`CMP_CORE_AGENT_ORCHESTRATOR`).
  3. Implement FastAPI gateway baseline: health endpoints, admin auth guard, and config read/validate/diff/apply endpoints (`CMP_API_FASTAPI_GATEWAY`).
  4. Implement minimal admin UI config panels and workflow using server-rendered templates + HTMX (with lightweight UI kit) : load -> edit allowlisted keys -> validate -> diff preview -> confirm -> apply (`CMP_ADMIN_MINIMAL_UI`).
  5. Integrate Anthropic-first provider adapter baseline and correlation ID propagation (`CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER`, `CMP_OBSERVABILITY_LOGGING`).
- Dependencies: none
- Deliverables:
  - Runtime bootstrap module and validated config domain loader.
  - Effective-config API with redaction and provenance projection.
  - Admin config UI with controlled apply flow and audit trail.
  - Health endpoints and provider baseline call path with trace IDs.
- Success Criteria:
  - Service starts with validated configuration only.
  - Invalid config payloads fail safely without destructive persistence.
  - Effective-config projection is available with secret redaction and source provenance.
  - Admin config apply flow enforces validate/diff/confirm and records audit events.
  - Health endpoints report readiness and liveness consistently.
  - Baseline model invocation path returns successful responses.

### Phase 2: Store Foundation and Runtime State Integrity

- Risk Level: MEDIUM
- Priority: CRITICAL
- Objectives:
  - Establish durable persistence and concurrency primitives for all runtime domains.
  - Enforce idempotency and atomic state updates across sessions/tasks.
- Tasks:
  1. Implement `CMP_STORE_STATE_FACADE` and internal components for session/task/idempotency/lock persistence (`CMP_STORE_SESSION_PERSISTENCE`, `CMP_STORE_TASK_PERSISTENCE`, `CMP_STORE_IDEMPOTENCY_LEDGER`, `CMP_STORE_LOCK_COORDINATOR`).
  2. Implement atomic write semantics and lock TTL behavior with recovery markers (`CMP_STORE_STATE_FACADE`).
  3. Implement session history append/read/replay contracts for per-turn agent lifecycle (`CMP_STORE_SESSION_PERSISTENCE`).
  4. Implement idempotency registration/check path for channel and API ingress (`CMP_STORE_IDEMPOTENCY_LEDGER`).
- Dependencies:
  - Phase 1
- Deliverables:
  - Store facade APIs with filesystem backend baseline.
  - Session/task/idempotency/lock persistence modules.
  - Recovery marker and consistency scan routines.
  - Store-level diagnostics and audit events.
- Success Criteria:
  - Session/task state persists across restarts without corruption.
  - Duplicate ingress events are rejected deterministically.
  - Concurrent writes for the same session/task are lock-protected.
  - Atomic write failures preserve last good state and emit recovery diagnostics.

### Phase 3: Telegram Channel and Normalized Event Intake

- Risk Level: HIGH
- Priority: CRITICAL
- Objectives:
  - Enable Telegram-first assistant interaction with deterministic event normalization.
  - Support text, attachment, voice, and callback flows with transport safety.
- Tasks:
  1. Implement Telegram adapter ingress/egress with allowlist enforcement and outbound retry policy (`CMP_CHANNEL_TELEGRAM_ADAPTER`).
  2. Implement normalized inbound event mapping to `INT_ORCH_EVENT_INPUT` including callback and idempotency fields (`CMP_CHANNEL_TELEGRAM_ADAPTER`, `CMP_CORE_AGENT_ORCHESTRATOR`).
  3. Implement voice and attachment metadata handling with synchronous MTProto transcription intake and `transcript_text` mapping (`CMP_CHANNEL_TELEGRAM_ADAPTER`).
  4. Integrate per-channel throttling and channel audit logging (`CMP_CHANNEL_TELEGRAM_ADAPTER`, `CMP_OBSERVABILITY_LOGGING`).
  5. Implement Telegram recent-session listing and callback-based session resume selection flow (`CMP_CHANNEL_TELEGRAM_ADAPTER`, `CMP_CORE_AGENT_ORCHESTRATOR`, `CMP_STORE_SESSION_PERSISTENCE`).
- Dependencies:
  - Phase 1
  - Phase 2
- Deliverables:
  - Telegram adapter and response pipeline.
  - Event normalization and callback handling path.
  - Attachment/voice handlers with synchronous transcription attempt and fallback-state handling.
  - Channel-level throttling/retry and audit telemetry.
  - Interactive recent-session picker and active-session switch path for Telegram users.
- Success Criteria:
  - Authorized Telegram interactions complete reliably.
  - Normalized event schema remains stable for all supported Telegram event types.
  - Unauthorized users are blocked and audited.
  - Failed outbound sends follow configured retry behavior.
  - Telegram user can list latest sessions and resume selected session with signed callback validation.

### Phase 4: Filesystem Memory and Retrieval Quality

- Risk Level: MEDIUM
- Priority: HIGH
- Objectives:
  - Deliver durable long-term memory without database dependencies.
  - Ensure deterministic retrieval, deduplication, and maintenance behavior.
- Tasks:
  1. Implement markdown+frontmatter memory schemas and directory structure for profile/preference/project/task/fact/summary (`CMP_MEMORY_FILESYSTEM_STORE`).
  2. Implement deterministic retrieval/index pipeline with category caps and no-vector invariants (`CMP_MEMORY_FILESYSTEM_STORE`).
  3. Implement controlled write/update policy, confidence thresholds, and deduplication behavior (`CMP_MEMORY_FILESYSTEM_STORE`).
  4. Implement index rebuild/repair path and degraded fallback retrieval flow (`CMP_MEMORY_FILESYSTEM_STORE`, `CMP_OBSERVABILITY_LOGGING`).
- Dependencies:
  - Phase 1
  - Phase 2
  - Phase 3 (for production event shape)
- Deliverables:
  - Memory persistence module and schema validator.
  - Retrieval/index pipeline with bounded context outputs.
  - Update and deduplication policy implementation.
  - Memory repair/rebuild utilities and diagnostics.
- Success Criteria:
  - Memory persists across restarts with schema validity.
  - Retrieval provides relevant compact context under category caps.
  - Duplicate/conflicting artifacts are reduced by update/consolidation logic.
  - Index failures degrade gracefully with audit visibility.

### Phase 5: Capabilities, Skills, MCP Bridge, and Sub-agent Delegation

- Risk Level: HIGH
- Priority: HIGH
- Objectives:
  - Implement extensibility runtime with strict policy controls.
  - Enable bounded sub-agent delegation with explicit model selection.
- Tasks:
  1. Implement manifest discovery/validation and runtime registry lifecycle for capabilities and skills (`CMP_TOOL_RUNTIME_REGISTRY`, `CMP_SKILL_RUNTIME_ENGINE`).
  2. Implement dedicated memory operations capability and orchestrator wiring for memory proposal/apply flows (`CMP_TOOL_RUNTIME_REGISTRY`, `CMP_CORE_AGENT_ORCHESTRATOR`, `CMP_MEMORY_FILESYSTEM_STORE`).
  3. Implement remaining first-party capability set: web research, shell policy wrappers, macOS bridges, and `gh` integration (`CMP_TOOL_RUNTIME_REGISTRY`).
  4. Implement MCP bridge integration from server registry (`config/mcp_servers.yaml` + `plugins/mcp/*/tool_map.yaml`) with mapped-tool allowlisting and risk-class policy gates; reuse existing first-party tool mapping runtime (`CMP_TOOL_RUNTIME_REGISTRY`).
  5. Implement dynamic capability shortlist selection and descriptor injection model (top-N, policy pre-filtered) (`CMP_TOOL_RUNTIME_REGISTRY`, `CMP_CORE_AGENT_ORCHESTRATOR`).
  6. Implement sub-agent coordinator with schema validation, model allowlist, capability subset checks, budget/concurrency/TTL/heartbeat controls (`CMP_AGENT_SUBAGENT_COORDINATOR`).
  7. Implement parent-child trace and audit correlation for capability and sub-agent paths (`CMP_OBSERVABILITY_LOGGING`, `CMP_AGENT_SUBAGENT_COORDINATOR`).
- Dependencies:
  - Phase 1
  - Phase 2
  - Phase 4
- Deliverables:
  - Capability/skill runtime with policy enforcement.
  - Dedicated memory operations capability path integrated with orchestrator policy gates.
  - MCP bridge integration with mapped-tool allowlisting and safe tool activation path.
  - Sub-agent spawn/result/error contract enforcement implementation.
  - Audit history for capability and sub-agent executions.
- Success Criteria:
  - Capabilities/skills load deterministically and invalid manifests are rejected.
  - Memory operations run through dedicated capability contracts with auditable proposal/apply outcomes.
  - Per-turn tool context is shortlist-based, not full-catalog injection.
  - Mapped MCP tools remain deny-by-default until explicitly allowlisted and pass policy/confirmation gates.
  - Sub-agent requests enforce model/capability/budget/concurrency constraints and remain auditable.

### Phase 6: Scheduler, Reminder Lifecycle, and Operational Hardening

- Risk Level: MEDIUM
- Priority: MEDIUM
- Objectives:
  - Deliver reliable scheduled automation and reminder execution.
  - Finalize v1 readiness through admin operational hardening and runbooks.
- Tasks:
  1. Implement scheduler runtime and persisted jobs/history model (`CMP_AUTOMATION_SCHEDULER`, `CMP_STORE_TASK_PERSISTENCE`).
  2. Implement reminder CRUD lifecycle through orchestration, API, and Telegram response path (`CMP_AUTOMATION_SCHEDULER`, `CMP_API_FASTAPI_GATEWAY`, `CMP_CORE_AGENT_ORCHESTRATOR`).
  3. Implement retry, missed-run handling, and execution audit semantics (`CMP_AUTOMATION_SCHEDULER`, `CMP_OBSERVABILITY_LOGGING`).
  4. Integrate maintenance jobs for memory consolidation/index rebuild routines (`CMP_AUTOMATION_SCHEDULER`, `CMP_MEMORY_FILESYSTEM_STORE`).
  5. Expand admin operational panels: memory browser, capability/MCP toggles, sub-agent/scheduler audits, backup/export flows, and store runtime diagnostics/remediation controls backed by `StoreRuntimeManager` (`CMP_ADMIN_MINIMAL_UI`, `CMP_API_FASTAPI_GATEWAY`, `CMP_STORE_STATE_FACADE`).
  6. Define and validate `tmux` runtime runbook and smoke checklist (`CMP_OBSERVABILITY_LOGGING`).
- Dependencies:
  - Phase 1
  - Phase 2
  - Phase 3
  - Phase 4
  - Phase 5
- Deliverables:
  - Scheduler runtime with persisted jobs and history.
  - Reminder creation/update/list/cancel support.
  - Admin operational controls for runtime modules and MCP state, including store runtime diagnostics and recovery actions via `StoreRuntimeManager`.
  - Backup/export routines and documented restore process.
  - Deployment and operations runbook.
- Success Criteria:
  - Scheduled reminders fire within expected latency window.
  - Reminder lifecycle operations are durable across restart scenarios.
  - Admin can verify and control health, memory, capability/MCP, scheduler, sub-agent state, and store runtime health/remediation actions.
  - Backup/restore process is reproducible.
  - Smoke checks pass for critical user and scheduler paths.

## Quality Gates

- Gate 1 (after Phase 1): config domain validation, effective-config API, admin config apply workflow, health checks, and provider baseline all pass.
- Gate 2 (after Phase 2): store/session/idempotency/lock guarantees pass restart and concurrency tests.
- Gate 3 (after Phase 3): Telegram normalization, allowlist, and retry behavior pass channel acceptance checks.
- Gate 4 (after Phase 4): memory persistence/retrieval/index-repair behavior passes durability and quality checks.
- Gate 5 (after Phase 5): capability policy controls, MCP mapping gates, and sub-agent enforcement pass safety tests.
- Gate 6 (after Phase 6): scheduler reliability, admin operational controls, and runbook smoke suite pass.
- Gate 7 (cross-phase): boundary integrity check passes:
  - each behavior family has one canonical owner doc,
  - duplicated field-level contracts outside owner docs are removed,
  - cross-domain references and invariants remain consistent with technical spec.

## Success Metrics

- Telegram request success rate from allowlisted user exceeds target threshold in smoke and field usage.
- Persistent memory recall quality supports relevant context in repeated sessions.
- Capability safety controls block unauthorized command/capability paths.
- Config management flow prevents invalid runtime config writes while preserving auditability.
- MCP onboarding and mapped-tool activation remain deny-by-default until explicit policy approval.
- Sub-agent delegation remains within configured budget and concurrency limits.
- Scheduler delivers reminders within target lateness window.
- Recovery from process restart preserves memory, schedules, and operational visibility.

