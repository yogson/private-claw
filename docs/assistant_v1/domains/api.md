# API Gateway Domain

## Purpose

Define the FastAPI gateway boundary for operational and integration endpoints in v1.

## Owned Components

- `CMP_API_FASTAPI_GATEWAY`

## Scope

- Expose health, admin, scheduler, memory, and sub-agent audit endpoints.
- Validate authentication for admin operations.
- Translate HTTP payloads to internal orchestration/store contracts.
- Return normalized HTTP responses and error payloads.

## Inputs

- HTTP requests from admin UI and API clients.
- Internal service responses from orchestrator, scheduler, memory, and store domains.

## Outputs

- HTTP responses (success and normalized errors).
- Internal calls to orchestration and store interfaces.
- API-level audit events.

## Constraints

- Must not contain business logic; only transport and contract mapping.
- Must enforce admin token/shared-secret checks on protected routes.
- Must remain backward-compatible with documented v1 endpoint contracts.

## Risks

- Contract drift between API payloads and internal interfaces.
- Security regressions if endpoint guards are misconfigured.

## Done Criteria

- Endpoint contracts are documented and validated.
- Protected endpoints reject unauthorized requests.
- API-to-internal mapping is deterministic and audited.

