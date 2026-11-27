# Telegram Domain

## Purpose

Define the Telegram interaction boundary for Personal AI Assistant v1, including inbound polling updates, outbound responses, multimodal ingestion, and channel-level safety controls.

## Owned Components

- `CMP_CHANNEL_TELEGRAM_ADAPTER`
- `CMP_CORE_AGENT_ORCHESTRATOR` (integration dependency)
- `CMP_OBSERVABILITY_LOGGING` (audit dependency)

## Scope

- Receive and validate Telegram updates via long-polling (no webhook or public URL required).
- Enforce user allowlist checks.
- Normalize text, attachment, and voice events into internal event contracts.
- For voice messages, invoke Telegram MTProto transcription worker synchronously and map successful output to `transcript_text` before orchestrator handoff.
- Send final assistant responses and operational notices to Telegram.
- Support interactive Telegram UI elements (inline keyboards/buttons) for guided user flows.
- Process callback query events from button clicks and map them to normalized events.
- Support session-resume selection flows where user can list recent sessions and pick one to continue.
- Apply retry and throttling behavior for channel reliability.

## Inputs

- Telegram updates (delivered via long-polling).
- Runtime channel configuration and allowlist.
- Telegram voice message metadata and MTProto transcription worker result (`transcript_text` when available).
- Telegram callback query payloads from inline UI interactions.

## Outputs

- Normalized inbound event objects for orchestrator processing.
- Outbound message delivery events.
- Outbound interactive payloads (message text + inline keyboard metadata).
- Channel audit logs for authorization and delivery outcomes.

## Interactive UI Strategy

- v1 supports Telegram inline keyboards/buttons for structured interactions.
- Main agent may return interactive response payloads for:
  - ad-hoc quizzes,
  - selection prompts,
  - confirmation/cancellation actions.
- Button payloads must include stable callback identifiers and signed context data.
- Callback events are normalized and handled through the same turn lifecycle as text messages.

### Session Resume Selection Flow

- v1 supports a guided "resume session" flow triggered by the `/sessions` bot command:
  - user sends `/sessions` (or `/sessions@botname` in group contexts),
  - adapter returns an interactive message with the latest N resumable sessions scoped to the requesting chat,
  - user selects one via inline button callback,
  - selected `session_id` becomes active for subsequent turns in the same chat context.
- Session list entries include compact user-facing metadata:
  - session label (generated title or fallback from first user message),
  - last activity timestamp,
  - short preview snippet (bounded length, sanitized).
- Resume callback payload format (current):
  - `rs:{session_id}:{ts36}:{sig}`,
  - `ts36` is a hex-encoded Unix timestamp used for TTL enforcement (1 hour),
  - `sig` is HMAC-SHA256 over `"{chat_id}:{session_id}:{ts36}"` (truncated),
  - `chat_id` is not embedded in payload fields; chat binding is cryptographic via signature input.
- Session listing is scoped to the requesting chat's sessions only; sessions from other chats are never returned.
- Session selection must remain within the same allowlisted user/chat context; cross-user/session escalation is forbidden.

### Confirmation Flow

Lifecycle and policy are owned by the Agent domain (`domains/agent.md`, "User Confirmations"); this section owns
the Telegram transport only.

- One message per new confirmation, sent after the turn's own response: kind title, `summary_for_user`
  (for memory: action, memory type, body or target `memory_id`; for `capability_edit`: the field-level change
  summary), and Approve / Reject inline buttons. The prompt is sent as plain text (`ChannelResponse.plain_text`),
  like the edit after the press: no markdown conversion, so the user sees exactly the text they approve.
- Attachments (e.g. the full prompt diff of a `capability_edit`): every part is sent first, in order, as its own
  plain-text message headed `<title> — part i/N (confirmation <tag>)`, where the tag is 8 hex chars of an HMAC of
  the confirmation id under a per-process key. Then the button message
  follows, sent as a Telegram **reply to part 1** (`ChannelResponse.reply_to_message_id`, with
  `allow_sending_without_reply=False`, so a deleted part 1 fails the send instead of dropping the binding).
  Parts 2..N reply to part 1 too. The button message adds `📎 Sent in full above: <title> (N parts). This message
  is a reply to part 1 (confirmation <tag>); read every part before deciding.` The agent's own text is sent
  before all of this and is never a reply. It can imitate the header layout, but not the tag (the agent knows
  the confirmation id from the tool result, never the key), and it cannot change, remove or reorder the real
  parts: the bot sends them directly above the buttons, and the button message replies to the real part 1. The buttons are sent only after every
  part was delivered with a message id. If a part fails (send error, no id reported, or a rendered part over
  4096 UTF-16 units, which core bounds make impossible), nothing more is sent for that confirmation. It is
  discarded as `not_delivered` (`failed`, session slot released), so no buttons exist for it. A failed button
  send after the parts is handled the same way. On resolve or expiry only the button message is edited; the
  parts stay as they are.
- Size: the prompt is cut to 4096 − 300 UTF-16 units with an explicit `… [truncated]` mark, so it is always one
  message. The final edit truncates the base text so `base + status` fits in 4096 and the status line is never lost.
- Callback payload: `cf:{confirmation_id}:{a|r}:{sig}`; `sig` is HMAC-SHA256 (truncated) over
  `"cf:{chat_id}:{session_id}:{confirmation_id}:{a|r}"`. The `cf:` label separates it from other callbacks signed
  with the same secret. The session id is not in the payload: a known request is verified against its own session,
  and an unknown id (e.g. after a restart) against the chat's current session. In the unknown case a valid
  signature makes the handler write an `expired` record to that session. No token store; expiry is enforced by the
  confirmation service.
- Confirmation callbacks are not pre-acknowledged by the polling loop. The handler answers
  `answerCallbackQuery` with the real outcome (approved label per kind: `✅ Saved.` for memory, default
  `✅ Approved.`; `❌ Rejected.`, `⌛ Expired …`, `⚠️ Failed: …`) and edits the
  original message (`origin_message_id`) to show the status and drop the keyboard.
- Any `cf:` or legacy `mc:` callback that is unknown, expired, forged, or from before a restart is answered as
  "no longer valid" and its message edited; such callbacks never reach the orchestrator/LLM.
- Callback acknowledgements are wrapped in try/except and logged; a failed ack never aborts handling.

### Session Reset Flow

- v1 supports explicit session context reset via `/reset` (and `/reset@botname` in group contexts).
- The command clears persisted conversation history for the currently active session context in the chat:
  - default chat session (`tg:{chat_id}`), or
  - currently activated resume-session override (if selected earlier).
- After reset, subsequent turns in that session start as a fresh conversation (no prior replayed context).
- The command is processed at adapter/API handler level and bypasses normal orchestrator turn execution.
- `/reset` runs in the session's turn slot (like `/compact`): it waits in the turn queue behind a running turn or
  compaction, so it cannot interleave with them. If the slot stays busy past the queue timeout
  (`SESSION_LOCK_WAIT_TIMEOUT_SECONDS`, 300 s), the user gets a busy reply ("Session is busy. Try again once the
  current turn finishes.") and nothing is archived or cleared.
- When a turn fails because the history exceeds the model's token limit, the handler resets the session
  automatically (archive usage + clear, when reset is available). This runs after the failed turn released its slot,
  and takes the slot the same way as `/reset`; if the slot stays busy past the timeout, the session is not reset and
  the user is told to send `/reset` once the current turn finishes.

### New Session Flow

- v1 supports explicit new-session creation via `/new` (and `/new@botname` in group contexts).
- The command creates a fresh chat-scoped session ID (`tg:{chat_id}:{suffix}`) and switches active chat context to it immediately.
- The command is processed at adapter/API handler level and bypasses normal orchestrator turn execution.
- Subsequent user turns in the same chat are routed to the newly activated session until another session switch command/callback is applied.

### Model Selection Flow

- v1 supports per-chat LLM model selection via the `/model` bot command:
  - user sends `/model` (or `/model@botname` in group contexts),
  - adapter returns an interactive message listing models from `config/model.yaml` `model_allowlist`,
  - user selects one via inline button callback,
  - selected `model_id` becomes the active model override for subsequent turns in the same chat (stored per chat, including group/supergroup with negative chat IDs).
- Model list is sourced from `config/model.yaml` `model_allowlist`.
- Callback payload format: `ms:{model_id}:{ts36}:{sig}` (same TTL and HMAC binding as session resume).
- Model override is persisted per chat in `runtime/active_model_context.json`.

### Commands Menu Flow

- v1 configures Telegram native bot commands during polling startup using Bot API command metadata:
  - `/new` - start a fresh session for the current chat,
  - `/reset` - clear context for the currently active session,
  - `/compact` - manually compact the active session (summarize older turns, keep the last `keep_recent_turns` verbatim); bypasses `token_threshold`, `min_turns_before_compact` and `max_compactions` but still honors `enabled`,
  - `/wtf` - recap the active session in a few lines (one-shot LLM call over user/assistant dialogue only, tool calls excluded; model and budget in `config/recap.yaml`). Reads history without the session lock, so it answers during an active turn; the recap is never persisted or added to the agent context,
  - `/sessions` - list recent sessions and resume one via inline callbacks,
  - `/model` - select LLM model for the current session,
  - `/usage` - show token and cost usage statistics for current session, today (UTC), and this month (UTC).
- `/usage` is processed at adapter/API handler level and bypasses normal orchestrator turn execution.
- Usage aggregation is user-scoped: only usage attributed to the requesting user (via `user_id` on assistant records or turn-level correlation) is counted; shared-session usage from other participants is excluded.

### Usage Stats Flow

- v1 supports usage statistics via `/usage` (and `/usage@botname` in group contexts).
- The command returns three sections: current session, today (UTC), and this month (UTC).
- Token counts and estimated costs are derived from persisted assistant message records with usage payloads.
- Cost calculation uses static local [genai-prices](https://github.com/pydantic/genai-prices) data (provider: anthropic).
  The stored `input_tokens` is the run total and includes cache reads/writes; the usage payload also stores
  `cache_read_tokens`, `cache_write_tokens` and `cache_write_1h_tokens`, so cached input is priced at the model's cache rates.
  Records without these fields (written before they existed) are priced as uncached input.
  A turn genai-prices cannot price (unknown model, inconsistent cache data) counts as $0, logs a
  `usage.cost_unknown` warning with the model id and reason, and `/usage` adds a note that the cost is incomplete.
- Usage of removed turns is kept in a per-user archive: `/reset` archives the session's usage before clearing it, and
  compaction (automatic and `/compact`) archives the usage of the turns it summarizes away. The turns compaction keeps
  verbatim stay in the session and are not archived, so they are not counted twice. `/reset` runs in the session's
  turn slot, so it waits for a running turn or compaction instead of interleaving with it. Archive rows keep the
  cache split (`cache_read_tokens`, `cache_write_tokens`, `cache_write_1h_tokens`) and a frozen `cost_usd`
  (`null` when the turn could not be priced).
- User attribution is enforced at turn level: assistant records carry `user_id`; records without it are correlated via the turn's user_message. Only usage attributed to the requesting user is included.
- v1 explicitly configures Telegram `MenuButtonCommands` so the client menu opens the registered command list.
- Command matching in runtime is centralized in a shared command parser that supports Telegram command forms:
  - `/command`,
  - `/command@botname` (group contexts),
  - case-insensitive token parsing.
- Command registration failures must not block polling startup; they are logged and polling continues.

### Rich Messages (Tables)

- Formatted agent replies (no `parse_mode`, not `plain_text`) can be sent as native rich messages via Bot API
  `sendRichMessage` with `InputRichMessage(markdown=<original agent Markdown>)`, so GFM tables render natively
  (alignment, links and bold inside cells) instead of as a monospace `pre` block.
- `rich_messages` in `channel.telegram.yaml`: `off` (never), `tables_only` (default: only replies with a GFM pipe
  table outside fenced code blocks), `all` (every formatted reply).
- `reply_parameters` go on the first message and `reply_markup` on the last, as on the entity path.
- Splitting: content over 32,768 UTF-8 bytes or ~500 blocks (each text line and table row counted conservatively) is
  split at blank-line block boundaries, never inside a table or code block. An oversized table is split into row
  groups, each repeating the header and separator rows. If the content cannot be split safely (oversized code block,
  table over 20 columns), the reply uses the entity path.
- Fallback: any API error from `sendRichMessage` (except flood control and network errors, which are retried as
  usual; also 403, "message to be replied not found" and 5xx, which would fail on the entity path too and are
  handled as on the legacy path) resends that chunk and the rest through the entity path; chunks already delivered are not resent. A 404 or
  a "method not found"/"unknown method"/"method is not supported" error sets a process-wide latch that disables the rich path for later sends
  (logged once as `telegram.egress.rich_messages_unsupported`). The plain-text retry on "can't parse entities"
  stays the last resort. Rich failures are reported to the audit logger like other egress API errors.
- Tables the detector does not recognize (inside blockquotes or list items, after a line starting with ```` ```x``` ````,
  or with `\\|`) fall back to the legacy rendering in `tables_only` mode.
- Message edits (`edit_message_text`) and confirmation prompts (plain text) are unchanged.

## Voice Handling Strategy

- v1 uses a dedicated Telegram MTProto transcription worker (Pyrogram/Telethon user client) to access Telegram built-in voice transcription.
- Ingress flow is synchronous for voice messages: adapter requests transcription during intake and waits up to configured timeout.
- On success, adapter writes `transcript_text` on the voice metadata model and passes normalized event downstream in the same turn.
- On timeout, unsupported media, or permission/quota failures, adapter must continue intake with `transcript_text=null` and emit structured audit metadata.
- Bot API remains the transport for normal bot ingress/egress; MTProto worker is used only for transcription requests/results.
- No external speech-to-text infrastructure is introduced in v1 baseline.

## Table Rendering on the Entity Path

- On the entity path (Markdown converted to text and entities by telegramify-markdown), `table_render.render_tables`
  first rewrites GFM pipe tables outside fenced code blocks, so they read well on a phone. Native rich messages, where
  enabled, send the original Markdown and do not use this; the fallback from the rich path (a rejected
  `sendRichMessage`, see Rich Messages above) resends through the entity path and therefore also goes through this
  renderer.
- Display width counts emoji and East Asian wide characters as 2 cells and combining marks, zero-width joiners and
  variation selectors as 0 (`wcwidth`). The budget is `TABLE_WIDTH_BUDGET` = 38 monospace cells.
- Layout:
  - grid: the whole table fits the budget. A compact monospace grid in a `pre` block: columns separated by ` | `, a
    `-+-` separator line as wide as the content, `:--:` / `--:` alignment honored, no trailing whitespace. Inline
    Markdown is stripped from cells (links become their text), since `pre` cannot be formatted;
  - lines: 2 columns that do not fit. One `**<col1>** — <col2>` line per row; the header row is dropped;
  - cards: 3+ columns that do not fit. Per row, the first cell in bold as a title, then one `<header>: <value>` line per
    non-empty cell, with a blank line between cards.
  Lines and cards keep inline Markdown in cells (bold, code, links with their URL), so they become entities.
- Parsing follows GFM: `\|` is a literal pipe, missing cells are empty, extra cells are dropped, outer pipes are
  optional. A table ends at a line without an unescaped pipe. A header-only table that does not fit becomes one bold
  line of headers. Tables inside blockquotes or list items are left as is.
- The system prompt asks the model to use tables only for compact data (up to ~4 short columns) and bulleted lists
  otherwise.

## Constraints

- Single-user allowlist model in v1.
- No direct business logic execution in adapter layer.
- Multimodal handling must degrade gracefully if processing fails.
- Telegram transcription worker requires user-client credentials (`api_id`, `api_hash`) and operational access to chats where voice messages are transcribed.
- Callback payloads must be validated and protected against replay/tampering.
- Native command menu bootstrap must be idempotent across restarts.

## Risks

- Delivery failures and API rate limits.
- Inconsistent media metadata from Telegram update variants.
- Callback payload version drift between sent UI and handler logic.

## Done Criteria

- Allowlisted requests are accepted and normalized.
- Unauthorized requests are blocked and logged.
- Text/attachment/voice messages flow through the same normalized contract.
- Voice messages trigger synchronous transcription attempt; successful transcriptions are persisted as `transcript_text` in normalized voice metadata.
- Interactive responses with inline buttons are rendered and callback actions are handled correctly.
- Session resume flow lists recent sessions and reliably switches active session on user selection.
- Outbound send failures follow retry policy and emit diagnostics.

