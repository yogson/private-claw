# Capabilities Domain

## Migration (v1 Refactor)

**Breaking change**: Capabilities are now defined in `config/capabilities/*.yaml` (prompt + toolset), not in `plugins/capabilities/`. Tools are configured in `config/tools.yaml`.

- **Old**: `config/capabilities.yaml` had `allowed_capabilities`, `shell_readonly_commands`, `command_allowlist`.
- **New**: `config/capabilities.yaml` has `enabled_capabilities`, `denied_capabilities` only. Tool params (e.g. `shell_readonly_commands`) live in `config/tools.yaml` and can be overridden per capability in `config/capabilities/<id>.yaml`.
- **Removed**: `plugins/capabilities/` directory and plugin manifest loading at startup.

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
- Expose model-visible tool descriptors and execute provider-native tool calls.
- Support first-party capabilities in v1:
  - macOS personal integration (Notes, Reminders, Calendar),
  - GitHub CLI (`gh`) integration,
  - web search and content fetch,
  - Telegram voice transcript extraction capability from channel metadata,
  - memory management operations (including `memory_propose_update` proposal tool).

## Canonical Models

| Model ID | File Pattern | Required Fields | Purpose |
|---|---|---|---|
| `CMP_DATA_MODEL_TOOL_DEFINITION` | `config/tools.yaml` | `tool_id`, `entrypoint`, `enabled`, `default_params` | Tool registration and global defaults |
| `CMP_DATA_MODEL_CAPABILITY_DEFINITION` | `config/capabilities/*.yaml` | `capability_id`, `prompt`, `tools` | Capability = prompt + toolset + per-tool overrides; optional `self_edit` opt-in for agent-proposed edits |
| `CMP_DATA_MODEL_CAPABILITY_POLICY` | `config/capabilities.yaml` | `enabled_capabilities`, `denied_capabilities` | Operator policy: which capability manifests are active |
| `CMP_DATA_MODEL_SKILL_MANIFEST` | `plugins/skills/*/manifest.yaml` | `skill_id`, `version`, `entrypoint`, `required_capabilities`, `capabilities` | Skill registration and dependencies |
| `CMP_DATA_MODEL_MCP_SERVER_REGISTRY` | `config/mcp_servers.yaml` | `server_id`, `transport`, `enabled`, `connection` | External MCP server registration and connection policy |

Tool definition schema (`config/tools.yaml`):
- `tool_id`: stable identifier (e.g. `shell_execute_readonly`, `memory_search`).
- `entrypoint`: `<python_module>:<callable_name>` format.
- `enabled`: bool, default true. Acts as a global kill-switch: if false, the tool is never exposed regardless of capability bindings.
- `default_params`: optional shell_readonly_commands, command_allowlist, timeouts.

## Capability-First Activation Model

`config/tools.yaml` is the canonical catalog of **all** available tools (typically enabled by default). Capabilities select which subset the agent can use:

- **assistant** (`config/capabilities/assistant.yaml`): baseline capability with general-purpose tools (memory, ask, shell readonly, web search). Does not include higher-risk tools like `shell_execute_allowlisted`.
- **Add-on capabilities** (e.g. `deploy`, `github-ops`): extend the toolset for specific needs. Example: `deploy` adds `shell_execute_allowlisted` with a `command_allowlist` for `gh`, `git`, etc.

To restrict the general-purpose agent from using `gh` while allowing a specialized "github ops" agent: keep `shell_execute_allowlisted` enabled in `tools.yaml`, omit it from `assistant`, and add it only in a `github-ops` capability. Enable `assistant` for the general agent; enable `assistant` + `github-ops` for the specialized one.

### macOS Personal Capability

The **macos_personal** capability (`config/capabilities/macos_personal.yaml`) exposes Notes and Reminders tools. It is opt-in and disabled by default.

**Tool IDs** (in `config/tools.yaml`):

- `macos_notes_read` – list recent Notes (name, body)
- `macos_notes_write` – create a Note (title, body)
- `macos_reminders_read` – list Reminders (name, body, due_date)
- `macos_reminders_write` – create a Reminder (title, body, list_name, due_year, due_month, due_day, due_hour, due_minute)

**Activation**: Add `macos_personal` to `enabled_capabilities` in `config/capabilities.yaml`:

```yaml
enabled_capabilities:
  - assistant
  - macos_personal
denied_capabilities: []
```

**Platform**: Tools return `rejected_platform` on non-macOS (non-darwin) systems. On macOS, they invoke AppleScript via `osascript` with bounded timeouts.

Capability definition schema (`config/capabilities/*.yaml`):
- `capability_id`: stable identifier (e.g. `assistant`, `omnichem-deploy`).
- `prompt`: optional system prompt fragment appended for this capability.
- `tools`: list of `{tool_id, enabled, params_override}` bindings.
- `tool_overrides`: optional per-tool param overrides keyed by tool_id.
- `nested_capabilities`: capability ids activated together with this one (must be known ids).
- `mcp_servers`: ids from `config/mcp_servers.yaml` whose tools this capability grants.
- `claude_code_settings`: `permissions` (`allow` / `deny`) and `mcp_servers` for Claude Code sub-agents.
- Discovery: load all `*.yaml` / `*.yml` under `config/capabilities/` except `index.yaml` and hidden names (`._x.yaml` AppleDouble files, `.#x.yaml` editor locks, dot-prefixed temp files); backup/swap files such as `x.yaml~` or `.x.yaml.swp` never match. Duplicate `capability_id` is a startup error.
- **Strict schema**: unknown keys are validation errors on the manifest and on every nested
  block (`tools[]`, `params_override`, `tool_overrides`, `claude_code_settings`,
  `permissions`), e.g. `[a.yaml] nested_capabilites: extra fields not permitted`. A typo
  can no longer be dropped silently. At startup this fails fast; on a live reload the
  catalog keeps the last-known-good snapshot (see below). Parsed definitions are frozen
  pydantic models shared by snapshots, turns and adapters: their fields cannot be
  reassigned, but the freeze is shallow (lists and dicts such as `tools`,
  `tool_overrides` or `claude_code_settings.mcp_servers` are not deep-frozen), so
  consumers treat them as read-only and copy before modifying.

## Tool Status and Params Merge Rules

This section defines how runtime tool availability and parameters are resolved from:
- `config/capabilities.yaml` (policy),
- `config/capabilities/<id>.yaml` (capability tool bindings/overrides),
- `config/tools.yaml` (tool definitions/default params).

Resolution flow:
1. Start from policy and resolve the active set with the single resolver
   `resolve_active_capabilities(enabled, denied, snapshot.definitions)`
   (`core/capabilities/resolver.py`), used by every consumer (system prompt, tools, tool
   params, mismatch check, MCP toolsets, sub-agent MCP servers, `~/.claude/settings.json`):
   - start from `enabled_capabilities`, or from the per-session `/capabilities` override when set (it replaces `enabled_capabilities` for that session), in order,
   - expand `nested_capabilities` depth-first, cycle-safe, children right after their parent,
   - never include **or descend into** a capability listed in `denied_capabilities`: the
     children of a denied capability are active only if they are enabled explicitly or
     reachable through another, non-denied parent,
   - drop ids that have no manifest (debug log `capabilities.resolve.missing_manifest`; the
     catalog reports a missing enabled manifest once as a `capabilities.reload_warning`).
2. Collect `tool_id` values from each surviving capability manifest where binding `enabled=true`.
3. Keep only tools that also exist in `config/tools.yaml` with `enabled=true`.
4. Resolve tool callable from `entrypoint`.

Status (enabled/disabled) semantics:
- A tool is executable only if ALL are true:
  - capability is enabled by policy and not denied,
  - capability tool binding exists and `enabled=true`,
  - global tool definition exists and `enabled=true`.
- If a capability references a tool that is missing or disabled in `config/tools.yaml`, bootstrap fails (fail-fast); on a live reload the new snapshot is rejected and the last-known-good stays in use.

Parameter merge semantics:
- Base params come from `config/tools.yaml` -> `tools[].default_params`.
- Capability overrides are then applied for each enabled capability in `enabled_capabilities` order.
- Within one capability:
  - inline `tools[].params_override` is applied first,
  - `tool_overrides[tool_id]` is applied after it (therefore wins on key conflicts).
- If multiple capabilities override the same tool/key, later capabilities in `enabled_capabilities` win.

List handling:
- `command_allowlist` is replaced by override value (not deep-merged item-by-item).
- `shell_readonly_commands` is replaced by override value (not union-merged).

Skill manifest schema requirements:
- `skill_id`: stable identifier.
- `entrypoint`: `<python_module>:<callable_name>`.
- `required_capabilities`: list of capability IDs required for execution.
- `capabilities`: optional field accepted by the schema (`SkillManifest.capabilities` in `extensions/registry/models.py`) but ignored: nothing reads it. Dynamic capability requests are not planned (see "Dynamic Activation Model (Not planned)").
- `dependency_resolution`: all `required_capabilities` must be registered and enabled at load time; unresolved dependencies block skill activation.
- Discovery pattern: load manifests from `plugins/skills/*/manifest.yaml`; duplicate `skill_id` values are startup errors.

Capability policy schema (`config/capabilities.yaml`):
- `enabled_capabilities`: list of capability IDs to enable (must have a manifest in `config/capabilities/*.yaml`).
- `denied_capabilities`: list of capability IDs to block (overrides enabled).

MCP server registry schema requirements (`config/mcp_servers.yaml`):
- `id`: stable identifier (letters, digits, `_`, `-`); it prefixes the tool names (`mcp_<id>_<tool>`).
- `transport`: `stdio` (`command`, `args`, `env`), `streamable-http` or `sse` (`url`, optional `headers`).
- `enabled`: bool.
- `include_tools` / `exclude_tools`: server-side tool names to expose / hide (mutually exclusive).
- `confirm_tools`: tools needing user confirmation; tools annotated `destructiveHint: true` (and not `readOnlyHint: true`) always need it.
- `include_instructions`: append the server's own instructions to the agent instructions (default true).
- `call_seconds`: per-server override of `timeouts.call_seconds`.

Whole servers are connected and their tools discovered at runtime (pydantic-ai `MCPToolset`); there is no per-tool mapping and no `cap.mcp.*` ID scheme. Connections are opened once at app startup and shared by all turns; a server that is down is retried lazily with backoff, and a dead long-lived session is re-established on the failing operation. Listing operations (tools, instructions) are retried once on the new session; a tool call is never replayed (the server may already have executed it) - the model receives a tool error saying the outcome is unknown. A changed server config reconnects in place; the old session is closed once idle. SSE / streamable-http streams use a long idle limit so they survive gaps between turns; `call_seconds` only bounds individual calls. Servers that return failures as a successful `{"error": ...}` payload must be fixed at the server; the client does not guess.

`confirm_tools` and `destructiveHint` tools trigger a "notify before call" hook: in verbose mode the call is announced in the chat first. It does not block the call or wait for approval today; a real approval flow is future work.

MCP activation: a capability manifest grants servers with `mcp_servers: [<server_id>]` (nested capabilities are followed). Unknown or disabled server IDs log a warning at bootstrap. The model sees the tools as `mcp_<server_id>_<tool_name>`; `mcp_` is reserved and no first-party tool may use it.

## Capability Catalog: Snapshots and Live Reload

`CapabilityCatalog` (`core/capabilities/catalog.py`, interface
`CapabilityCatalogInterface`) is the single owner of manifest state. Nothing on the turn
path parses manifests directly; `load_capability_definitions` remains only as the
low-level, uncached parser.

Snapshot (`core/capabilities/snapshot.py`):
- `CapabilitySnapshot` is immutable: `revision`, read-only `definitions` and `files`
  (`capability_id -> ManifestFileState(path, mtime_ns, size, sha256)`; the sha256 of the raw
  bytes is the precondition for editors), `capabilities_dir`, `errors`, `loaded_at`.
- `revision` starts at 1 and grows by exactly 1 on every successful swap; the swap is a
  single attribute assignment.

API:
- `current()`: the current snapshot, no I/O.
- `refresh()`: called **once at the start of every turn** by the orchestrator (and when
  `/capabilities` is rendered). It stats `config/capabilities/*.yaml` (file set,
  `st_mtime_ns`, `st_size`; at most once per second) and returns the current snapshot when
  nothing changed — no parsing. Otherwise it re-reads only files whose stat changed and
  re-parses only files whose content hash changed; a touch-only change does not bump the
  revision. Two kinds of files are re-read even when their stat is unchanged:
  - *racily clean* files (Git's rule): a file whose mtime is within 2 s of the moment it
    was read could be rewritten in the same mtime tick with the same size (coarse-mtime
    filesystems, scripted edits), so it is re-hashed on every refresh until its mtime is
    older than the read;
  - unreadable files (e.g. `chmod 000`, EIO): the read error is never cached, so fixing
    the cause (permissions, a remount) is picked up on the next refresh without touching
    the file.
- `reload(reason)`: re-reads every file regardless of stat (e.g. right after an approved
  write), still bumping the revision only if content changed.
- `subscribe(callback)`: called after every swap to a new revision, after the catalog lock
  is released. Delivery is serialized: one delivery loop runs at a time and keeps going
  until every subscriber has received the latest revision. Each subscriber gets a revision
  at most once, never an older one after a newer one, and always ends on the latest; an
  intermediate revision may be skipped. A subscriber may call `refresh()`/`reload()`: a
  revision produced meanwhile (by a subscriber, or by another thread) is only recorded and
  handed to the running loop, so that call can return before its revision has reached
  every subscriber. Subscriber errors are logged, never raised.

Change detection is polling at turn start; there is no background watcher, because
nothing consumes the snapshot between turns. Editors, `git pull`, sub-agents and future
edit tools are all picked up the same way, without a restart.

Validation and last-known-good (LKG):
- Per file: YAML parse, dict shape and the strict `CapabilityDefinition` schema.
- Cross-file (on the whole candidate): duplicate `capability_id`, `nested_capabilities`
  refer to known ids, and (validator from bootstrap) the active set's tool bindings exist
  and are enabled in `tools.yaml`, do not use the reserved `mcp_` prefix and their
  entrypoints resolve (`core/capabilities/validation.py`, shared by bootstrap and reload).
- **Any** blocking issue rejects the reload as a whole: the previous snapshot (same
  revision, same definitions) stays current and the issues are recorded in
  `snapshot.errors`. There is no partial per-file merge. A new broken file is therefore
  excluded, and a broken edit of an existing file keeps its previous definition.
- A half-written file usually fails to parse and is retried on the next turn; the fix (or
  reverting to the LKG content) clears the errors. This is **not** a guarantee: a write
  cut at a line boundary can still be valid YAML and pass the schema (e.g. a `tools` list
  cut short), and is then accepted as a new revision until the rest of the write lands
  (its mtime changes, so the next refresh picks up the complete file). The real
  protection is an atomic write: editors that save via temp file + rename, and the #54
  writer, which must write a temp file whose name is not a manifest name (see
  `is_capability_manifest_name`) and `os.replace` it.
- Deleting a manifest removes the capability. If it is enabled in policy, the reload still
  succeeds with a non-blocking warning, and the resolver ignores the missing id. At startup
  an enabled capability without a manifest is still a fatal error.
- Startup is unchanged and fail-fast: `bootstrap_with_catalog()` builds the catalog, and
  any blocking issue at the initial load aborts startup.
- Surfacing: one structured `capabilities.reload_failed` warning when a distinct error
  first appears (with `file`, `error`, `lkg_revision`; a failure of the scan itself also
  carries the traceback), not on every refresh while it persists. An error that clears and
  later comes back is logged again. `capabilities.reload_recovered` is logged once the
  reload succeeds again. The Telegram `/capabilities` picker shows a warning block with the
  revision still in use and, per error, only its first line and line/column (PyYAML puts
  the offending source line after the first line, and it may hold secrets). Paths are shown
  as the file name only: read and scan errors carry the OS message (`[a.yaml] read error:
  Permission denied`), and any other absolute path is cut to its last component. The full
  message is in the log. There is no proactive Telegram notification.

One snapshot per turn: the orchestrator calls `refresh()` once and passes that snapshot to
the tool-mismatch check, `TurnAdapterCache.get_or_build(config, snapshot)` (system prompt +
tools) and `build_tool_runtime_params(config, snapshot)`. Delegation feedback turns use the
same path. The turn's `prompt_trace` records `capabilities.revision`, the active ids and the
`manifest_sha256` of each active capability.

Adapter cache and prompt:
- `TurnAdapterCache` key: `(frozenset(enabled), frozenset(denied), snapshot.revision)`. A
  new revision builds new adapters (new prompt and tools) and evicts all adapters of older
  revisions; the cache is also LRU-bounded (16 entries).
- The system prompt is composed from the snapshot only and is byte-stable for a revision,
  so the Anthropic instruction cache keeps hitting. The current date is **not** part of it:
  it is a dynamic pydantic-ai instruction evaluated on every run, sent as a separate block
  after the cached system prompt.

Derived state follows revisions:
- The Telegram `/capabilities` picker reads the catalog on every render.
- Sub-agent MCP servers (`claude_code_settings.mcp_servers` of the default policy's active
  set) are recomputed and handed to both Claude Code backends on every new revision
  (`SubagentMcpServersSync`); tasks already running keep their servers.
- `~/.claude/settings.json` is applied once at startup and then re-applied only when a
  revision changes the `claude_code_settings` of the default policy's active capabilities
  (`ClaudeCodeSettingsSync`, created in `api/lifespan.py`). The sync records only a
  successful apply, so a write skipped at startup or later is retried with the next
  revision even if that revision leaves `claude_code_settings` unchanged. The merge semantics are unchanged: if no active capability
  defines settings any more, the file is left as it is. The writer
  (`core/capabilities/claude_settings_file.py`) only owns `permissions.allow`,
  `permissions.deny` and `mcpServers` and preserves every other key. If the existing file
  is unreadable, not valid JSON or not a JSON object, it is left untouched and
  `capabilities.claude_code_settings.skipped` is logged; the next revision retries. A write
  that fails (read-only `~/.claude` or target, full disk) is skipped the same way and is
  never raised, so it cannot abort startup. Writes are atomic (temp file in the same
  directory + `os.replace`, then a best-effort fsync of the directory), keep the file mode
  (a new file gets `0600`), follow a symlinked `settings.json` to its target, and are
  skipped when the merged content is already on disk. `allow`/`deny` are still replaced as a whole,
  so entries added there by hand are dropped on the next apply.

Out of scope (still startup-only): `tools.yaml`, `capabilities.yaml` (policy),
`mcp_servers.yaml` and the other config domains.

## Agent-Driven Manifest Editing (`self_edit`, [#54](https://github.com/yogson/private-claw/issues/54))

The agent can propose edits to capability manifests. Nothing is ever written without an
explicit user approval through the generic confirmation mechanism (kind `capability_edit`,
see `domains/agent.md` "User Confirmations").

Opt-in and scope:
- `self_edit: StrictBool = false` in `CapabilityDefinition` (a quoted `"true"`/`"yes"` or
  `1` is a validation error). Only manifests a human marked with the plain YAML boolean
  `self_edit: true` are readable/editable by the agent (YAML 1.1 `yes`/`on` load as true
  but do not opt in), and only if the file (after
  following symlinks) lives inside the active config dir (e.g. `config.local/`). A symlink
  that resolves elsewhere is `not_editable`; a symlink inside the config dir is written at
  its target, so the link survives.
- The agent may change any field **except** `capability_id` (a rename is create + delete,
  both out of scope) and `self_edit` (only a human sets or clears it). Creating, deleting or
  renaming capabilities is not supported. `tools.yaml`, `capabilities.yaml` and the other
  config domains are never edited.
- There is no content filtering: the approver checks the content. The service only
  validates structure.

Components:
- `core/capabilities/edit_service.py` — `CapabilityEditService`: read, propose (validation),
  apply, history, rollback. All domain logic lives here.
- `core/capabilities/edit_summary.py` — the approver-facing summary.
- `core/confirmation/capability_edit.py` — `build_capability_edit_draft` and
  `CapabilityEditHandler` (registered in `ConfirmationHandlerRegistry` in `api/lifespan.py`).
- `agent/tools/capability_edit.py` — thin tools, bound by the `capability_admin` capability
  (`config/capabilities/capability_admin.yaml`, not enabled by default):

| Tool | Result |
|---|---|
| `capability_read(capability_id)` | `{status: ok, capability_id, file, self_edit, raw_yaml, base_sha256, revision}` |
| `capability_propose_edit(capability_id, old_string, new_string, reason, base_sha256)` | `pending_confirmation {confirmation_id, deduplicated, note}` / `another_confirmation_pending` / `rejected_invalid` / `not_editable` / `stale` / `unavailable` (with `reason`) |
| `capability_history(capability_id, limit=10)` | `{status: ok, entries: [{history_id, created_at, sha256, reason, confirmation_id, op}]}` |
| `capability_propose_rollback(capability_id, history_id, reason, base_sha256)` | same shape as `capability_propose_edit` |

`not_editable` results also list `editable_capabilities`. Both propose tools are in
`CONFIRMATION_TOOL_NAMES`.

Proposal validation (no write):
1. Resolve `capability_id` → file through the catalog snapshot (see opt-in rules above).
   The file on disk must parse with `self_edit: true` and pass the structural check of
   step 5, else `not_editable` (so a file that already hides a second opt-in is never
   edited).
2. sha256 of the raw bytes ≠ `base_sha256` → `stale` ("call capability_read again").
3. Exact match on the **raw text**: `old_string` non-empty, different from `new_string`,
   exactly one occurrence, else `rejected_invalid`. The replacement keeps comments and
   formatting outside the edited span byte-identical (no YAML round-trip, no new dependency).
4. The result must parse and validate against the strict schema (`extra="forbid"`), keep
   the parsed `capability_id` and `self_edit: true` unchanged, name only tools from the
   tools registry (`tools.yaml`) in `tool_overrides` keys and `tools` bindings it adds
   (keys that already existed are tolerated), and pass the catalog's
   cross-file checks on a candidate snapshot (`validate_candidate`: nested references,
   tool bindings of the default policy).
5. Structural guard of the protected keys (`core/capabilities/manifest_structure.py`,
   `rejected_invalid`), run on the `yaml.compose` node tree **before** parsing, never on
   the text (YAML escapes such as `"self\x5fedit"` and merge keys hide keys from text
   matching): exactly one YAML document with a mapping root; no merge keys (`<<`) and no
   aliases anywhere; scalar keys only; no two keys of one mapping equal after unescaping
   and NFKC + casefold (so duplicates, quoting and case variants collide); no top-level key
   that only normalises to `self_edit`/`capability_id`; exactly one top-level `self_edit`
   and one `capability_id`; `self_edit` is the plain boolean `true`. Comments may mention
   `self_edit` freely. The same check runs at propose, on rollback content and again at
   apply.
6. The draft payload is `{op: edit, capability_id, file, base_sha256, new_sha256,
   old_string, new_string, reason, base_revision}`; rollbacks carry `op: rollback` and
   `target_history_id` instead of the strings. `dedupe_key` = sha256 of
   `(capability_id, base_sha256, op, old_string, new_string | history_id)`.

Summary (`summary_for_user`, `core/capabilities/edit_summary.py`): a generic structural
diff of the parsed manifests (`model_dump` before vs. after, so shorthands such as
`command_allowlist: [git]` appear with their effective defaults). Headline with id and file
name, the reason (clipped to 300 chars with `(+N chars hidden)`), then one line per changed
leaf with the concrete values: `path: before → after`, `path: + value`, `path: − removed`.
Lists of entries are matched by their identity field (`tool_id`, `id`, `command_pattern`,
`name`), so e.g. `tool_overrides.shell_execute_allowlisted.command_allowlist[git].allowed_args_pattern: "^status$" → ".*"`;
lists of strings show each item added (`+`) or removed (`−`); a pure reorder says
`order changed`. Values on ⚠ lines are never cut: a changed string longer than 120 chars
is shown as a diff excerpt with up to 20 unchanged chars of context on each side and the
removed / added segments in full, e.g.
`allowed_args_pattern: edited (134 → 137 chars): …"aaaaaaaaaaaaaaaaaa)$"[+"|.*"]`. A ⚠
value, differing segment or key longer than 400 chars cannot be shown in full, so the
proposal is refused (`rejected_invalid`, "propose a smaller edit"). Only non-sensitive
values are cut with `(+N chars hidden)`. Inside `claude_code_settings.mcp_servers.<server>`,
every value at or below an `env` / `headers` key or a secret-looking key (token, secret,
password, api key, auth, credential, cookie, private key) is shown as `<hidden>` whatever
its type, also when the agent changes its type (`env: {...}` → `"x"`) or replaces an
enclosing value: dicts keep their key names, `KEY=value` list items keep `KEY`, and a change
that touches only hidden values says `(secret value changed)`. Secrets elsewhere (e.g. a
`?token=` in an MCP `url` or in `args`) are shown, so keep them in `env` / `headers`.
YAML parse errors on the edit path report the problem and line / column only, never the
source excerpt PyYAML embeds (it may quote a secret).

Every manifest-derived string in the summary (values, list items, keys such as
`tool_overrides` / `mcp_servers` names, the file name), the reason and the prompt excerpt
is rendered as one line: values and keys are double-quoted with `\n`, `\r`, `\t`, `"` and
`\\` escaped, free text has whitespace runs collapsed, and control, format (bidi
overrides/isolates, zero-width), line/paragraph separator, private-use and unassigned
characters, spaces other than U+0020, and blank-rendering letters / marks (Hangul fillers
U+115F / U+1160 / U+3164 / U+FFA0, Braille blank U+2800, combining grapheme joiner,
variation selectors, tag characters) are escaped as `\uXXXX`. So no manifest content can forge extra summary lines or a
fake "Approved"/⚠-free appearance. The failure detail of an applied confirmation
(shown in the "Failed:" status line) is sanitised the same way.

Every top-level field except `prompt` (tools, tool_overrides, params_override, MCP servers,
nested capabilities, Claude Code permissions and MCP servers, and any field added later) is
security-sensitive: its lines are marked `⚠`, listed first, and a
`⚠ Security-sensitive changes …` line precedes the field list. `prompt` shows the size
change, the added / removed line counts and an excerpt of the added text that says how much
was cut (`excerpt (first 300 of N added chars)`). The summary is capped in UTF-16 code units
(the unit Telegram counts) at `SUMMARY_MAX_CHARS`: if it does not fit, non-⚠ lines are
dropped with an explicit `(+N more changes not shown)` line; if the ⚠ lines alone do not
fit, the proposal is `rejected_invalid` and the agent is asked to split it into smaller
edits. `No manifest fields change` appears only when the parsed manifests are identical.

Apply (`CapabilityEditHandler.apply` on Approve; per-capability `asyncio.Lock`, file I/O in
`asyncio.to_thread`):
1. Re-resolve the file (must be the same path), re-read it. sha256 ≠ `base_sha256` →
   `failed`/`stale` ("changed between proposal and approval … nothing was written").
2. `self_edit` revoked by a human in the meantime → `failed`/`not_editable`.
3. Re-run the replacement (or read the history entry) and the full validation; the result
   must hash to the approved `new_sha256`.
4. Back up the current bytes to `<config_dir>/.capability_history/<capability_id>/<history_id>.yaml`
   (`history_id = <UTC YYYYmmddTHHMMSSffffff>Z__<sha8>`) plus a `.json` sidecar
   `{history_id, created_at, op, capability_id, file, sha256, new_sha256, reason,
   confirmation_id, session_id, user_id, base_revision, target_history_id}`, both written
   atomically. A failed backup aborts with `backup_failed`.
5. Atomic write of the new content (`core/capabilities/atomic_file.py`: temp file in the
   target's directory, fsync, `os.replace`, file mode kept, best-effort directory fsync).
   A failed write leaves the original intact and removes the backup (`write_failed`).
6. `catalog.reload("capability_edit:<confirmation_id>")`. If the catalog does not serve the
   new content (e.g. another manifest broke meanwhile and the reload was rejected as a
   whole), the previous bytes are restored and the outcome is `failed`/`reload_rejected`.
   Otherwise the outcome is `applied` with `revision N → M` and the backup's history id.
7. History is pruned to the newest `app.capability_history_retention` versions per
   capability (default 50) after a successful apply.

The handler never reports `applied` unless the write happened and the catalog serves it;
the `[[CONFIRMATION_RESOLVED]]` record carries the real status and detail. Reject / expire
write nothing. Audit: `capability_edit.proposed` (tool), `capability_edit.applied`,
`capability_edit.rolled_back`, `capability_edit.failed`, `capability_edit.restored`,
`capability_edit.history_pruned`, with `confirmation_id`, `session_id`, `user_id`,
`capability_id`, `op`, `base_sha256`/`new_sha256` and the revisions; `capabilities.reloaded`
logs the revision bump with `reason=capability_edit:<confirmation_id>`.

History and rollback:
- The history directory is a hidden sibling of `capabilities/` in the active config dir, so
  it travels with the config and the catalog never loads it as manifests. `.capability_history/`
  is in `.gitignore` (this matters when the active config dir is the tracked repo default
  `config/`). Backups are verbatim copies of the manifests, so they contain whatever the
  manifests contain, including inline `claude_code_settings.mcp_servers.*.env` secrets
  (backup files are created with mode `0600`). When the history dir is inside a git work
  tree and `git check-ignore` does not confirm that it is ignored (or git is unavailable),
  `CapabilityEditService` logs `capability_edit.history_in_git_work_tree` (warning, at most
  once per process): keep the directory ignored and never commit it.
- Each entry is the content **before** an applied edit or rollback, so a rollback can itself
  be rolled back. `capability_propose_rollback` validates the entry (well-formed id, sidecar
  hash matches the file) and the restored content (same checks as an edit, including
  `self_edit: true`) and goes through the same confirmation and apply steps.
- A human can always roll back by copying a history file over the manifest; live reload
  picks it up.

Known limitation (out of scope by owner decision): sub-agents with direct filesystem
access (Claude Code tasks with `permission_mode` `bypassPermissions`) can write
`<config_dir>/capabilities/*.yaml` directly. With live reload such a write takes effect on
the next turn **without** a confirmation and without a history entry. External edits are
not backed up by the catalog.

Operator setup: add the four `capability_*` entries from `config/tools.yaml` to the active
`tools.yaml`, enable `capability_admin` (policy or nesting), and mark the manifests the
agent may edit with `self_edit: true`.

> **Secrets in editable manifests.** `capability_read` returns the **full raw YAML** of an
> opted-in manifest to the model (and therefore to the provider and the session store),
> including any inline secrets such as `claude_code_settings.mcp_servers.*.env` values. The
> history backups hold the same content. The confirmation summary masks env/header values,
> but the model still sees them. Keep secrets out of `self_edit` manifests: put them in the
> environment of the process or in a non-editable manifest, and reference them from there.

Event loop: `capability_propose_edit` / `capability_propose_rollback` run the service's file
I/O and validation (including entrypoint resolution) in `asyncio.to_thread`; the sync tools
`capability_read` / `capability_history` are run in a worker thread by pydantic-ai, and
apply runs in `asyncio.to_thread` under the per-capability lock.

## Static Activation Model and Tool-Call Invariants (current, in force)

Active capabilities are static, resolved by `resolve_active_capabilities` over the CapabilityCatalog
snapshot ([#53](https://github.com/yogson/private-claw/issues/53) /
[PR #56](https://github.com/yogson/private-claw/pull/56)):

> config `enabled_capabilities`, or the per-session `/capabilities` override when set (the override
> **replaces** `enabled_capabilities` for that session, it does not add to it), expanded through
> `nested_capabilities`, minus `denied_capabilities`.

The system prompt and tool definitions are built from this full active set.

These rules are normative and enforced today:
- Startup: the runtime registry discovers and validates all manifests (invalid or duplicate
  manifests are startup errors; on live reload an invalid snapshot is rejected and the
  last-known-good stays in use).
- Capability execution originates only from provider-native tool calls; plain text or JSON payloads
  in model output are never treated as tool execution requests.
- Tool definitions passed to the model stay compact and contain only what is needed for safe
  invocation; never pass raw implementation internals or unrestricted shell command text to the
  model.
- For MCP-backed tools, never pass raw MCP transport credentials or command/env internals to the
  model.

## Dynamic Activation Model (Not planned)

> **Status: not implemented, not planned.** The owner decided not to pursue dynamic capability
> activation/loading for now (see [#55](https://github.com/yogson/private-claw/issues/55), closed as not planned, and closed [PR #60](https://github.com/yogson/private-claw/pull/60); the branch
> `feat/dynamic-capability-loading` is kept for reference). The current model is **static**, as
> described in the section above.
>
> This section and the sub-sections below whose headings carry "(Not planned)" ("Catalog Query
> Contract", "Tool Context Injection Rules" and "Example: Reminder Request") are a **historical
> design sketch only**. They are not a requirement and do not describe current behavior. (The
> tool-call and safety invariants in the static-activation section above are not part of the
> sketch and remain in force.)

In the sketched design, the system would separate **runtime registry availability** from **model-visible tool context**.

- The model would not receive all tool definitions by default.
- Per turn, only shortlisted capability descriptors would be injected into model context.

This is the sketched activation flow:

1. Build candidate set from intent signals:
   - user request text/entities,
   - memory intent-to-capability mappings,
   - optional skill `required_capabilities`.
2. Query catalog/registry and rank candidates (`top_n` small, recommended 1-3).
3. Enforce policy gates before model exposure:
   - remove denied capabilities,
   - keep only allowlisted capabilities for current context.
4. Load tool descriptors for survivors only.
5. Inject descriptor subset into model context and execute.
6. If no viable capability remains:
   - ask targeted clarification, or
   - return actionable refusal/guidance for capability registration.

For MCP-backed capabilities, steps 2-4 would additionally enforce:
- server connectivity and health checks,
- tool-level allowlisting from MCP mapping config,
- risk-class confirmation gates before descriptor injection.

## Catalog Query Contract (Not planned)

`CMP_TOOL_RUNTIME_REGISTRY` would provide deterministic catalog-query operations:

- `list_capabilities(filters)`:
  - filters by domain, side_effecting/read_only, and policy eligibility.
- `resolve_candidates(intent, entities, top_n)`:
  - returns ranked capability IDs with score + reason.
- `get_tool_descriptors(capability_ids)`:
  - returns compact LLM-facing descriptors for context injection.

Response records would include:
- `capability_id`,
- `score`,
- `selection_reason`,
- `policy_state` (`allowed`, `denied`, `requires_confirmation`).

## Tool Context Injection Rules (Not planned)

- Keep descriptor payload compact and deterministic.
- Include only fields needed for safe invocation: name, arguments, constraints, confirmation requirement.
- Include safety-critical notes verbatim when `requires_confirmation=true`.
- Maintain audit metadata of which descriptors were injected for each turn.

For MCP descriptors:
- include `mcp_server` and `mcp_tool` metadata for traceability.

## Example: Reminder Request (Not planned)

> **Illustrative of the not-planned dynamic model.** This walkthrough shows the historical
> shortlist/descriptor-injection flow and does **not** describe current behavior. In the current
> static model the reminder tools come from the active capability set (for example
> `macos_personal`, when enabled), are already in the model's tool context, and the model calls
> them via a native tool call with policy gates enforced at invocation.

For a user turn like "set a reminder for tomorrow at 9 to pay rent":

1. Intent resolves to reminder creation.
2. Catalog query returns candidates (for example `cap.macos.reminders.write`).
3. Policy gate confirms capability is allowed in current runtime.
4. Inject only reminder-related descriptor(s) into context.
5. Execute capability with normalized arguments.
6. Persist audit: candidate scores, chosen capability, descriptor IDs injected, execution outcome.

## Inputs

- Capability/skill manifests and runtime configuration policies.
- MCP server registry and tool mapping configs.
- Invocation requests from orchestrator and skills.

## Outputs

- Capability and skill execution results.
- MCP invocation results normalized to capability result contract.
- Policy decision events and execution audits.

## Constraints

- All side-effecting capabilities must pass explicit capability checks.
- Shell and macOS automation commands must use allowlisted patterns.
- External MCP tools must be explicitly allowlisted by server and tool name.
- Capability failures must return normalized errors without crashing orchestrator.
- High-risk operational procedures (for example, production deployments) must not rely on memory retrieval alone.

## External MCP Capability Bridge (Normative)

External MCP servers are integrated through `CMP_TOOL_RUNTIME_REGISTRY` as capability providers, not as direct orchestrator dependencies.

Required bridge behavior:
- Discover enabled MCP servers from `config/mcp_servers.yaml` at startup.
- Establish MCP client sessions and fetch tool metadata.
- Expose server tools to the agent as `mcp_<server>_<tool>` (filtered by `include_tools` / `exclude_tools`).
- Synthesize or load capability descriptors for mapped MCP tools.
- Apply capability policy gates before model exposure and invocation.
- Normalize MCP responses/errors into standard capability result/error format.

Risk and policy handling:
- `readonly` MCP tools may run without confirmation when policy allows.
- `interactive` and `side_effecting` MCP tools must enforce confirmation policy before execution (target behaviour; the current runtime only notifies before the call and does not block it).
- Server-level deny policy must block all mapped tools regardless of per-tool configuration.

Audit requirements for MCP invocations:
- record `capability_id`, `mcp_server`, `mcp_tool`,
- record policy state and confirmation decision,
- record normalized inputs (or redacted hash for sensitive payloads),
- record execution outcome and latency.

Example mapping (chrome-devtools):
- `mcp_chrome_devtools_browser_navigate`
- `mcp_chrome_devtools_browser_snapshot`
- `mcp_chrome_devtools_browser_click`
- `mcp_chrome_devtools_browser_type`

## Capability vs Memory Boundary

Capabilities and memory serve different purposes and must not be treated as interchangeable.

- Capabilities encode deterministic, repeatable procedures with policy checks, confirmation gates, timeout behavior, retries, and auditability.
- Memory stores contextual facts and routing hints (for example, project defaults, aliases, preferences, and recent decisions).
- Memory may guide capability selection, but memory content must not directly execute side-effecting multi-step procedures.

### Capability-Required Cases

A user-provided procedure must be represented as a capability (or capability template + project config) when any of the following are true:

- The workflow is side-effecting and high risk (for example, production deploy, release tagging, infrastructure changes).
- The workflow has ordered dependent steps (for example, update files -> push tag -> wait for build -> update deployment manifest -> push).
- The workflow requires external-state waits or gate checks (for example, CI completion, status checks).
- The workflow must be repeatable with stable behavior across sessions.
- The workflow requires clear audit logs and policy enforcement.

### Memory-Only Cases

Memory-only representation is acceptable for low-risk context:

- User preferences (for example, concise response style).
- Stable factual metadata (for example, repository alias, default branch name).
- Ephemeral task context that does not trigger privileged or destructive actions.

### Promotion and Versioning Rules

- If a memory-stored procedure is reused or considered safety-critical, promote it to a capability before autonomous execution.
- On first capture of a procedural workflow, require user confirmation to save as a capability.
- When procedure semantics change, do not silently mutate prior behavior: either create a new capability version, or edit an opted-in (`self_edit: true`) manifest through the `capability_edit` flow, where every change is explicitly approved by the user and the previous version is kept in the capability history.
- Memory should store mapping metadata, such as: `intent=deploy_to_prod`, `project=<id>`, `capability_id=<cap.deploy.prod.vN>`, and default input values.

### Execution Contract

- Intent resolution may start from memory, but execution must be delegated to a registered capability runtime.
- If no capability exists for a high-risk intent, the system must request confirmation or refuse autonomous execution with an actionable error.
- Capability invocation records must include resolved inputs, policy decisions, and outcome status for post-action review.

## Risks

- Unsafe command execution without strict policy control.
- Manifest dependency mismatch or capability drift.
- Overreliance on memory for operational runbooks can cause step omission, order drift, and inconsistent safety checks.

## Done Criteria

- Manifests load deterministically and invalid manifests are rejected.
- Allowed capability actions execute successfully with audit logs.
- Blocked actions produce policy errors with clear diagnostics.

