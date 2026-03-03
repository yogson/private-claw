# Store Domain

## Purpose

Define the persistent state abstraction layer for v1, with filesystem as the primary backend and optional Redis-compatible backend in future phases without domain logic rewrites.

## Owned Components

- `CMP_STORE_STATE_FACADE`
- `CMP_STORE_SESSION_PERSISTENCE`
- `CMP_STORE_TASK_PERSISTENCE`
- `CMP_STORE_IDEMPOTENCY_LEDGER`
- `CMP_STORE_LOCK_COORDINATOR`

## Scope

- Provide a single store interface for session history, sub-agent tasks, scheduler state, and idempotency records.
- Enforce atomic write/read/update patterns across all runtime domains.
- Provide lock primitives for session-level and task-level concurrency control.
- Support startup recovery scans for incomplete or stale states.
- Enable backend swap from filesystem to Redis-backed adapters later.

## Inputs

- Session events from `CMP_CORE_AGENT_ORCHESTRATOR`.
- Task lifecycle updates from `CMP_AGENT_SUBAGENT_COORDINATOR`.
- Scheduler state updates from `CMP_AUTOMATION_SCHEDULER`.
- Idempotency keys from channel adapters (Telegram update processing).

## Outputs

- Persisted session transcripts and metadata.
- Persisted task state transitions and heartbeats.
- Persisted scheduler jobs/history.
- Idempotency check and registration responses.
- Lock acquisition/release outcomes.

## Constraints

- Filesystem backend is mandatory baseline for v1.
- All writes must be atomic and path-safe within application data root.
- Lock operations must be bounded by TTL to avoid deadlocks.
- Store interfaces must remain backend-agnostic to allow future adapters.

Atomic write pattern:
- Acquire lock for target key/path.
- Write payload to temporary file in same directory.
- Flush and fsync temporary file.
- Atomically rename temporary file over target path.
- Release lock only after rename success.

Partial failure handling:
- If temp write or fsync fails, keep previous target file unchanged.
- If rename fails, preserve temp artifact for cleanup/recovery scan.
- Emit store recovery marker and error audit event for operator visibility.

## Risks

- Race conditions without strict lock discipline.
- Partial writes during crashes if atomic patterns are not enforced.
- Backend divergence if interface contracts are not stable.

## Done Criteria

- Session and task persistence remains consistent across restarts.
- Duplicate Telegram updates are rejected through idempotency ledger.
- Concurrent writes to the same session/task are prevented by lock policy.
- Store interface supports filesystem implementation and clear future Redis adapter path.

