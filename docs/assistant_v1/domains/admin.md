# Admin UI Domain

## Purpose

Define the operator-facing control plane for v1 to inspect runtime health, audit execution behavior, and manage configuration safely through API-backed workflows.

## Owned Components

- `CMP_ADMIN_MINIMAL_UI`

## UI Stack Baseline (v1)

Implementation baseline for `CMP_ADMIN_MINIMAL_UI`:
- Server-rendered HTML with FastAPI + Jinja templates.
- Incremental interactivity with HTMX for partial updates and form workflows.
- Lightweight CSS component kit (Tabler or Pico CSS) for consistent operator UI primitives.
- Optional Alpine.js for small local interactions (modal state, inline toggles, confirm prompts).
- Optional Chart.js only for targeted operational visualizations (for example scheduler/sub-agent trend charts).

Out of scope for v1 baseline:
- Full SPA architecture and frontend build pipeline.
- ORM/database-coupled admin frameworks.
- Client-side business logic that bypasses API validation and policy checks.

Selection rationale:
- Optimized for operational workflows (`validate -> diff -> confirm -> apply`) and auditability.
- Keeps complexity and maintenance burden low for single-user, single-host deployment.
- Preserves server-side policy enforcement as the primary control boundary.

## Scope

- Provide operational visibility:
  - service/process health,
  - memory/index status,
  - scheduler job state,
  - sub-agent lifecycle and policy outcomes,
  - capability and MCP runtime readiness.
- Provide bounded controls:
  - capability and skill enable/disable actions via policy-aware API routes,
  - scheduler create/pause/cancel operations,
  - maintenance actions (for example memory index rebuild trigger).
- Provide configuration management UX:
  - inspect effective config (base file + env override projection),
  - edit allowed config keys with validation, diff preview, and apply flow,
  - test high-risk integrations (for example MCP connectivity checks) before enablement.
- Provide operator audit UX:
  - recent control actions and outcomes,
  - policy rejection reasons,
  - last error context with trace/correlation references.

## Configuration Coverage Model (Normative)

Admin UI must cover operational configuration domains defined in `docs/assistant_v1/ASSISTANT_V1_TECHNICAL_SPECIFICATION.md` without duplicating backend validation logic.

| Config Domain | Primary File | Admin Surface | Editable in v1 | Notes |
|---|---|---|---|---|
| App runtime | `config/app.yaml` | Runtime mode, data root, timezone, log level | Partial | `data_root` edits require confirmation and restart warning |
| Telegram channel | `config/channel.telegram.yaml` | Allowlist, polling/webhook mode (token redacted) | Partial | Secrets never displayed in cleartext |
| Model routing | `config/model.yaml` | Default model, allowlist, routing mode | Partial | Must validate against provider/model allowlist |
| Capability policy | `config/capabilities.yaml` | Allowed/denied capability sets, command allowlist view | Yes | High-risk changes require explicit confirmation |
| MCP registry | `config/mcp_servers.yaml` | Server list, transport/connection metadata, enablement state | Yes | Credentials/headers are redacted and env-referenced only |
| Scheduler | `config/scheduler.yaml` | Tick cadence, retry policy, lateness thresholds | Yes | Warn on values that can cause overload |
| Store | `config/store.yaml` | Backend mode, lock TTL, idempotency retention | Partial | Backend swap controls are view-only in v1 if not filesystem |

Effective-config view rules:
- Show source provenance per value: `file`, `env_override`, or `default`.
- Display secrets as redacted placeholders while preserving whether a value is set.
- Show schema-validation errors inline before apply.

## Admin Configuration Workflow (Normative)

All configuration changes must follow a controlled, auditable workflow:

1. **Load** current effective config through API.
2. **Edit** only allowlisted mutable keys for v1.
3. **Validate** payload server-side against canonical config schema.
4. **Preview diff** (before/after, impacted domains, restart requirement flag).
5. **Confirm apply** for high-risk changes (policy, MCP, scheduler/store runtime knobs).
6. **Persist** via atomic config write path and emit audit event.
7. **Reconcile runtime**:
   - hot-reload where supported,
   - otherwise show explicit restart-required status.

Validation and apply failures must remain non-destructive (previous persisted config remains active).

## MCP Onboarding and Management Workflow (Normative)

Admin UI must support guided onboarding for external MCP servers aligned with `docs/assistant_v1/domains/capabilities.md`:

1. Register server identity and transport in `config/mcp_servers.yaml` (`server_id`, `transport`, `connection`, `enabled=false`).
2. Run connectivity and metadata probe before enabling.
3. Present discovered tools and require explicit allowlist selection.
4. Define or update tool mapping (`plugins/mcp/*/tool_map.yaml` equivalent API flow):
   - capability ID mapping (`cap.mcp.<server>.<tool>`),
   - risk class (`readonly`, `interactive`, `side_effecting`),
   - confirmation requirements.
5. Validate mapping against capability policy and naming rules.
6. Enable server and mapped tools only after successful validation.
7. Show runtime state:
   - connected/disconnected,
   - mapped tool count,
   - blocked tools with policy reason,
   - last probe/invocation status.

Safety requirements for MCP admin controls:
- Never expose raw credentials, transport command internals, or secret env values in UI.
- Deny-by-default for newly discovered tools until explicitly mapped and allowed.
- Require confirmation for enabling `interactive` and `side_effecting` tools.
- Keep per-tool and per-server disable controls available for emergency stop.

## Inputs

- API responses from `CMP_API_FASTAPI_GATEWAY`.
- Authenticated operator actions.
- System/audit state snapshots from capability, scheduler, memory, and sub-agent domains.

## Outputs

- API requests to operational endpoint groups.
- UI action audit events with trace correlation.
- Operator-visible diagnostics and remediation guidance.

## Constraints

- UI does not execute business logic or direct datastore mutation.
- API remains the only mutation path.
- UI must reflect policy gates and authorization outcomes exactly as returned by API.
- Keep v1 UX operationally compact; avoid deep workflow automation that bypasses confirmations.

## Risks

- Config drift if UI presents stale effective values during concurrent edits.
- Operator confusion if restart-required vs hot-reload behavior is unclear.
- Overexposure risk if secret redaction/provenance rendering is inconsistent.
- MCP misuse if onboarding skips explicit allowlisting and risk classification.

## Done Criteria

- Admin can inspect all required runtime statuses and audits from one surface.
- Config panels cover v1 operational files with editable/view-only boundaries enforced.
- Config edits use validate -> diff -> confirm -> apply flow with audit records.
- MCP server onboarding supports probe, allowlist mapping, policy checks, and safe enablement.
- Emergency disable actions for capabilities/MCP tools are available and auditable.

