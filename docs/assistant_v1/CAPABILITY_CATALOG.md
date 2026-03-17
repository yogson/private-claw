# Capability Catalog

## Overview

This catalog defines canonical capability IDs and scope for Personal AI Assistant v1.

Boundary note:
- This document owns capability IDs and catalog record shape.
- Runtime activation flow, manifest loading, and policy enforcement are owned by `docs/assistant_v1/domains/capabilities.md`.
- Cross-domain invariants are owned by `docs/assistant_v1/ASSISTANT_V1_TECHNICAL_SPECIFICATION.md`.
- Boundary governance is defined in `docs/assistant_v1/COMPONENT_BOUNDARIES.md`.

Naming convention:
- `cap.<domain>.<action>`
- Examples: `cap.macos.notes.read`, `cap.github.gh.pr.list`
- External MCP mapping convention: `cap.mcp.<server>.<tool>`
- Example: `cap.mcp.chrome_devtools.browser_snapshot`

Catalog usage model:
- Startup: load and validate full catalog/manifests.
- Turn-time: expose only shortlisted capability descriptors to the model.
- Do not provide all capability/tool definitions in model context by default.

## Catalog Record Contract

Each catalog entry should be backed by a descriptor record used for dynamic activation:

- `capability_id`
- `domain`
- `summary`
- `permissions.read_only`
- `permissions.side_effecting`
- `permissions.requires_confirmation`
- `input_schema` (LLM-facing invocation args)
- `safety_notes`
- `examples` (optional)
- `status` (`enabled`, `disabled`, `deprecated`)

## Core Capability Groups

### 1) macOS Personal Capabilities

- `cap.macos.notes.read`
- `cap.macos.notes.write`
- `cap.macos.reminders.read`
- `cap.macos.reminders.write`
- `cap.macos.calendar.read`
- `cap.macos.calendar.write`

Optional future additions:
- `cap.macos.contacts.read`
- `cap.macos.notifications.send`

### 2) GitHub CLI Capabilities

- `cap.github.gh.issue.list`
- `cap.github.gh.issue.view`
- `cap.github.gh.pr.list`
- `cap.github.gh.pr.view`
- `cap.github.gh.pr.create`
- `cap.github.gh.checks.view`

### 3) Web Knowledge Capabilities

- `cap.web.search.query`
- `cap.web.fetch.page`
- `cap.web.summarize.content`

### 4) Memory Capabilities

- `cap.memory.read`
- `cap.memory.write`
- `cap.memory.update`
- `cap.memory.consolidate`

### 5) Telegram Input Capabilities

- `cap.telegram.voice.transcript.extract`
- `cap.telegram.ui.inline_keyboard.render`
- `cap.telegram.ui.callback.handle`

## Recommended Additional Capabilities

### 6) Filesystem Capabilities

- `cap.fs.read`
- `cap.fs.write`
- `cap.fs.list`
- `cap.fs.search`

### 7) Guarded Command Capabilities

- `cap.shell.execute.allowlisted`
- `cap.shell.execute.readonly`

### 8) Scheduler and Monitoring Capabilities

- `cap.scheduler.job.create`
- `cap.scheduler.job.cancel`
- `cap.scheduler.job.list`
- `cap.monitor.ci.watch`
- `cap.monitor.asset.threshold`

### 9) Notification Capabilities

- `cap.notify.telegram.send`
- `cap.notify.telegram.alert`

### 10) Cursor Agent Capabilities

- `cap.cursor.agent.run`
- `cap.cursor.agent.resume`
- `cap.cursor.agent.cancel`

### 11) External MCP Capabilities

Namespace rule:
- One capability per approved MCP tool.
- Server ID and tool name must be stable and lowercase in capability ID path.
- Capability descriptors for MCP tools must include `mcp_server` and `mcp_tool` metadata.

Chrome-devtools MCP example set:
- `cap.mcp.chrome_devtools.browser_navigate`
- `cap.mcp.chrome_devtools.browser_snapshot`
- `cap.mcp.chrome_devtools.browser_click`
- `cap.mcp.chrome_devtools.browser_type`
- `cap.mcp.chrome_devtools.browser_fill`
- `cap.mcp.chrome_devtools.browser_scroll`
- `cap.mcp.chrome_devtools.browser_wait`

### 12) Delegation Capabilities

- `cap.delegation.task.enqueue`
- `cap.delegation.task.status`
- `cap.delegation.workflow.coding`

Implementation note:
- In runtime config, delegated coding workflows are currently represented by the
  `delegation_coding` capability manifest and the `delegate_subagent_task` tool.

## Policy Rules

These are catalog-level policy invariants. Runtime enforcement behavior is defined in `docs/assistant_v1/domains/capabilities.md`.

- Capability invocations must be explicitly allowlisted per runtime context.
- Sub-agents must receive a strict subset of parent-allowed capabilities.
- Side-effecting capabilities require audit logging and bounded timeouts.
- MCP-backed capabilities must pass both capability policy and MCP server/tool allowlist checks.
- High-risk capabilities (`shell`, mutating `gh`, write-capable `fs`) require stricter budget and approval rules.
- Candidate capability ranking must happen before descriptor injection; keep per-turn shortlist small (recommended top 1-3).

