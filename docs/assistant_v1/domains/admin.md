# Admin UI Domain

## Purpose

Define the minimal admin interface for operating and inspecting assistant runtime state in v1.

## Owned Components

- `CMP_ADMIN_MINIMAL_UI`

## Scope

- Display service health and runtime component status.
- Provide memory browsing and capability toggle controls.
- Provide scheduler and sub-agent audit views.
- Surface configuration/status diagnostics for operational troubleshooting.

## Inputs

- API responses from `CMP_API_FASTAPI_GATEWAY`.
- User actions from authenticated admin sessions.

## Outputs

- API requests to operational endpoints.
- UI-level operation history events.

## Constraints

- Keep UI operationally minimal for v1.
- Do not bypass API authorization or policy checks.
- Do not embed business logic beyond display and control orchestration.

## Risks

- Misleading status representations if API polling/state mapping is stale.
- Operational misuse if sensitive controls are not clearly scoped.

## Done Criteria

- Admin can inspect health, memory state, and scheduler/sub-agent status.
- Capability enable/disable controls work through API policy paths.
- Operational actions are reflected in auditable logs.

