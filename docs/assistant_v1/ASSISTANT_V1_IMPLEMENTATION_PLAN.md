# ASSISTANT V1 Implementation Plan

## Overview

This roadmap defines the phased delivery of Personal AI Assistant v1 for single-user, single-host deployment. Implementation prioritizes a stable orchestration core, filesystem memory durability, safe extensibility, and pragmatic operations.

Core principles:
- Keep each phase bounded and testable.
- Enforce component-ID traceability across tasks and deliverables.
- Minimize coupling by introducing clear interfaces before feature expansion.
- Maintain deployment simplicity for `tmux`-managed runtime.
- Use `docs/assistant_v1/project_tracker.json` as the authoritative tracker for this feature scope.

## Parallel Development

Parallel opportunities after Phase 1 contracts are stable:
- Phase 2 channel implementation and Phase 3 memory implementation can proceed in parallel.
- Phase 4 plugin runtime and Phase 5 scheduler can proceed in parallel once orchestrator interfaces are finalized.
- Admin API endpoints in Phase 6 can start as read-only while runtime modules mature.

Parallel constraints:
- Sub-agent spawn policy implementation depends on base orchestration and capability policy contracts.
- Scheduler integration depends on memory/task schema finalization.

## Phase Details

### Phase 1: Foundation and Runtime Skeleton

- Risk Level: MEDIUM
- Priority: CRITICAL
- Objectives:
  - Establish runtime foundation and system contracts.
  - Implement baseline observability and configuration validation.
- Tasks:
  1. Implement application bootstrap and configuration domains (`CMP_CORE_AGENT_ORCHESTRATOR`, `CMP_OBSERVABILITY_LOGGING`).
  2. Implement FastAPI gateway with health endpoints (`CMP_API_FASTAPI_GATEWAY`).
  3. Integrate Anthropic-first provider adapter using Pydantic AI (`CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER`).
  4. Define sub-agent spawn contract and policy schemas (`CMP_AGENT_SUBAGENT_COORDINATOR`).
- Dependencies: none
- Deliverables:
  - Runtime bootstrap module and validated config loader.
  - Operational health endpoints.
  - Provider adapter interface and baseline model call path.
  - Sub-agent contract schema and policy config templates.
- Success Criteria:
  - Service starts with validated configuration only.
  - Health endpoints report readiness and liveness consistently.
  - Baseline model invocation path returns successful responses.
  - Sub-agent contract schemas pass static and runtime validation checks.

### Phase 2: Telegram Channel and Multimodal Intake

- Risk Level: HIGH
- Priority: CRITICAL
- Objectives:
  - Enable Telegram-first assistant interaction.
  - Normalize text, attachment, and voice events for orchestration.
- Tasks:
  1. Implement Telegram adapter ingress/egress and authorization allowlist (`CMP_CHANNEL_TELEGRAM_ADAPTER`).
  2. Define normalized inbound event schema and routing contract (`CMP_CORE_AGENT_ORCHESTRATOR`).
  3. Implement attachment and voice metadata ingestion with fallback handling (`CMP_CHANNEL_TELEGRAM_ADAPTER`).
  4. Add channel-level throttling and retry behavior (`CMP_CHANNEL_TELEGRAM_ADAPTER`).
- Dependencies:
  - Phase 1
- Deliverables:
  - Telegram event adapter and response pipeline.
  - Channel normalization contract implementation.
  - Multimodal ingestion handlers.
  - Channel audit logs for inbound/outbound interactions.
- Success Criteria:
  - Authorized Telegram requests receive responses reliably.
  - Text, attachment, and voice events are normalized without schema drift.
  - Unauthorized users are blocked and audited.
  - Failed outbound sends are retried according to policy.

### Phase 3: Filesystem Memory System

- Risk Level: MEDIUM
- Priority: HIGH
- Objectives:
  - Deliver durable long-term memory without database dependencies.
  - Ensure memory retrieval quality and update consistency.
- Tasks:
  1. Implement markdown+frontmatter storage schemas and directory structure (`CMP_MEMORY_FILESYSTEM_STORE`).
  2. Implement memory retrieval strategies by category and recency (`CMP_MEMORY_FILESYSTEM_STORE`).
  3. Implement controlled memory write/update policy and deduplication (`CMP_MEMORY_FILESYSTEM_STORE`).
  4. Implement consolidation routine and retention rules (`CMP_MEMORY_FILESYSTEM_STORE`, `CMP_AUTOMATION_SCHEDULER`).
- Dependencies:
  - Phase 1
  - Phase 2 (for production conversation data shape)
- Deliverables:
  - Memory persistence module and schema validator.
  - Retrieval pipeline for profile/preferences/projects/tasks/facts/summaries.
  - Update and deduplication policy implementation.
  - Consolidation job and memory maintenance tasks.
- Success Criteria:
  - Memory persists across restarts with schema validity.
  - Retrieval provides relevant context to core responses.
  - Duplicate and conflicting entries are reduced by consolidation.
  - Memory write failures are handled with graceful degradation.

### Phase 4: Capabilities, Skills, and Sub-agent Delegation

- Risk Level: HIGH
- Priority: HIGH
- Objectives:
  - Implement extensibility framework with strict safety controls.
  - Enable bounded sub-agent spawning with explicit model selection.
- Tasks:
  1. Implement manifest discovery and registry lifecycle for capabilities/skills (`CMP_TOOL_RUNTIME_REGISTRY`, `CMP_SKILL_RUNTIME_ENGINE`).
  2. Implement first-party capabilities: memory management, web research, shell ops, calendar, macOS Notes/Reminders bridge, GitHub CLI (`gh`) integration (`CMP_TOOL_RUNTIME_REGISTRY`).
  3. Implement capability allowlists, command policy enforcement, and execution timeouts (`CMP_TOOL_RUNTIME_REGISTRY`).
  4. Implement sub-agent coordinator execution path with model allowlist, budget, and concurrency controls (`CMP_AGENT_SUBAGENT_COORDINATOR`).
  5. Implement parent-child trace/audit correlation for delegated tasks (`CMP_OBSERVABILITY_LOGGING`, `CMP_AGENT_SUBAGENT_COORDINATOR`).
- Dependencies:
  - Phase 1
  - Phase 3
- Deliverables:
  - Capability and skill manifest runtime with enable/disable controls.
  - First-party capability pack and safety wrappers.
  - Sub-agent spawn endpoint/path with enforcement policies.
  - Audit history for all capability and sub-agent executions.
- Success Criteria:
  - Capabilities and skills load/unload deterministically from manifests.
  - Restricted capabilities are blocked with explicit policy errors.
  - Sub-agent requests require explicit model selection and respect budgets.
  - Parent request remains stable when sub-agent invocation fails.

### Phase 5: Scheduler and Reminder Execution

- Risk Level: MEDIUM
- Priority: HIGH
- Objectives:
  - Deliver reliable reminder and maintenance automation in single timezone mode.
  - Integrate schedule lifecycle with Telegram and admin surfaces.
- Tasks:
  1. Implement scheduler runtime and persisted job model (`CMP_AUTOMATION_SCHEDULER`).
  2. Implement reminder CRUD flow via orchestration and API (`CMP_AUTOMATION_SCHEDULER`, `CMP_API_FASTAPI_GATEWAY`).
  3. Implement retry behavior, missed-run handling, and error audit (`CMP_AUTOMATION_SCHEDULER`, `CMP_OBSERVABILITY_LOGGING`).
  4. Integrate maintenance jobs for memory consolidation (`CMP_AUTOMATION_SCHEDULER`, `CMP_MEMORY_FILESYSTEM_STORE`).
- Dependencies:
  - Phase 2
  - Phase 3
- Deliverables:
  - Scheduler runtime with persisted jobs.
  - Reminder creation/update/list/cancel support.
  - Failure recovery policy and history logs.
  - Scheduled maintenance workflows.
- Success Criteria:
  - Scheduled reminders fire within expected latency window.
  - Reminder lifecycle operations are durable across restarts.
  - Failed runs are retried or marked failed according to policy.
  - Maintenance jobs execute without user-facing regressions.

### Phase 6: Minimal Admin and Operational Hardening

- Risk Level: MEDIUM
- Priority: MEDIUM
- Objectives:
  - Provide minimal but effective operational controls.
  - Finalize v1 readiness with smoke validation and runbooks.
- Tasks:
  1. Implement minimal admin UI and operational endpoints (`CMP_ADMIN_MINIMAL_UI`, `CMP_API_FASTAPI_GATEWAY`).
  2. Implement memory browser and capability toggle views/actions (`CMP_ADMIN_MINIMAL_UI`, `CMP_MEMORY_FILESYSTEM_STORE`, `CMP_TOOL_RUNTIME_REGISTRY`).
  3. Implement backup/export flows for memory and runtime audit artifacts (`CMP_MEMORY_FILESYSTEM_STORE`, `CMP_OBSERVABILITY_LOGGING`).
  4. Define and validate `tmux` runtime runbook and smoke checklist (`CMP_OBSERVABILITY_LOGGING`).
- Dependencies:
  - Phase 2
  - Phase 3
  - Phase 4
  - Phase 5
- Deliverables:
  - Minimal admin UI and secure operations endpoints.
  - Memory browse and capability control surface.
  - Backup/export routines and documented restore process.
  - Deployment and operations runbook.
- Success Criteria:
  - Admin can verify health and runtime state quickly.
  - Memory and capability operations are available from admin panel.
  - Backup/restore process is reproducible.
  - Smoke checks pass for critical user and scheduler paths.

## Quality Gates

- Gate 1 (after Phase 1): service start, health checks, provider adapter, and config validation all pass.
- Gate 2 (after Phases 2-3): Telegram interaction and memory persistence stable across restart tests.
- Gate 3 (after Phase 4): plugin safety controls and sub-agent policy enforcement validated.
- Gate 4 (after Phase 5): scheduler reliability and reminder lifecycle pass acceptance checks.
- Gate 5 (after Phase 6): operational readiness checklist and smoke test suite pass.

## Success Metrics

- Telegram request success rate from allowlisted user exceeds target threshold in smoke and field usage.
- Persistent memory recall quality supports relevant context in repeated sessions.
- Capability safety controls block unauthorized command/capability paths.
- Sub-agent delegation remains within configured budget and concurrency limits.
- Scheduler delivers reminders within target lateness window.
- Recovery from process restart preserves memory, schedules, and operational visibility.

