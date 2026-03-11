# API Gateway Domain

## Purpose

Define the FastAPI gateway boundary for operational and integration endpoints in v1.

## Owned Components

- `CMP_API_FASTAPI_GATEWAY`

## Scope

- Expose health, admin, scheduler, memory, and sub-agent audit endpoints.
- Validate authentication for admin operations.
- Translate HTTP payloads to internal orchestration/store contracts.
- Provide ingress idempotency integration for API-triggered events before orchestration dispatch.
- Return normalized HTTP responses and error payloads.

## Required Endpoint Groups (v1)

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
- Store runtime admin (`StoreRuntimeManager`-backed):
  - Read store statistics, lock diagnostics/contention, and recovery summary/history.
  - Trigger store recovery scan and expired-resource cleanup.
  - Execute emergency lock remediation (`force_release_lock`) with auditable operator context.

## Inputs

- HTTP requests from admin UI and API clients.
- Internal service responses from orchestrator, scheduler, memory, and store domains (including `StoreRuntimeManager` runtime operations).

## Outputs

- HTTP responses (success and normalized errors).
- Internal calls to orchestration and store interfaces.
- API-level audit events.

## Constraints

- Must not contain business logic; only transport and contract mapping.
- Must enforce admin token/shared-secret checks on protected routes.
- Must apply store-backed idempotency checks for API ingress events carrying idempotency keys.
- Must remain backward-compatible with documented v1 endpoint contracts.

## Risks

- Contract drift between API payloads and internal interfaces.
- Security regressions if endpoint guards are misconfigured.

## Done Criteria

- Endpoint contracts are documented and validated.
- Protected endpoints reject unauthorized requests.
- API-to-internal mapping is deterministic and audited.

