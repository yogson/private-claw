# Subagents Domain

## Purpose

Define background-task sub-agents that run independently from the main turn-based agent, with explicit model selection, lifecycle controls, TTL, and auditable execution.
Primary sub-agent backend in v1 is Claude Code agents for coding-task offloading.

## Owned Components

- `CMP_AGENT_SUBAGENT_COORDINATOR`
- `CMP_OBSERVABILITY_LOGGING` (delegation audit integration)

## Scope

- Validate sub-agent task creation requests.
- Enforce model allowlist, capability subset, budget, timeout, concurrency, and TTL limits.
- Launch and monitor asynchronous sub-agent workers.
- Support Claude Code agent execution as the primary governed sub-agent backend for coding tasks.
- Support Cursor-agent execution mode as an optional governed backend for compatible workflows.
- Persist task state transitions and heartbeat updates.
- Notify parent orchestration/event handlers on progress and completion.
- Record full delegation trace and usage metadata.

## Execution Model

- Main agent remains short-lived and never blocks on long-running sub-agent execution.
- Sub-agents are background tasks with a specific purpose and explicit completion criteria.
- Coding-heavy objectives should default to Claude Code agents unless policy or capability constraints require another backend.
- Sub-agent tasks can be long-running until one of:
  - objective completed,
  - TTL expired,
  - task cancelled,
  - task failed permanently.

## Request and Result Contracts

Required request fields:
- `task_id`: unique parent-scoped identifier
- `objective`: concise task objective
- `model_id`: explicit allowed model identifier
- `max_tokens`: upper token budget
- `timeout_seconds`: hard wall clock limit
- `allowed_capabilities`: explicit capability allowlist
- `result_format`: expected output schema identifier

Immediate async acknowledgement fields:
- `task_id`
- `accepted` (true/false)
- `rejection_reason` (when rejected)
- `expires_at`
- `next_check_at` (optional for monitor tasks)

Result fields:
- `status`: completed, failed, timed_out, expired, cancelled
- `summary`: concise result text
- `artifacts`: structured attachments or references
- `usage`: token and duration data
- `safety_flags`: policy and guardrail outcomes

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

Coordinator enforcement rules:
- Reject spawn if `model_id` is not allowlisted.
- Reject spawn when parent task budget would be exceeded.
- Enforce max concurrent sub-agents per parent request.
- Enforce capability subset relative to parent permission scope.
- Enforce TTL (`expires_at`) and max runtime for each task.
- Require heartbeat updates for long-running tasks and expire stale tasks.
- Emit start/update/final audit events with usage metrics.

## Inputs

- Task creation requests from `CMP_CORE_AGENT_ORCHESTRATOR`.
- Runtime policy configuration (`model allowlist`, `budget`, `capabilities`, `ttl`).
- Optional trigger payloads (CI status source, market threshold, research objective).
- Optional Claude Code execution parameters (`claude_agent_profile`, `run_mode`, `attachments`, `workspace_scope`).
- Optional Cursor-agent execution parameters (`cursor_agent_type`, `run_mode`, `attachments`).

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
- Claude Code agent execution is the default route for coding-task delegation and must remain policy-gated with explicit capability and workspace scope constraints.
- Cursor-agent execution must be gated by explicit capability allowlist and run with the same budget/TTL/trace constraints.

## Risks

- Cost spikes from excessive delegation.
- Capability escalation if policy checks are bypassed.
- Zombie tasks if heartbeat/TTL enforcement is missing.
- Duplicate side effects if retry behavior is not idempotent.
- Workspace overreach if Claude Code agent scope boundaries are not enforced.
- Unintended high-impact code changes from Claude Code agent runs without strict policy envelopes.
- Unintended high-impact actions from Cursor-agent runs without strict policy envelopes.

## Done Criteria

- Invalid or unsafe spawn requests are blocked deterministically.
- Accepted tasks execute within budget, timeout, and TTL limits.
- Background tasks survive process restarts with consistent persisted state.
- Progress/completion events can trigger follow-up actions or user notifications.
- Parent-child traceability exists for all task lifecycle transitions.
- Claude Code agent tasks are used as the primary coding offload path and produce auditable lifecycle records equivalent to other backends.
- Cursor-agent tasks execute only when explicitly authorized and produce the same auditable lifecycle records.

## Current v1 Implementation Notes

- Delegation entrypoint tool: `delegate_subagent_task`.
- Single-run contract: a task is delegated with `objective` and optional `model_id` override.
- Model, backend, timeout, and budget defaults are configured via tool defaults
  (for example `delegation_default_model_id`, `delegation_default_timeout_seconds`).
- Coordinator implementation: `src/assistant/subagents/coordinator.py`.
- Backend abstraction: `DelegationBackendAdapterInterface` with provider routing in coordinator.
- First backend implementation: `ClaudeCodeBackendAdapter` (`src/assistant/subagents/backends/claude_code.py`).
- Completion notifications are currently disabled and will be rebuilt.

- SDK streaming backend: `ClaudeCodeStreamingBackendAdapter`
  (`src/assistant/subagents/backends/claude_code_streaming.py`), with the AskUserQuestion relay.

### Claude Code permission mode

Both Claude Code backends start every run with an explicit `--permission-mode`. The coordinator
resolves the mode when it accepts a task and stores it in the task's `backend_params`:

1. `backend_params.permission_mode` on the request, if it names a known mode.
2. Otherwise the `delegation_default_permission_mode` tool param (`config/tools.yaml`, or a
   capability `tool_overrides` entry). Pydantic validates the value at startup.
3. Otherwise `auto`.

Supported values: `default` (`manual` is accepted as an alias), `acceptEdits`, `plan`, `auto`,
`dontAsk`, `bypassPermissions`. An unknown requested value logs `subagent.permission_mode.unknown`
and falls back to the configured default. It never falls back to `bypassPermissions`, which is
used only when it is configured or requested explicitly. The shared resolver lives in
`src/assistant/subagents/permission_modes.py`.

Auto mode (see https://code.claude.com/docs/en/permission-modes) lets a classifier approve
actions with no prompts and blocks irreversible, destructive or outward-facing ones. In a
delegated run nobody can answer a prompt, so:

- A call the classifier blocks is denied, and the sub-agent receives the reason and keeps
  working. Repeated blocks (3 in a row or 20 in total) make Claude Code fall back to prompting.
  `claude -p` then denies those calls. The streaming backend's `can_use_tool` callback denies
  every prompt it gets in every mode except `bypassPermissions`, AskUserQuestion apart.
  Approving those prompts would silently turn the run into `bypassPermissions`.
- Claude Code does not fail when auto mode is unavailable, for example with an unsupported model
  (it needs Opus/Sonnet 4.6+, Haiku 5.5 or a Fable model, so the `*-4-5` models in the example
  config do not qualify) or with `permissions.disableAutoMode`. Instead it starts in Manual
  (`default`) mode. Both backends compare the mode reported by the CLI's `system/init` event with
  the requested one. On a mismatch they abort the run before any tool call, log
  `subagent.permission_mode.mismatch`, and fail the task with an actionable error.
- A CLI too old to know a mode rejects `--permission-mode`, and the task fails with a hint to
  upgrade Claude Code or change `delegation_default_permission_mode`.
- claude-agent-sdk 0.1.50 types `permission_mode` as
  `Literal["default", "acceptEdits", "plan", "bypassPermissions"]`. The streaming backend
  therefore passes `auto` and `dontAsk` through `ClaudeAgentOptions.extra_args`
  (`--permission-mode auto`), the same way it already passes the `xhigh` effort.

The classifier trusts only the working directory and the repo's own remotes. To trust more
infrastructure (source-control orgs, internal domains, buckets), add prose entries to
`autoMode.environment` in `~/.claude/settings.json`. Project `.claude/settings*.json` files are
ignored for `autoMode`. See https://code.claude.com/docs/en/auto-mode-config, and use
`claude auto-mode config` to inspect the effective rules. The `claude_code` backend loads user
settings, so these entries apply to it. The streaming backend runs with the SDK default
`--setting-sources ""`, so it does not read `~/.claude/settings.json`, and the classifier uses
its built-in environment defaults there. Auto mode also ignores broad allow rules such as
`Bash(python:*)` (wildcarded interpreters) from the capability `claude_code_settings`, so the
classifier decides those commands. Narrow rules and deny rules still apply.
