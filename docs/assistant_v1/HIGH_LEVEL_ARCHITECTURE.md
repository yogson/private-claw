# High Level Architecture

## Overview

This document provides a concise, implementation-oriented architecture view for Personal AI Assistant v1.
It is an orientation layer, not the canonical source for field-level contracts or delivery gates.

Primary implementation context:
- Single-user, single-host runtime.
- Python with `uv`, FastAPI, and Pydantic AI (Anthropic-first).
- Filesystem-first persistence (no database in v1).
- Telegram-first interaction with minimal admin surface.

Canonical references:
- Decomposition and dependency strategy: `docs/assistant_v1/ASSISTANT_V1_ARCHITECTURE_ANALYSIS.md`
- Cross-domain interfaces and invariants: `docs/assistant_v1/ASSISTANT_V1_TECHNICAL_SPECIFICATION.md`
- Delivery sequencing and quality gates: `docs/assistant_v1/ASSISTANT_V1_IMPLEMENTATION_PLAN.md`
- Capability IDs and catalog schema: `docs/assistant_v1/CAPABILITY_CATALOG.md`
- Sub-agent field-level contracts: `docs/assistant_v1/SUBAGENT_ORCHESTRATION_CONTRACT.md`
- Boundary governance: `docs/assistant_v1/COMPONENT_BOUNDARIES.md`

## Core Components

- `CMP_CORE_AGENT_ORCHESTRATOR`: turn lifecycle, context assembly, execution route selection.
- `CMP_CHANNEL_TELEGRAM_ADAPTER`: Telegram ingress/egress, allowlist checks, event normalization.
- `CMP_MEMORY_FILESYSTEM_STORE`: memory artifacts, deterministic retrieval, index lifecycle.
- `CMP_STORE_STATE_FACADE`: shared state abstraction for sessions, tasks, idempotency, locks.
- `CMP_TOOL_RUNTIME_REGISTRY`: capability discovery, policy checks, invocation runtime, MCP bridge.
- `CMP_SKILL_RUNTIME_ENGINE`: higher-level workflows composed from capability primitives.
- `CMP_AGENT_SUBAGENT_COORDINATOR`: policy-gated async delegation with model/budget/concurrency limits, using Claude Code agents as primary coding offload workers.
- `CMP_AUTOMATION_SCHEDULER`: reminders and maintenance jobs in single-timezone mode.
- `CMP_API_FASTAPI_GATEWAY`: health/admin transport contracts and authn/authz boundaries.
- `CMP_ADMIN_MINIMAL_UI`: operator controls and status views via API-only mutation paths.
- `CMP_OBSERVABILITY_LOGGING`: structured traces, audits, and correlation propagation.
- `CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER`: provider-facing inference adapter.

Store internal components:
- `CMP_STORE_SESSION_PERSISTENCE`
- `CMP_STORE_TASK_PERSISTENCE`
- `CMP_STORE_IDEMPOTENCY_LEDGER`
- `CMP_STORE_LOCK_COORDINATOR`

## Component Topology

```text
[Telegram] --> [CMP_CHANNEL_TELEGRAM_ADAPTER] --> [CMP_CORE_AGENT_ORCHESTRATOR] <-- [CMP_API_FASTAPI_GATEWAY] <-- [CMP_ADMIN_MINIMAL_UI]
                                                     |            |            \
                                                     |            |             +--> [CMP_OBSERVABILITY_LOGGING]
                                                     |            +----------------> [CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER]
                                                     +-----------------------------> [CMP_MEMORY_FILESYSTEM_STORE]
                                                     +-----------------------------> [CMP_STORE_STATE_FACADE] --> [Filesystem backend]
                                                     +-----------------------------> [CMP_SKILL_RUNTIME_ENGINE] --> [CMP_TOOL_RUNTIME_REGISTRY] --> [Local + MCP capabilities]
                                                     +-----------------------------> [CMP_AGENT_SUBAGENT_COORDINATOR] --> [Claude Code agents (primary) + other sub-agent workers]
                                                     +-----------------------------> [CMP_AUTOMATION_SCHEDULER]
```

## High-Level Runtime Flow

1. Inbound event arrives from Telegram, scheduler, API, or system.
2. Event is normalized to `INT_ORCH_EVENT_INPUT` and validated.
3. Orchestrator classifies turn and decides retrieval scope.
4. Memory/capability/sub-agent paths are selected under policy gates.
5. Response is emitted via channel/API and state is persisted atomically.
6. Structured audit/trace events are emitted for observability.

## Runtime and Configuration Model

- Runtime is managed as a single host/process group (for example `tmux`).
- Operational configuration domains:
  - `config/app.yaml`
  - `config/channel.telegram.yaml`
  - `config/model.yaml`
  - `config/capabilities.yaml`
  - `config/mcp_servers.yaml`
  - `config/scheduler.yaml`
  - `config/store.yaml`
- Startup is fail-fast for invalid required config.
- Effective config must preserve source provenance (`file`, `env_override`, `default`) and secret redaction.

## Delivery Sequence (Current v1 Plan)

1. Core runtime + config system + admin config control plane.
2. Store foundation (session/task/idempotency/lock integrity).
3. Telegram channel and normalized event intake.
4. Filesystem memory and retrieval quality.
5. Capabilities/skills/MCP bridge and sub-agent delegation.
6. Scheduler/reminder lifecycle and operational hardening.

## Key Constraints

- Boundary ownership must follow `docs/assistant_v1/COMPONENT_BOUNDARIES.md`.
- API/Admin remain transport and operator surfaces; no domain business logic.
- Memory guides routing but does not replace deterministic capability execution for high-risk actions.
- Sub-agent runtime state and user-facing task memory remain separate model families.
- Capability exposure to the model is shortlist-based per turn; no full catalog injection by default.

