# High Level Architecture

## Overview

This document defines the architecture for a personal AI assistant platform v1 designed for a single user, single-host deployment. The system provides Telegram-based interaction (text, attachments, and voice notes), long-term memory persisted in the file system, scheduled automations, a minimal web admin surface, and an extensible manifest-based capability and skill runtime.

The architecture prioritizes:
- Fast shipping with clear module boundaries
- Reliable local operation without external database dependencies
- Safe extensibility for capabilities and skills
- Agent/sub-agent orchestration with explicit model selection controls

## Executive Summary

The platform is organized around a central orchestrator that normalizes inbound events from Telegram and the admin/API surface, builds working context from memory, then routes execution through either direct model calls, skills, capabilities, or spawned sub-agents.

Primary subsystem goals:
- `CMP_CORE_AGENT_ORCHESTRATOR`: Single orchestration entrypoint for all interactions.
- `CMP_CHANNEL_TELEGRAM_ADAPTER`: Telegram protocol integration and event normalization.
- `CMP_MEMORY_FILESYSTEM_STORE`: Persistent memory with markdown+frontmatter schemas.
- `CMP_STORE_STATE_FACADE`: Unified persistence abstraction for sessions, tasks, idempotency, and locks.
- `CMP_TOOL_RUNTIME_REGISTRY`: Dynamic loading and guarded execution of manifest capabilities.
- `CMP_SKILL_RUNTIME_ENGINE`: Reusable higher-level capability workflows built atop capability primitives.
- `CMP_AGENT_SUBAGENT_COORDINATOR`: Controlled fan-out execution to model-specific sub-agents.
- `CMP_AUTOMATION_SCHEDULER`: Timezone-bound recurring and one-off reminders/tasks.
- `CMP_API_FASTAPI_GATEWAY`: Operational endpoints and minimal admin backing API.
- `CMP_ADMIN_MINIMAL_UI`: Lightweight operation and observability UI.
- `CMP_OBSERVABILITY_LOGGING`: Structured logs, correlation IDs, and audit trail.

Store facade internal components (implementation details under `CMP_STORE_STATE_FACADE`):
- `CMP_STORE_SESSION_PERSISTENCE`
- `CMP_STORE_TASK_PERSISTENCE`
- `CMP_STORE_IDEMPOTENCY_LEDGER`
- `CMP_STORE_LOCK_COORDINATOR`

High-level interaction model:
1. Inbound request enters from Telegram or API.
2. Orchestrator resolves user intent and required capability path.
3. Memory retrieval and policy checks are applied.
4. Work is executed by direct model inference, skill+capability chain, or sub-agent delegation.
5. Results are persisted (when needed), emitted to user channel, and logged.

## Proposed Architecture

### Component Topology

```text
[Telegram User]
      |
      v
[CMP_CHANNEL_TELEGRAM_ADAPTER]
      |
      v
[CMP_CORE_AGENT_ORCHESTRATOR] <---- [CMP_API_FASTAPI_GATEWAY] <---- [CMP_ADMIN_MINIMAL_UI]
      |        |         |                ^
      |        |         |                |
      |        |         +-------- [CMP_AUTOMATION_SCHEDULER]
      |        |
      |        +------> [CMP_AGENT_SUBAGENT_COORDINATOR] ---> [Sub-agent Workers (model-selected)]
      |
      +------> [CMP_STORE_STATE_FACADE] ---> [Filesystem Backend (v1)]
      |
      +------> [CMP_SKILL_RUNTIME_ENGINE] ---> [CMP_TOOL_RUNTIME_REGISTRY] ---> [Local/External Capabilities]
      |
      +------> [CMP_MEMORY_FILESYSTEM_STORE]
      |
      +------> [LLM Provider Adapter (Anthropic-first)]
      |
      +------> [CMP_OBSERVABILITY_LOGGING]
```

### Runtime and Deployment

- Single process group on one host, managed via `tmux`.
- FastAPI service exposes admin/API endpoints and optional webhook endpoint.
- Telegram integration supports either webhook mode or polling mode (configurable).
- Scheduler runs in-process with persisted schedule state on filesystem.
- Memory and runtime metadata are stored in a predictable directory layout under an application data root.

### Data Domains

- Conversation domain: inbound/outbound messages and normalized events.
- Memory domain: profile, preferences, projects/tasks, facts, and summaries.
- Store domain: session persistence, task state, idempotency ledger, locks.
- Capability/skill domain: manifests, capabilities, permissions, runtime execution records.
- Automation domain: schedules, next-run metadata, completion/outcome events.
- Observability domain: traces, structured logs, and auditable actions.

### Sub-agent Delegation Pattern

`CMP_CORE_AGENT_ORCHESTRATOR` may delegate bounded tasks to `CMP_AGENT_SUBAGENT_COORDINATOR` when:
- A task is parallelizable or specialized.
- A distinct model profile is required for quality or speed.
- Safety policy allows the requested capability set.

Sub-agent executions are governed by:
- Explicit model identifier
- Time and token budgets
- Capability allowlists
- Parent-child trace correlation

## Implementation Strategy

### Guiding Principles

- Single responsibility per module and strong contracts between components.
- Configuration-first behavior with startup validation.
- File-system durability for all essential state.
- Manifest-driven extension points for capability/skill growth.
- Policy gates at orchestration boundaries (security, budget, capability).

### Incremental Delivery Strategy

1. Establish base runtime skeleton and configuration contracts.
2. Deliver Telegram channel and core orchestration loop.
3. Introduce filesystem memory and retrieval/write policies.
4. Add capability/skill runtime with manifest loading and permission guards.
5. Implement scheduler and reminder flows.
6. Add minimal admin and operational hardening.
7. Introduce sub-agent delegation with model-selection guardrails.

### Technology Baseline

- Language/runtime: Python with `uv` package/environment workflow.
- API framework: FastAPI for operational and integration endpoints.
- AI framework: Pydantic AI with Anthropic-first provider strategy.
- Persistence: File system only (no database in v1).
- Operations: `tmux` process management and local health checks.

## Risk Assessment

### Architectural Risks

- Lack of database may increase complexity for querying historical state at scale.
- Shell/macOS-integrated capabilities increase operational and security sensitivity.
- Voice and file ingestion paths may introduce heavier processing latency.
- Sub-agent fan-out may increase cost and unpredictability without strict controls.

### Mitigations

- Keep memory model strongly typed and path-constrained with deterministic indexing.
- Enforce capability permission policies, command allowlists, and execution timeouts.
- Use staged ingestion and graceful fallback for multimodal content.
- Require explicit sub-agent model choice and enforce per-task execution budgets.
- Apply structured logs with correlation IDs for post-incident diagnostics.

