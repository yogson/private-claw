# Agent Domain

## Purpose

Define the main orchestration domain that receives normalized events, builds context, chooses execution paths, and returns user-facing responses.

The v1 execution model is turn-based and in-process: handle exactly one inbound message/event per execution cycle, persist results, and complete the cycle. The next user message triggers a new in-process cycle that reloads session history and continues.

## Owned Components

- `CMP_CORE_AGENT_ORCHESTRATOR`
- `CMP_CORE_CONFIRMATION_SERVICE`
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

## Memory Update Intent Capture (Normative)

Memory writes are orchestrator-owned side effects. The model may propose updates, but it must not write memory directly.

### Proposal Tool Contract (Primary Path)

Memory update proposals should be emitted through a dedicated capability/tool call:

- `memory_propose_update`

The tool call arguments should follow:

```json
{
  "intent_id": "uuid-or-deterministic-hash",
  "action": "upsert|delete|touch",
  "memory_type": "profile|preferences|projects|tasks|facts|summaries",
  "memory_id": "optional-existing-id",
  "candidate": {
    "tags": ["optional", "tags"],
    "entities": ["optional", "entities"],
    "priority": 5,
    "confidence": 0.9,
    "body_markdown": "normalized markdown content"
  },
  "reason": "why this memory update is proposed",
  "source": "explicit_user_request|agent_inferred|capability_output|scheduler",
  "requires_user_confirmation": true
}
```

Notes:
- `action=upsert` allows create-or-update behavior in Memory domain.
- `action=touch` updates usage metadata only (for example `last_used_at`).
- `requires_user_confirmation` is ignored: every memory proposal requires user approval (see "User Confirmations").
- `memory_propose_update` accepts proposals only; it does not perform direct memory writes.

### Strict Tool-Call Path

Memory proposals are accepted only through native tool calling.

- The orchestrator must expose `memory_propose_update` as a model-visible tool descriptor.
- Memory intents encoded in plain assistant text (including JSON-like blobs) must not trigger writes.
- If model output is malformed for a tool route, the turn remains side-effect free and must emit diagnostics.

### How Intent Is Caught

The orchestrator captures intents in this order:

1. provider-native `tool_call` for `memory_propose_update`,
2. deterministic fallback parser for explicit user commands only (for example "remember this", "forget X"), producing the same schema.

The system should avoid free-form natural language scraping of assistant prose for writes.

### Apply Pipeline

1. The tool validates and normalizes the proposal into a `MemoryUpdateIntent` (invalid -> `rejected_invalid`).
   A body longer than `MEMORY_BODY_MAX_UTF16` (2500 UTF-16 code units, as Telegram counts; an emoji is 2) is
   also `rejected_invalid`: the user must see the full text they approve, so it is never shown truncated. In the
   summary, `reason` is capped at 300 chars and `memory_id` at 200.
2. It registers a `memory_update` confirmation request (see "User Confirmations") and returns
   `pending_confirmation` (with `confirmation_id`, `deduplicated`, `note`) or `another_confirmation_pending`.
3. The call and its result are persisted inline with the turn like any other tool call, so later turns see them.
4. Nothing is written until the user presses Approve; the write then goes through `MemoryUpdateHandler`.
   The kind's code (draft, body limit, handler) lives with the memory extension
   (`assistant.extensions.first_party.memory.confirmation`); `core.confirmation` stays kind-agnostic.

Idempotency rule:
- The writer intent id is `confirmation:<confirmation_id>`; one confirmation is applied at most once.

## User Confirmations (Normative)

`CMP_CORE_CONFIRMATION_SERVICE` (`assistant.core.confirmation`) is the single mechanism for actions that need an
explicit user decision. Kinds: `memory_update` (memory proposals) and `capability_edit` (agent-proposed capability
manifest edits and rollbacks; see `domains/capabilities.md` "Agent-Driven Manifest Editing"). Other kinds plug in by
registering a handler and building a draft with a `summary_for_user` (a summary, not a full diff). A kind can
also attach content that the user must receive in full before deciding (`attachments`, below).

Request model (`ConfirmationRequest`): `confirmation_id`, `kind`, `session_id`, `origin_tool_call_id`, `payload`,
`summary_for_user`, `dedupe_key`, `attachments`, `status` (`pending|approved|rejected|expired|failed`), `created_at`,
`expires_at`, `resolved_at`, `result` (`ok`, `status`, `detail`), `ui_ref` (`chat_id`, `message_id`).

Attachments (`ConfirmationAttachment`: `title`, `parts`): optional, empty for `memory_update`. `capability_edit` uses
one for a prompt change: the full prompt diff, see `domains/capabilities.md`. Core produces the content, already
split into ordered parts of at most `ATTACHMENT_PART_MAX_UTF16` (3700) UTF-16 units, with a one-line title of at
most `ATTACHMENT_TITLE_MAX_UTF16` (120). The model rejects anything larger, so content is split and never truncated;
`summary_for_user`'s cap does not apply to it. A channel only renders and delivers. It must deliver every part
before the buttons, bind the buttons visibly to the parts, and never show usable buttons unless every part was
delivered. Any part that fails makes the confirmation undelivered (below). Attachments are not written to the
history record (the record carries the summary). On resolve or expiry only the button message is edited.

Rules:
- State is in memory only. After a restart unknown ids resolve as "no longer valid". When the user presses a
  button of such a confirmation, its signature proves which session issued it, so an `expired` record is written
  to that session (`ConfirmationService.record_unknown_expired`).
- Pending in history but no longer awaiting an answer (e.g. pending at restart and never pressed, or proposed in a
  turn that was stopped with `/stop`): at turn start, after `expire_due(session_id)` and before the agent runs, the
  orchestrator scans the replayed tool results whose `status` is `pending_confirmation`
  (`unresolved_pending_ids`). Every such `confirmation_id` without a `confirmation-<id>` record is reconciled
  (`ConfirmationService.reconcile_pending`). An id the service no longer knows gets an `expired` record
  ("no longer awaiting an answer (the assistant restarted or the turn ended before it was resolved) ... propose it
  again if still needed"). A known id that is already final but was never recorded gets its final record. A
  still-pending id is left alone. History is then re-read, so the same turn sees the outcome. The store deduplicates
  records by event id, so a later press of the old button adds no second record.
  Only results of the first-party confirmation-creating tools in `CONFIRMATION_TOOL_NAMES`
  (`assistant.core.confirmation.history`; currently `memory_propose_update`, `capability_propose_edit` and
  `capability_propose_rollback`) are scanned, and only ids in the
  service's own format (16 lowercase hex chars, `is_confirmation_id`). Output of any other tool (MCP servers, fetched
  JSON, external APIs) is untrusted and may use the same shape; it must never become a trusted resolution record.
  Every new confirmation-creating tool (e.g. capability edits) must be added to `CONFIRMATION_TOOL_NAMES`.
  Two guards enforce this. First, the tool sets `ConfirmationDraft.origin_tool_name` from `ctx.tool_name`, and the
  orchestrator's requester refuses a draft from any tool outside the set: it logs
  `orchestrator.confirmation_tool_not_registered` and raises, so no confirmation is created. Second,
  `tests/assistant/core/confirmation/test_tool_registry.py` finds every tool that reads `confirmation_requester` or
  returns `pending_confirmation`, and fails if one is missing from the set.
- The tool-mismatch guard (warn instead of running the LLM when the most recent turn called tools that are no
  longer active) skips synthetic `confirmation-<id>` turns when picking the most recent turn. A resolution record,
  whether from a button press or written by turn-start reconciliation, is not a model turn and does not change
  whether the user gets the warning.
- TTL: `app.confirmation_ttl_seconds` (default 86400). The orchestrator calls `expire_due(session_id)` before each turn
  reads history, so a timed-out confirmation's `expired` record is visible to that turn. Expiry is also checked on
  request/resolve. Resolved entries are pruned one TTL after resolution (on request, resolve and sweep).
- `request()` returns the existing pending request when the `dedupe_key` matches; at most one confirmation is pending
  per session, a different proposal while one is pending is not registered (`blocked_by`).
- `resolve()` is idempotent: a per-id `asyncio.Lock` plus compare-and-set on `pending`; racing or repeated
  decisions are no-ops that return the existing final state.
- Approval runs the per-kind handler (`ConfirmationHandlerInterface.apply`). For `memory_update` only
  `written|updated|deleted|touched` writer statuses count as applied; anything else is `failed` with the writer
  reason. The blocking writer runs in `asyncio.to_thread`. For `capability_edit` only a written manifest that the
  catalog serves counts as `applied`; `stale`, `not_editable`, `rejected_invalid`, `backup_failed`, `write_failed` and
  `reload_rejected` are `failed` with a precise detail.
- Only a button press is approval; text replies never resolve a confirmation.
- `summary_for_user` is capped at `SUMMARY_MAX_CHARS` (3500) with an explicit "[truncated: N more chars not shown]"
  note. Kinds where the user must see everything they approve reject over-long proposals before requesting.
- Every final state (approved, rejected, expired, failed/not delivered) is appended to the session as its own complete
  synthetic turn (`turn_id = confirmation-<id>`, USER_MESSAGE + TURN_TERMINAL) with content
  `[[CONFIRMATION_RESOLVED]] {"confirmation_id","kind","status","summary","detail","origin_tool_call_id"}`.
  It is replayed and compacted like any other turn. No agent turn is triggered by a resolution.
  Only these synthetic records keep the marker. In user-typed text (history and the current turn) it is rewritten to
  `[[user-typed: CONFIRMATION_RESOLVED]]`, so a user cannot forge an outcome.
- The orchestrator returns the confirmations created during a turn in `OrchestratorResult.confirmations`; channel
  handlers must render each one (normal turns, delegation-feedback turns, and alongside ask_question / web-app
  responses).
- Undelivered confirmations never block the session. A confirmation whose buttons were not sent is discarded with a
  `failed` record (outcome status `not_delivered`), which frees the session slot at once and tells the agent the user
  never saw it. This covers a failed or cancelled send (of an attachment part or of the button message), an error
  while building the turn's response or the confirmation message, and a missing chat id. A failed part is
  named in the detail ("Part k of N of the content the user must read before deciding could not be delivered,
  so the confirmation buttons were not shown. Nothing was applied."). The Telegram glue wraps response building
  (`discard_confirmations_on_error`) and delivery (`try/finally` in `deliver_with_confirmations`).
- If the turn itself fails or is cancelled, its confirmations are discarded without a record
  (`discard(..., record=False)`), which frees the session slot at once. Most failures (model/API errors, a
  cancellation before any tool result was captured) persist no tool call, so a record would relate to nothing the
  agent can see. A `/stop` does persist the partial turn, including the `pending_confirmation` tool result, as a
  `cancelled` turn; the next turn's reconciliation then writes the `failed` record ("The turn did not complete, so
  the confirmation was never shown."), or an `expired` record with the neutral wording above if the process
  restarted in between.
- A press on a confirmation discarded as `not_delivered` applies nothing and is answered "This confirmation is no
  longer valid." The send may have failed after the message actually arrived (e.g. a timeout), so the user-facing
  text does not claim the buttons could not be delivered; the agent-facing record detail still says so.

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
  - Take one CapabilityCatalog snapshot per turn; several consumers call
    `resolve_active_capabilities` against that snapshot (a cached adapter may skip the call
    entirely) to resolve the active capability set (config `enabled_capabilities`, or the
    per-session `/capabilities` override when set, expanded through `nested_capabilities`, minus
    `denied_capabilities`). The system prompt and tool definitions are built from this full active
    set; there is no orchestrator-side intent-based candidate selection (not planned, see
    [#55](https://github.com/yogson/private-claw/issues/55)).
  - The model chooses among the tools of the active set via provider-native tool calls.
  - Check capability policy allow/deny gates at invocation; tool entrypoints run only when the model
    calls the tool.

Capability path is required when:
- Action is side-effecting and non-trivial.
- Workflow is ordered, multi-step, or safety-critical.
- External state checks/waits are required.

If no appropriate capability exists for a high-risk intent:
- Do not execute from memory-only instructions.
- Ask for confirmation-limited fallback or refuse with actionable guidance to create/register capability.

### 4) Capability Selection (Not planned: ranking heuristics)

> **Not planned / historical.** An earlier design had the orchestrator rank eligible capabilities
> (policy eligibility, intent-capability mappings from memory metadata, input completeness, lower
> risk, deterministic tie-breaker) and pick candidates before model exposure. This does not exist
> and is not planned (see [#55](https://github.com/yogson/private-claw/issues/55) and
> `domains/capabilities.md`, "Dynamic Activation Model (Not planned)"). It is not a requirement.

Current behavior: the model picks among the tools of the static active capability set via native
tool calls; policy gates run at invocation, and the invoked tool and its outcome are auditable.

### 5) Focused Context Budgeting

The orchestrator must enforce strict context budgets to avoid prompt bloat:
- Always include: current user turn + minimal recent session window.
- Add memory artifacts only up to Memory domain category caps.
- Capability prompt/tool metadata comes from the full active capability set only (see `domains/capabilities.md`); there is no per-turn shortlist (not planned, [#55](https://github.com/yogson/private-claw/issues/55)).
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
- Keep memory bundle compact and deduplicated.
- Prefer one execution path per turn unless user explicitly requests comparison.
- If ambiguity remains after retrieval/ranking, ask a narrow clarification question instead of loading more context.

### 7) End-to-End Decision Flow (Normative)

1. Classify turn.
2. Decide retrieval scope (none/minimal/standard) with explicit trigger reason.
3. Retrieve and score memory candidates (if triggered), apply caps.
4. Resolve the active capability set (`resolve_active_capabilities`); no per-turn candidate selection (not planned).
5. Expose the active set's tools to the model; policy gates are enforced at tool invocation (or direct-model path when no tool is called).
6. Assemble bounded prompt/context in priority order.
7. Execute route.
8. Persist response, audits, and any memory update intents.

## Cross-Domain Contracts

To coordinate Agent, Capabilities, and Memory domains safely:

- Agent -> Memory:
  - Sends retrieval query with intent hints/entities/tags and requested category caps.
  - Receives scored compact memory bundle plus retrieval audit data.

- Agent -> Capabilities:
  - Sends the model's native tool call (tool id and inputs) for a tool of the active capability set.
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

