# Assistant V1 Component Boundaries

## Purpose

Prevent responsibility drift and future code duplication by defining:
- canonical ownership per component/domain,
- explicit non-ownership boundaries,
- single source-of-truth rules for contracts and policies,
- change protocol when boundaries are updated.

This document is normative for boundary governance across the `docs/assistant_v1` docset.

## Boundary Principles (Normative)

1. One component/domain owns one behavior family. If two docs define behavior for the same family, one must be reduced to a reference.
2. Cross-domain invariants live in `ASSISTANT_V1_TECHNICAL_SPECIFICATION.md`; implementation behavior lives in domain docs.
3. Contract shape appears once as canonical schema; all other mentions are references plus invariants only.
4. Operational transport docs (`api`, `telegram`, `admin`) must not absorb orchestration/business logic.
5. Memory can guide routing but cannot substitute deterministic capability execution for high-risk actions.
6. Sub-agent execution state and user-facing task memory are separate model families and cannot be merged.

## Canonical Source Map

| Concern | Canonical Document | Notes |
|---|---|---|
| Cross-domain interfaces and global invariants | `docs/assistant_v1/ASSISTANT_V1_TECHNICAL_SPECIFICATION.md` | Interface IDs, required global constraints |
| Target decomposition and dependency structure | `docs/assistant_v1/ASSISTANT_V1_ARCHITECTURE_ANALYSIS.md` | Architecture intent and component graph |
| Delivery sequencing and quality gates | `docs/assistant_v1/ASSISTANT_V1_IMPLEMENTATION_PLAN.md` | Execution phases and readiness gates |
| Runtime capability IDs and catalog records | `docs/assistant_v1/CAPABILITY_CATALOG.md` | ID namespace and catalog scope |
| Sub-agent spawn/result/error contracts | `docs/assistant_v1/SUBAGENT_ORCHESTRATION_CONTRACT.md` | Authoritative shape and enforcement policy |
| Domain-specific execution behavior | `docs/assistant_v1/domains/*.md` | Component-owned runtime behavior |

## Cross-Domain Document Roles

- `ASSISTANT_V1_TECHNICAL_SPECIFICATION.md`:
  - owns interface contracts and global invariants,
  - must not duplicate domain-level implementation details.
- `ASSISTANT_V1_ARCHITECTURE_ANALYSIS.md`:
  - owns decomposition and dependency strategy,
  - must not own field-level runtime schemas.
- `ASSISTANT_V1_IMPLEMENTATION_PLAN.md`:
  - owns phased rollout and quality gates,
  - must not become a policy/contract source.
- `CAPABILITY_CATALOG.md`:
  - owns capability IDs and catalog record shape,
  - must not own runtime activation algorithms.
- `SUBAGENT_ORCHESTRATION_CONTRACT.md`:
  - owns sub-agent contract shape and coordinator decision contract,
  - must not absorb broader domain orchestration behavior.

## Ownership Matrix

### Agent (`CMP_CORE_AGENT_ORCHESTRATOR`)

- Owns:
  - turn lifecycle and route selection,
  - context assembly policy and token budgeting,
  - integration choreography across memory/capabilities/subagents.
- Must not own:
  - direct tool/shell/filesystem side effects,
  - HTTP transport contracts,
  - Telegram adapter transport concerns.

### Telegram (`CMP_CHANNEL_TELEGRAM_ADAPTER`)

- Owns:
  - Telegram update normalization,
  - allowlist enforcement at channel ingress,
  - outbound Telegram payload rendering and callback normalization.
- Must not own:
  - intent resolution or route selection,
  - memory retrieval logic,
  - capability/sub-agent policy decisions.

### Memory (`CMP_MEMORY_FILESYSTEM_STORE`)

- Owns:
  - memory artifact persistence and index lifecycle,
  - deterministic retrieval and scoring policy,
  - index repair/degradation fallback behavior.
- Must not own:
  - action execution decisions,
  - capability policy gating,
  - transport-facing API concerns.

### Capabilities (`CMP_TOOL_RUNTIME_REGISTRY`, `CMP_SKILL_RUNTIME_ENGINE`)

- Owns:
  - manifest discovery/validation,
  - runtime capability selection support and invocation policy checks,
  - command/template safety controls.
- Must not own:
  - long-term memory retrieval policy,
  - session persistence internals,
  - Telegram/API transport behavior.

### Subagents (`CMP_AGENT_SUBAGENT_COORDINATOR`)

- Owns:
  - spawn validation and authorization,
  - budget/concurrency/TTL enforcement,
  - asynchronous lifecycle tracking and result normalization.
- Must not own:
  - parent turn response synthesis,
  - direct channel response formatting,
  - mutation of parent capability policy model.

### Store (`CMP_STORE_STATE_FACADE` and internal store components)

- Owns:
  - session/task/idempotency/lock persistence interfaces,
  - atomic write semantics and lock TTL behavior,
  - startup recovery markers and consistency scans.
- Must not own:
  - business routing decisions,
  - memory scoring/retrieval logic,
  - API/UI interaction behavior.

### API (`CMP_API_FASTAPI_GATEWAY`)

- Owns:
  - HTTP request/response contracts,
  - authn/authz checks for admin operations,
  - transport-to-internal contract translation.
- Must not own:
  - orchestration business rules,
  - capability runtime logic,
  - scheduler decision policy.

### Admin (`CMP_ADMIN_MINIMAL_UI`)

- Owns:
  - operational visualization and control surface,
  - authenticated operator actions via API.
- Must not own:
  - backend policy execution,
  - direct datastore mutation outside API contracts.

### Observability (`CMP_OBSERVABILITY_LOGGING`)

- Owns:
  - structured event schema and correlation propagation,
  - audit record persistence requirements.
- Must not own:
  - remediation/business logic,
  - runtime control flow decisions.

## Overlap Hotspots and Resolution Rules

### 1) Sub-agent contracts duplicated across docs

- Risk:
  - `domains/subagents.md`, `ASSISTANT_V1_TECHNICAL_SPECIFICATION.md`, and `SUBAGENT_ORCHESTRATION_CONTRACT.md` all define overlapping fields/states.
- Rule:
  - `SUBAGENT_ORCHESTRATION_CONTRACT.md` owns field-level request/result/error schemas.
  - `domains/subagents.md` owns runtime behavior/lifecycle policy.
  - `ASSISTANT_V1_TECHNICAL_SPECIFICATION.md` keeps only global invariants and references.

### 2) Memory retrieval policy repeated in Agent and Memory docs

- Risk:
  - Retrieval triggers and scoring/caps can drift between orchestrator and memory sections.
- Rule:
  - `domains/memory.md` owns retrieval/index algorithm details.
  - `domains/agent.md` owns retrieval decision triggers and context assembly orchestration only.
  - Technical spec keeps global constraints (`no vector`, auditable bounded retrieval).

### 3) Capability policy overlap between catalog and domain spec

- Risk:
  - `CAPABILITY_CATALOG.md` and `domains/capabilities.md` can diverge on what is catalog vs runtime policy.
- Rule:
  - `CAPABILITY_CATALOG.md` owns capability IDs and catalog record shape.
  - `domains/capabilities.md` owns dynamic activation flow, manifest schema, and runtime policy enforcement.

### 4) API/Admin boundary blur

- Risk:
  - operational behavior leaking into UI or API docs as domain logic.
- Rule:
  - `domains/api.md` owns endpoint transport contract.
  - `domains/admin.md` owns operator UX behavior.
  - business execution logic remains in core domains (agent/memory/capabilities/subagents/store).

## Documentation Change Protocol

When updating architecture or behavior:

1. Identify canonical owner from this file before editing.
2. Edit canonical doc first.
3. In non-owner docs, replace duplicated content with:
   - a short invariant/reference summary,
   - link to canonical owner doc.
4. If ownership changes, update this file and `ASSISTANT_V1_TECHNICAL_SPECIFICATION.md` boundaries section in the same change.
5. If contract field names change, update all impacted interface references and acceptance criteria.

## PR Checklist (Anti-Drift)

- [ ] Each changed behavior family has exactly one canonical owner doc.
- [ ] No duplicated field-level contract definitions outside the canonical owner.
- [ ] Cross-domain invariants remain in technical spec and are still valid.
- [ ] Domain docs preserve "owns / must not own" boundary intent.
- [ ] Added capabilities or interfaces are reflected in the proper canonical source map.
