# Subagents Domain

## Purpose

Define background-task sub-agents that run independently from the main turn-based agent, with explicit model selection, lifecycle controls, TTL, and auditable execution.

## Owned Components

- `CMP_AGENT_SUBAGENT_COORDINATOR`
- `CMP_OBSERVABILITY_LOGGING` (delegation audit integration)

## Scope

- Validate sub-agent task creation requests.
- Enforce model allowlist, capability subset, budget, timeout, concurrency, and TTL limits.
- Launch and monitor asynchronous sub-agent workers.
- Persist task state transitions and heartbeat updates.
- Notify parent orchestration/event handlers on progress and completion.
- Record full delegation trace and usage metadata.

## Execution Model

- Main agent remains short-lived and never blocks on long-running sub-agent execution.
- Sub-agents are background tasks with a specific purpose and explicit completion criteria.
- Sub-agent tasks can be long-running until one of:
  - objective completed,
  - TTL expired,
  - task cancelled,
  - task failed permanently.

## Lifecycle State Machine

- `queued`: task accepted and waiting for worker slot.
- `running`: worker actively executing task logic.
- `waiting`: task is paused between checks/polls (for monitor-type tasks).
- `completed`: objective achieved successfully.
- `failed`: unrecoverable failure after retry policy.
- `timed_out`: run exceeded execution timeout.
- `expired`: TTL reached before completion.
- `cancelled`: explicitly cancelled by user/system.

Required transitions:
- `queued -> running|cancelled|expired`
- `running -> waiting|completed|failed|timed_out|expired|cancelled`
- `waiting -> running|completed|failed|expired|cancelled`

## Inputs

- Task creation requests from `CMP_CORE_AGENT_ORCHESTRATOR`.
- Runtime policy configuration (`model allowlist`, `budget`, `capabilities`, `ttl`).
- Optional trigger payloads (CI status source, market threshold, research objective).

## Outputs

- Immediate async acknowledgement (task accepted/rejected).
- Task result contract (`completed`, `failed`, `timed_out`, `expired`, `cancelled`).
- Progress and completion events for parent flow/notification handling.
- Delegation audit events and usage records.

## Constraints

- Every spawn must include explicit `model_id`.
- Capabilities must be subset of parent-permitted capabilities.
- No unbounded fan-out is allowed.
- Every task must include `expires_at` or derive it from policy default TTL.
- Long-running tasks must emit heartbeat updates.
- Task state must be persisted atomically to support restart recovery.

## Risks

- Cost spikes from excessive delegation.
- Capability escalation if policy checks are bypassed.
- Zombie tasks if heartbeat/TTL enforcement is missing.
- Duplicate side effects if retry behavior is not idempotent.

## Done Criteria

- Invalid or unsafe spawn requests are blocked deterministically.
- Accepted tasks execute within budget, timeout, and TTL limits.
- Background tasks survive process restarts with consistent persisted state.
- Progress/completion events can trigger follow-up actions or user notifications.
- Parent-child traceability exists for all task lifecycle transitions.

