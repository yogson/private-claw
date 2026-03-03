# Capabilities Domain

## Purpose

Define the manifest-driven capability and skill ecosystem for v1, including discovery, policy enforcement, and safe runtime invocation.

## Owned Components

- `CMP_TOOL_RUNTIME_REGISTRY`
- `CMP_SKILL_RUNTIME_ENGINE`

## Scope

- Discover capability and skill manifests at startup.
- Validate manifest schema and dependency constraints.
- Register and route runtime invocations.
- Enforce capability permissions and command allowlists.
- Support first-party capabilities in v1:
  - macOS personal integration (Notes, Reminders, Calendar),
  - GitHub CLI (`gh`) integration,
  - web search and content fetch,
  - Telegram voice transcript extraction capability from channel metadata,
  - memory management operations.

## Inputs

- Capability/skill manifests and runtime configuration policies.
- Invocation requests from orchestrator and skills.

## Outputs

- Capability and skill execution results.
- Policy decision events and execution audits.

## Constraints

- All side-effecting capabilities must pass explicit capability checks.
- Shell and macOS automation commands must use allowlisted patterns.
- Capability failures must return normalized errors without crashing orchestrator.

## Risks

- Unsafe command execution without strict policy control.
- Manifest dependency mismatch or capability drift.

## Done Criteria

- Manifests load deterministically and invalid manifests are rejected.
- Allowed capability actions execute successfully with audit logs.
- Blocked actions produce policy errors with clear diagnostics.

