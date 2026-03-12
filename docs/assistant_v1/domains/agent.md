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

First-turn behavior for new sessions:
- First inbound user event is handled via the normal execution route (direct model / capability / sub-agent selection).
- The first user message is included in the LLM request context when the direct model path is selected.
- No hardcoded greeting-only short-circuit is used for new sessions.
- The resulting assistant response is persisted as regular conversation history, then the turn finalizes.

## Context Assembly and Routing Policy

The orchestrator must treat memory retrieval and capability loading as gated steps, not default "load everything" behavior.

### 1) Turn Classification (before retrieval)

Classify each inbound turn into one of:
- `chat_or_explanation`: user asks for discussion, explanation, planning, or brainstorming.
- `knowledge_or_preference_sensitive`: answer quality depends on user/project history, prior decisions, aliases, or stored preferences.
- `action_request_low_risk`: user requests bounded operational actions with limited side effects.
- `action_request_high_risk_or_multi_step`: user requests side-effecting workflows with ordered steps, waits, or production impact.

This classification controls whether memory retrieval is needed and whether capability path is mandatory.

### 2) Memory Retrieval Decision

Run memory retrieval when any of the following are true:
- Turn references prior commitments ("as we decided", "same as before", "my usual setup").
- Turn includes ambiguous entities that need project/user disambiguation.
- Response quality depends on preferences, long-lived facts, or active project/task state.
- Turn asks for continuity across sessions.

Skip or minimize memory retrieval when:
- Turn is self-contained and fully specified.
- Turn is generic factual Q&A with no user-specific context.
- Turn is high-risk execution where policy/capability checks dominate and memory should only provide hints.

Required retrieval behavior:
- Use deterministic retrieval pipeline from Memory domain.
- Request compact category-bounded memory bundle, not raw memory dump.
- Persist retrieval audit markers in turn metadata (selected IDs, scores, retrieval mode).

### 3) Capability Resolution and Loading

Capability loading has two phases:

- Startup phase:
  - Discover and validate manifests once.
  - Build in-memory registry of enabled capabilities and skills.
  - Reject invalid or duplicate manifests as startup errors.

- Turn phase:
  - Resolve required capability IDs from user intent plus memory hints.
  - Check capability policy allow/deny gates before invocation.
  - Load runtime entrypoint only for selected capabilities (lazy per-turn invocation), not for entire registry.

Capability path is required when:
- Action is side-effecting and non-trivial.
- Workflow is ordered, multi-step, or safety-critical.
- External state checks/waits are required.

If no appropriate capability exists for a high-risk intent:
- Do not execute from memory-only instructions.
- Ask for confirmation-limited fallback or refuse with actionable guidance to create/register capability.

### 4) Capability Selection Heuristics

When multiple capabilities are eligible, rank by:
- policy eligibility (must pass),
- exact intent-capability mapping from memory metadata,
- required input completeness from current turn,
- lower risk profile (prefer read-only when user intent allows),
- deterministic tie-breaker (`capability_id` lexical order or configured priority).

The selected capability and rejection reasons for alternatives must be auditable.

### 5) Focused Context Budgeting

The orchestrator must enforce strict context budgets to avoid prompt bloat:
- Always include: current user turn + minimal recent session window.
- Add memory artifacts only up to Memory domain category caps.
- Add capability metadata only for candidates in final shortlist.
- Exclude irrelevant historical tool outputs and stale low-confidence memory.

Recommended assembly order under token pressure:
1. Current user turn and immediate conversation state.
2. Safety/policy-critical capability constraints.
3. High-score memory artifacts.
4. Nice-to-have background summaries.

When projected tokens exceed budget:
- First compress summaries and remove low-score memory.
- Then reduce historical conversation window.
- Never drop policy constraints required for safe execution.

### 6) Anti-Overwhelm Rules

To keep reasoning tractable and responses concise:
- Keep candidate capabilities shortlist small (for example, top 1-3).
- Keep memory bundle compact and deduplicated.
- Prefer one execution path per turn unless user explicitly requests comparison.
- If ambiguity remains after retrieval/ranking, ask a narrow clarification question instead of loading more context.

### 7) End-to-End Decision Flow (Normative)

1. Classify turn.
2. Decide retrieval scope (none/minimal/standard) with explicit trigger reason.
3. Retrieve and score memory candidates (if triggered), apply caps.
4. Resolve capability candidates from intent + memory mappings.
5. Enforce policy gates and select capability (or direct-model path).
6. Assemble bounded prompt/context in priority order.
7. Execute route.
8. Persist response, audits, and any memory update intents.

## Cross-Domain Contracts

To coordinate Agent, Capabilities, and Memory domains safely:

- Agent -> Memory:
  - Sends retrieval query with intent hints/entities/tags and requested category caps.
  - Receives scored compact memory bundle plus retrieval audit data.

- Agent -> Capabilities:
  - Sends resolved intent, inputs, and candidate capability IDs.
  - Receives policy decision and normalized execution result/error.

- Memory -> Agent:
  - Provides contextual guidance only; never direct authority for high-risk execution.

- Capabilities -> Agent:
  - Provides deterministic execution with policy enforcement and auditable outcomes.

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

