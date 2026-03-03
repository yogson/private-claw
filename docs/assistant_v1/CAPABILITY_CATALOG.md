# Capability Catalog

## Overview

This catalog defines canonical capability IDs and scope for Personal AI Assistant v1.

Naming convention:
- `cap.<domain>.<action>`
- Examples: `cap.macos.notes.read`, `cap.github.gh.pr.list`

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

## Policy Rules

- Capability invocations must be explicitly allowlisted per runtime context.
- Sub-agents must receive a strict subset of parent-allowed capabilities.
- Side-effecting capabilities require audit logging and bounded timeouts.
- High-risk capabilities (`shell`, mutating `gh`, write-capable `fs`) require stricter budget and approval rules.

