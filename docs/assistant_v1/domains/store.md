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

## Canonical Models

| Model ID | Path Pattern | Required Fields | Purpose |
|---|---|---|---|
| `CMP_DATA_MODEL_SESSION_LOG` | `runtime/sessions/*.jsonl` | `session_id`, `sequence`, `event_id`, `turn_id`, `timestamp`, `record_type`, `payload` | Per-session append-only event log for replayable turns |
| `CMP_DATA_MODEL_IDEMPOTENCY_RECORD` | `runtime/idempotency/*.json` | `key`, `source`, `created_at`, `ttl_seconds` | Duplicate event prevention |
| `CMP_DATA_MODEL_LOCK_RECORD` | `runtime/locks/*.lock` | `lock_key`, `owner_id`, `acquired_at`, `expires_at` | Session/task lock coordination |
| `CMP_DATA_MODEL_STORE_RECOVERY_MARKER` | `runtime/recovery/*.json` | `component`, `last_scan_at`, `status` | Startup consistency and recovery tracking |

### `CMP_STORE_SESSION_PERSISTENCE` Record Contract

`CMP_DATA_MODEL_SESSION_LOG` is append-only JSONL and must be sufficient to reconstruct model-facing history on any later turn.

Required top-level fields:
- `session_id`: stable conversation/session identifier.
- `sequence`: strictly increasing integer per session (monotonic ordering key).
- `event_id`: unique event ID for idempotency and audit correlation.
- `turn_id`: identifier for the turn that produced the record.
- `timestamp`: ISO-8601 UTC timestamp.
- `record_type`: one of:
  - `user_message`
  - `assistant_message`
  - `assistant_tool_call`
  - `tool_result`
  - `system_message`
  - `turn_summary`
  - `turn_terminal`
- `payload`: record-type specific content.

Required payload fields by `record_type`:
- `user_message`:
  - `message_id`, `content`
  - optional: `attachments`, `source_event_id`
- `assistant_message`:
  - `message_id`, `content`
  - optional: `model_id`, `usage`, `finish_reason`
- `assistant_tool_call`:
  - `message_id`, `tool_call_id`, `tool_name`, `arguments_json`
- `tool_result`:
  - `message_id`, `tool_call_id`, `tool_name`
  - exactly one of: `result` or `error`
- `system_message`:
  - `message_id`, `content`, `scope` (`session` or `turn`)
- `turn_summary`:
  - `summary_text`
  - optional: `retrieval_audit`, `capability_audit`
- `turn_terminal`:
  - `status`: `completed`, `failed`, `cancelled`, `timed_out`, `interrupted`
  - optional: `error_code`, `error_message`

Invariants:
- `sequence` is assigned by store write path under session lock.
- `assistant_tool_call.tool_call_id` must be unique within a session.
- `tool_result.tool_call_id` must reference an existing `assistant_tool_call`.
- `turn_terminal` is at most one per `turn_id`.
- Persisted records must never be updated in place; only appended.

## `CMP_STORE_SESSION_PERSISTENCE` Operations

### Append Contract

- Input: list of records for one active `turn_id`.
- Preconditions:
  - session lock acquired (`INT_STORE_LOCK`).
  - idempotency check passed for ingress event.
- Behavior:
  - assign contiguous `sequence` values;
  - append records atomically to session JSONL;
  - fsync and rename via atomic write pattern;
  - release lock after successful append.
- Idempotency rule:
  - repeated append request with same (`session_id`, `event_id`) must not duplicate records.

### Read Contract

- `read_session(session_id)` returns full ordered log by `sequence`.
- `read_window(session_id, max_records)` returns most recent ordered suffix for bounded context assembly.
- Reads must tolerate trailing partial line artifacts:
  - ignore malformed trailing record,
  - emit recovery marker and diagnostics event.

### Replay Contract

`replay_for_turn(session_id, budget)` reconstructs model-facing history:
- include newest applicable `system_message` policy scope;
- include ordered `user_message`, `assistant_message`, `assistant_tool_call`, `tool_result`;
- include only turns with terminal record when available;
- for interrupted turns:
  - either synthesize terminal closure (`turn_terminal=status=interrupted`) during recovery,
  - or exclude incomplete suffix from model-facing replay until repaired.
- enforce context budget by dropping oldest complete turns first.

Replay correctness guarantees:
- no orphan `tool_result` without matching `assistant_tool_call`;
- no open tool calls in replayed suffix (unless marked interrupted and excluded);
- deterministic output for same persisted input and budget.

### Startup Recovery Contract

On process start:
- scan all session logs for malformed trailing lines, missing terminal records, and orphan tool-call/result pairs;
- write `CMP_DATA_MODEL_STORE_RECOVERY_MARKER` with scan status;
- for recoverable issues, append synthetic `turn_terminal(status=interrupted)` records and/or synthetic `tool_result(error=interrupted)` as configured by policy;
- never mutate or reorder existing valid records.

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
- v1 deployment target is local single-user, single-process runtime.
- Multi-worker or multi-process coordination guarantees are out of scope for v1 filesystem backend and are planned for future backend upgrades (for example Redis-backed adapters).
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
- Session append/read/replay behavior is deterministic for per-turn lifecycle continuity.
- Tool-call/tool-result history can be reconstructed without orphan edges after restart or interruption.

