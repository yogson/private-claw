# Agent Domain

## Purpose

Define the main orchestration domain that receives normalized events, builds context, chooses execution paths, and returns user-facing responses.

The v1 execution model is turn-based and in-process: handle exactly one inbound message/event per execution cycle, persist results, and complete the cycle. The next user message triggers a new in-process cycle that reloads session history and continues.

## Owned Components

- `CMP_CORE_AGENT_ORCHESTRATOR`
- `CMP_PROVIDER_LLM_ANTHROPIC_ADAPTER`
- `CMP_OBSERVABILITY_LOGGING` (integration)

## Scope

- Resolve intent from normalized events.
- Build execution context from memory and runtime state.
- Reload session history for the active session on every turn.
- Route execution through:
  - direct model path,
  - skill/capability path,
  - sub-agent path.
- Synthesize and return final responses.
- Persist turn artifacts (messages, metadata, memory intents) before cycle completion.
- Emit trace and audit events with correlation IDs.

## Turn Lifecycle

1. Inbound event arrives (user message, command, or scheduled trigger).
2. Agent turn-execution context is initialized for one turn.
3. Session history is loaded from persistent storage.
4. Context is assembled from history plus memory.
5. Agent executes route (direct model, skill/capability, optional sub-agent).
6. Response is emitted to channel.
7. Turn state is persisted atomically.
8. Turn-execution context is finalized; runtime remains active and awaits the next inbound event.

Greeting behavior for new sessions:
- First session event triggers a greeting response.
- Greeting turn is persisted as regular conversation history.
- Turn-execution context finalizes after persistence.

## Inputs

- Normalized events from channel adapters and scheduler.
- Persisted session history for the active session.
- Memory retrieval results.
- Capability/skill outputs.
- Sub-agent results.

## Outputs

- Channel response payloads.
- Persisted turn transcript and metadata.
- Memory update intents.
- Sub-agent spawn requests.
- Structured observability events.

## Constraints

- Must remain orchestration-only; no direct filesystem or shell side effects.
- Must enforce capability and policy gates before invoking capabilities/sub-agents.
- Must degrade gracefully when downstream providers/capabilities fail.
- Must process one turn per execution cycle and then finalize that cycle.
- Must use idempotency checks for duplicate inbound events.
- Must prevent concurrent writes to the same session (session-level lock).
- Must persist turn state atomically before cycle completion.

## Risks

- Routing complexity as feature count grows.
- Context bloat affecting latency and response quality.
- Duplicate event delivery causing repeated responses without idempotency.
- Session corruption risk without locking and atomic write semantics.

## Done Criteria

- Orchestrator consistently handles all inbound event types.
- Execution route selection is deterministic and auditable.
- Response generation remains functional during partial subsystem failures.
- Every turn reloads persisted history and finalizes after successful persistence.
- Duplicate inbound events are ignored safely.
- Session history remains consistent across restarts and failures.

