# ASSISTANT V1 Architecture Analysis

## Overview

This document analyzes the architecture for Personal AI Assistant v1 and defines the target decomposition for implementation. The system is designed as a single-user, single-host platform with Telegram as the primary interaction channel, file-system long-term memory, manifest-based extensibility, and controlled sub-agent spawning with explicit model selection.

Transformation goal: move from project intent and constraints to a componentized architecture that can be implemented incrementally with clear traceability.

## Document Boundaries

This document owns target decomposition, dependency structure, and architecture-level constraints.

This document does not own field-level runtime contracts or domain policy details. Those are owned by:
- `docs/assistant_v1/ASSISTANT_V1_TECHNICAL_SPECIFICATION.md` (cross-domain interfaces/invariants)
- `docs/assistant_v1/domains/*.md` (domain runtime behavior)
- `docs/assistant_v1/SUBAGENT_ORCHESTRATION_CONTRACT.md` (sub-agent contract schema)
- `docs/assistant_v1/CAPABILITY_CATALOG.md` (capability ID catalog)

Boundary governance reference:
- `docs/assistant_v1/COMPONENT_BOUNDARIES.md`

## Executive Summary

Key transformation goals:
- Define a robust orchestration core for multimodal Telegram and scheduled tasks.
- Introduce durable file-system memory without database dependencies.
- Establish safe extension points for capabilities, skills, and sub-agent delegation.
- Provide minimal operational surface through FastAPI and lightweight admin UI.

Architecture patterns:
- Central orchestrator pattern for inbound event handling.
- Adapter pattern for channels and provider integrations.
- Registry pattern for manifest-based capabilities and skills.
- Policy-gated delegation for sub-agent execution.
- Structured observability for operational diagnostics.

## Current Architecture Analysis

### Existing Components Assessment

Current state is a greenfield project with documentation conventions and implementation standards already defined in repository-level docs.

Strengths:
- Clear documentation and planning framework is already established.
- Strong component-ID traceability rule set is available.
- Explicit technology choices and constraints are already locked.
- Initial scope and MVP boundaries are documented and decision-ready.

Constraints:
- No runtime code implementation exists yet.
- No database allowed in v1, which constrains querying and history strategies.
- Single-host deployment with manual process management increases operational coupling.
- Security baseline is intentionally pragmatic for v1 and needs strict boundary enforcement.

### Dependency Analysis (Current-State Reality)

```text
[Project Intent + Requirements]
    |
    +--> [Technology Constraints]
    |       - Python + uv
    |       - FastAPI
    |       - Pydantic AI
    |       - Filesystem memory
    |
    +--> [Operational Constraints]
    |       - single host
    |       - tmux runtime
    |
    +--> [Product Constraints]
            - single user
            - Telegram-first interaction
            - minimal admin surface
```

Current-state dependency maturity:
- Interface contracts: not implemented
- Runtime modules: not implemented
- Data schemas: partially defined at concept level
- Operational runbook: not implemented

## Proposed Architecture

### Target Components and Responsibilities

- `CMP_CORE_AGENT_ORCHESTRATOR`: Conversation/session orchestration, context assembly, decision routing.
- `CMP_CHANNEL_TELEGRAM_ADAPTER`: Telegram ingress/egress, update normalization, attachment/voice intake metadata.
- `CMP_MEMORY_FILESYSTEM_STORE`: Markdown+frontmatter memory persistence, retrieval indexes, consolidation lifecycle.
- `CMP_STORE_STATE_FACADE`: Unified persistence abstraction for sessions, task states, idempotency records, and locks.
- `CMP_SKILL_RUNTIME_ENGINE`: High-level workflows composed from capabilities and memory access.
- `CMP_TOOL_RUNTIME_REGISTRY`: Manifest discovery, capability checks, invocation handling, execution isolation boundaries.
- `CMP_AGENT_SUBAGENT_COORDINATOR`: Parent-to-child task delegation, model selection enforcement, budget/time controls.
- `CMP_AUTOMATION_SCHEDULER`: Scheduled reminders/tasks with single-timezone behavior and persisted schedule state.
- `CMP_API_FASTAPI_GATEWAY`: Health, control, and admin endpoints.
- `CMP_ADMIN_MINIMAL_UI`: Read/operate interface for health, memory browsing, and capability toggles.
- `CMP_OBSERVABILITY_LOGGING`: Structured logs, correlation IDs, trace events, audit records.
- `CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER`: Anthropic-first model adapter for main and sub-agent paths.

Store facade internal decomposition (not top-level peers):
- `CMP_STORE_SESSION_PERSISTENCE`: Session history persistence and replay.
- `CMP_STORE_TASK_PERSISTENCE`: Sub-agent and scheduler task state persistence.
- `CMP_STORE_IDEMPOTENCY_LEDGER`: Duplicate event detection and retention.
- `CMP_STORE_LOCK_COORDINATOR`: Lock acquisition/release semantics with TTL.

### Target Dependency Tree

```text
CMP_CORE_AGENT_ORCHESTRATOR
├── CMP_CHANNEL_TELEGRAM_ADAPTER
├── CMP_MEMORY_FILESYSTEM_STORE
├── CMP_STORE_STATE_FACADE
├── CMP_SKILL_RUNTIME_ENGINE
│   └── CMP_TOOL_RUNTIME_REGISTRY
├── CMP_AGENT_SUBAGENT_COORDINATOR
│   └── CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER
├── CMP_AUTOMATION_SCHEDULER
├── CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER
└── CMP_OBSERVABILITY_LOGGING

CMP_API_FASTAPI_GATEWAY
├── CMP_CORE_AGENT_ORCHESTRATOR
├── CMP_MEMORY_FILESYSTEM_STORE
├── CMP_STORE_STATE_FACADE
└── CMP_OBSERVABILITY_LOGGING

CMP_ADMIN_MINIMAL_UI
└── CMP_API_FASTAPI_GATEWAY
```

### High-Level Runtime Flow

```text
Telegram/API/Scheduler Event
    -> CMP_CORE_AGENT_ORCHESTRATOR
    -> Context build (CMP_MEMORY_FILESYSTEM_STORE)
    -> Route:
         A) direct model inference
         B) skill/capability path
         C) sub-agent delegation path
    -> Response synthesis
    -> Channel output + memory updates + observability emission
```

### Architectural Constraints

- `CMP_MEMORY_FILESYSTEM_STORE` must remain authoritative for persisted memory in v1.
- `CMP_TOOL_RUNTIME_REGISTRY` must enforce explicit capability policies before invocation.
- `CMP_AGENT_SUBAGENT_COORDINATOR` must never run unbounded fan-out; limits are mandatory.
- `CMP_AUTOMATION_SCHEDULER` must treat all schedules as one configured timezone.
- `CMP_ADMIN_MINIMAL_UI` remains operationally minimal and not feature-complete.

## Implementation Strategy

### Architecture Decisions

- Use strict module boundaries with interface contracts per component ID.
- Build API and orchestration first, then memory, then extensions.
- Integrate sub-agent delegation as a bounded, policy-controlled path.
- Keep all persistent state human-auditable on filesystem.

### Evolution Strategy

Phase evolution:
1. Core runtime skeleton and health.
2. Telegram channel and multimodal normalization.
3. Durable memory and retrieval.
4. Capability/skill runtime, sub-agent contract hardening, and policy enforcement.
5. Scheduler and reminder delivery.
6. Minimal admin and operational hardening.

### Parallel Work Opportunities

- Telegram adapter and memory schema design can proceed in parallel after core contracts are fixed.
- Admin API surface and scheduler state model can proceed in parallel once orchestration interfaces are stable.
- Capability manifest schema and sub-agent policy schema can be developed concurrently.

## Risk Assessment

### High Risks

- Capability execution safety risk from shell and macOS CLI operations.
- Sub-agent budget/cost amplification risk under quality-first model strategy.
- Memory quality drift from over-aggressive extraction policies.

### Medium Risks

- Voice/attachment processing variance and failure handling complexity.
- Operational fragility due to manual `tmux` lifecycle and restart behavior.

### Mitigation Strategy

- Capability allowlists, command templates, and timeout-based execution guards.
- Per-task and per-day model budgets, spawn quotas, and allowlisted model IDs.
- Deterministic memory categories with write thresholds and periodic consolidation jobs.
- Graceful degradation for multimodal flows and event-level retry policy.
- Startup checks, health endpoints, and restart/runbook procedures.

