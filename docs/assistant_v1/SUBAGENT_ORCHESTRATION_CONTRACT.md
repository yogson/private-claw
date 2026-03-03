# Sub-agent Orchestration Contract

## Overview

This document defines the authoritative contract for sub-agent spawning in Personal AI Assistant v1.

Component ownership:
- Coordinator: `CMP_AGENT_SUBAGENT_COORDINATOR`
- Parent caller: `CMP_CORE_AGENT_ORCHESTRATOR`
- Audit/event sink: `CMP_OBSERVABILITY_LOGGING`

Goals:
- Ensure explicit model selection for every sub-agent invocation.
- Enforce bounded execution and predictable costs.
- Prevent capability escalation relative to parent context.

## Contract Summary

Sub-agent execution is a policy-gated internal operation and is not a direct public end-user action.

The parent orchestrator can request a sub-agent only through `INT_SUBAGENT_SPAWN`.
The coordinator is the sole authority for:
- validating request structure,
- authorizing model and capability use,
- enforcing budget/concurrency/time limits,
- returning normalized results.

## Spawn Request Contract

Required fields:
- `task_id`: unique identifier scoped to parent request.
- `parent_trace_id`: correlation identifier for observability.
- `objective`: concise execution goal for sub-agent.
- `model_id`: explicit model identifier from allowlist.
- `max_tokens`: hard upper bound for token usage.
- `timeout_seconds`: hard wall-clock timeout.
- `allowed_capabilities`: explicit capability list for this run.
- `result_format`: expected output format/schema identifier.

Optional fields:
- `priority`: low, medium, high.
- `temperature`: model generation control.
- `context_refs`: references to memory/capability artifacts already approved.
- `retry_budget`: maximum retry attempts for transient provider failures.
- `requested_ttl_seconds`: preferred TTL; coordinator may clamp to policy limits.

Validation rules:
- `model_id` must exist in configured sub-agent model allowlist.
- `allowed_capabilities` must be subset of parent request capability scope.
- `timeout_seconds` must be within configured min/max bounds.
- `max_tokens` must not exceed per-task and per-day budget policy.
- `objective` must be non-empty and under configured length limits.
- Effective `expires_at` must be resolved for every accepted task (from request hint or policy default).

## Spawn Decision Policy

The coordinator authorizes or rejects requests via the following checks:

1. Schema validation check.
2. Parent permission and capability subset check.
3. Model allowlist check.
4. Budget check:
   - per-sub-agent token cap
   - per-parent-request budget cap
   - global rolling window budget cap
5. Concurrency check:
   - max concurrent sub-agents per parent request
   - max concurrent sub-agents globally
6. Safety risk check:
   - block high-risk capability combinations
   - require stricter limits for shell or side-effecting capabilities

Rejected requests return policy reasons and must not execute.

## Runtime Safety Controls

- Hard timeout kill for all sub-agent runs.
- Capability guard enforcement at every capability dispatch boundary.
- Read-only mode enforcement when requested by parent policy.
- Strict prohibition of direct filesystem/shell actions unless explicitly allowed.
- Mandatory correlation and audit logging for every state transition.

Lifecycle states:
- `queued`
- `running`
- `waiting`
- `completed`
- `failed`
- `timed_out`
- `expired`
- `cancelled`

Required transitions:
- `queued -> running|cancelled|expired`
- `running -> waiting|completed|failed|timed_out|expired|cancelled`
- `waiting -> running|completed|failed|expired|cancelled`

## Result Contract

Required fields:
- `task_id`
- `parent_trace_id`
- `status`
- `summary`
- `usage` (tokens, duration)
- `safety_flags`

Optional fields:
- `artifacts`: structured outputs and references.
- `error`: normalized error object when non-success.
- `provider_metadata`: provider-specific diagnostics.

Result invariants:
- `status=completed` must include non-empty `summary`.
- `status=failed|timed_out|expired|cancelled` must include normalized error details when a failure/cancellation reason exists.
- All results must include usage and trace metadata.

## Error Model

Policy and runtime errors:
- `SUBAGENT_SCHEMA_INVALID`
- `SUBAGENT_MODEL_NOT_ALLOWED`
- `SUBAGENT_CAPABILITY_ESCALATION`
- `SUBAGENT_BUDGET_EXCEEDED`
- `SUBAGENT_CONCURRENCY_LIMIT`
- `SUBAGENT_TIMEOUT`
- `SUBAGENT_PROVIDER_FAILURE`

Recovery behavior:
- `*_NOT_ALLOWED`, `*_ESCALATION`, `*_BUDGET_EXCEEDED`, `*_CONCURRENCY_LIMIT`: no retry.
- `*_PROVIDER_FAILURE`: bounded retry if retry budget available.
- `*_TIMEOUT`: fail and return partial diagnostics, no immediate auto-retry.

## Observability and Audit

Each sub-agent run must emit:
- `subagent.spawn.requested`
- `subagent.spawn.blocked|accepted`
- `subagent.run.started`
- `subagent.run.progress` (optional for long tasks)
- `subagent.run.waiting|completed|failed|timed_out|expired|cancelled`

Mandatory audit dimensions:
- `task_id`
- `parent_trace_id`
- `model_id`
- `allowed_capabilities`
- `budget_requested`
- `budget_used`
- `duration_ms`
- `status`

## Acceptance Criteria

- Coordinator rejects invalid or unauthorized spawn requests deterministically.
- Every accepted spawn includes explicit `model_id`.
- Capability escalation attempts are blocked and audited.
- Budget and concurrency limits are enforced under load.
- Timeout behavior is deterministic and leaves no orphaned worker execution.
- Parent request receives normalized result/error contract in all cases.

