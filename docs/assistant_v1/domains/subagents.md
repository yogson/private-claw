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

1. The configured mode: the `delegation_default_permission_mode` tool param (`config/tools.yaml`,
   or a capability `tool_overrides` entry), validated by Pydantic at startup. Unset means `auto`.
2. `backend_params.permission_mode` on the request, if it names a known mode **and** is as
   restrictive as the configured mode or more. The configured mode is a ceiling:
   - A less restrictive request is refused. The coordinator logs `subagent.permission_mode.clamped`
     (with `task_id`, `trace_id`, `session_id`) and runs the task in the configured mode.
   - `bypassPermissions` therefore only runs when it is the configured mode.
3. An unknown requested value logs `subagent.permission_mode.unknown` and falls back to the
   configured mode, never to `bypassPermissions`.

The `delegate_subagent_task` tool has no argument that sets `backend_params.permission_mode`, so
the orchestrator LLM cannot choose a mode at all; the ceiling guards any future caller.

Supported values, from most to least restrictive (`PERMISSION_MODE_RANK` in
`src/assistant/subagents/permission_modes.py`, which also explains the order):

| Mode | In a delegated run |
|---|---|
| `plan` | Read-only analysis. |
| `dontAsk` | Allow/deny rules only. Every call that would prompt is denied, **AskUserQuestion included**: the CLI denies it itself, before `can_use_tool`, so the streaming backend's question relay does not work in this mode. |
| `default` (alias `manual`) | Allow/deny rules. Prompts are denied, except AskUserQuestion, which the streaming backend relays to the user. |
| `acceptEdits` | As `default`, plus file edits in the working directories are approved. |
| `auto` | A classifier approves most actions, Bash included, and blocks destructive, irreversible or outward-facing ones. |
| `bypassPermissions` | No permission checks. |

`manual` is accepted as an alias of `default` both in config and in requests and is sent to the
CLI as `default`, because older CLIs (e.g. 2.1.81, bundled with claude-agent-sdk 0.1.50) do not
accept `manual`. Mode names are otherwise case-sensitive (`AUTO` is an unknown value).

**Allow lists and the streaming backend.** The `claude_code` backend loads user settings, so the
capability allow lists that `apply_claude_code_settings` writes to `~/.claude/settings.json` apply
to it. The streaming backend runs with the SDK default `--setting-sources ""` and loads no
settings file, so it has **no allow list**: under `acceptEdits`, `default` and `dontAsk` it denies
nearly every Bash call. Use `auto` (or `bypassPermissions`, if you accept no checks) for streaming
runs that need to run commands.

Auto mode (see https://code.claude.com/docs/en/permission-modes) lets a classifier approve
actions with no prompts and blocks irreversible, destructive or outward-facing ones. In a
delegated run nobody can answer a prompt, so:

- A call the classifier blocks is denied, and the sub-agent receives the reason and keeps
  working. Repeated blocks (3 in a row or 20 in total) make Claude Code fall back to prompting.
  `claude -p` then denies those calls. The streaming backend's `can_use_tool` callback denies
  every prompt it gets in every mode except `bypassPermissions`, AskUserQuestion apart.
  Approving those prompts would silently turn the run into `bypassPermissions`.
- Claude Code does not fail when auto mode is unavailable, for example with an unsupported model
  (it needs Opus/Sonnet 4.6+, Haiku 5.5 or a Fable model; `*-4-5` models do not qualify, so the
  example config delegates to `claude-sonnet-5-5`) or with `permissions.disableAutoMode`. Instead
  it starts in Manual (`default`) mode. Both backends therefore compare the mode the CLI reports
  on every `system` event (`init` and `status`; `claude -p` reports the downgrade in a `status`
  event before `init`) with the requested one, until the model's first output (an assistant,
  user or result message). Other events such as `rate_limit_event` do not end the check. On a
  mismatch they log `subagent.permission_mode.mismatch`, kill the CLI, and fail the task with an
  actionable error naming the model. A mode change after the model's first output is only logged
  (`subagent.permission_mode.changed`).
- The abort is based on message order. The CLI does not wait for us, so it may already be waiting
  on the first API response when it is killed, but it cannot have acted on a model response yet.
  A missed downgrade would run in a *more* restrictive mode, not a less restrictive one.
- A CLI that does not know a mode rejects `--permission-mode`, and the task fails with a hint to
  upgrade Claude Code or change `delegation_default_permission_mode`. The bundled CLI 2.1.81
  already accepts `auto` and `dontAsk`.
- claude-agent-sdk 0.1.50 types `permission_mode` as
  `Literal["default", "acceptEdits", "plan", "bypassPermissions"]`. The streaming backend
  therefore passes `auto` and `dontAsk` through `ClaudeAgentOptions.extra_args`
  (`--permission-mode auto`), the same way it already passes the `xhigh` effort.

The classifier trusts only the working directory and the repo's own remotes. To trust more
infrastructure (source-control orgs, internal domains, buckets), add prose entries to
`autoMode.environment` in `~/.claude/settings.json`. Project `.claude/settings*.json` files are
ignored for `autoMode`. See https://code.claude.com/docs/en/auto-mode-config, and use
`claude auto-mode config` to inspect the effective rules. These entries apply to the
`claude_code` backend only. The streaming backend does not read `~/.claude/settings.json` (see
above), so its classifier uses the built-in environment defaults; tracked in
[#48](https://github.com/yogson/private-claw/issues/48). Auto mode also ignores broad allow rules
such as `Bash(python:*)` (wildcarded interpreters) from the capability `claude_code_settings`, so
the classifier decides those commands. Narrow rules and deny rules still apply.

#### Behaviour change (breaking) for existing deployments

Before this change the streaming backend fell back to `bypassPermissions` for an unset or
unknown mode, and its `can_use_tool` callback approved every prompt, so streaming runs
effectively had no permission checks whatever mode was configured. Now:

- An existing `config.local` without `delegation_default_permission_mode` runs sub-agents in
  `auto` on both backends. With a `*-4-5` delegation model those runs fail at startup with the
  "auto mode unavailable" error until the model is changed.
- Explicit `acceptEdits`/`plan`/`default` streaming runs really deny prompts (for example
  `ExitPlanMode` and Bash commands outside the allow list).
- A per-request `permission_mode` less restrictive than the configured mode is clamped.

One-line opt-out to the old streaming behaviour (no checks):
`delegation_default_permission_mode: bypassPermissions` under
`delegate_subagent_task.default_params`.
