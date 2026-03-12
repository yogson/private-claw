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
- Resume callback payload is signed with HMAC-SHA256 and includes:
  - bound `chat_id` (prevents cross-chat use of another chat's callback),
  - Unix timestamp (TTL enforcement rejects callbacks older than 1 hour),
  - target `session_id`.
- Session listing is scoped to the requesting chat's sessions only; sessions from other chats are never returned.
- Session selection must remain within the same allowlisted user/chat context; cross-user/session escalation is forbidden.

### Session Reset Flow

- v1 supports explicit session context reset via `/reset` (and `/reset@botname` in group contexts).
- The command clears persisted conversation history for the currently active session context in the chat:
  - default chat session (`tg:{chat_id}`), or
  - currently activated resume-session override (if selected earlier).
- After reset, subsequent turns in that session start as a fresh conversation (no prior replayed context).
- The command is processed at adapter/API handler level and bypasses normal orchestrator turn execution.

## Voice Handling Strategy

- v1 uses a dedicated Telegram MTProto transcription worker (Pyrogram/Telethon user client) to access Telegram built-in voice transcription.
- Ingress flow is synchronous for voice messages: adapter requests transcription during intake and waits up to configured timeout.
- On success, adapter writes `transcript_text` on the voice metadata model and passes normalized event downstream in the same turn.
- On timeout, unsupported media, or permission/quota failures, adapter must continue intake with `transcript_text=null` and emit structured audit metadata.
- Bot API remains the transport for normal bot ingress/egress; MTProto worker is used only for transcription requests/results.
- No external speech-to-text infrastructure is introduced in v1 baseline.

## Constraints

- Single-user allowlist model in v1.
- No direct business logic execution in adapter layer.
- Multimodal handling must degrade gracefully if processing fails.
- Telegram transcription worker requires user-client credentials (`api_id`, `api_hash`) and operational access to chats where voice messages are transcribed.
- Callback payloads must be validated and protected against replay/tampering.

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

