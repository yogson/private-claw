# Telegram Domain

## Purpose

Define the Telegram interaction boundary for Personal AI Assistant v1, including inbound updates, outbound responses, multimodal ingestion, and channel-level safety controls.

## Owned Components

- `CMP_CHANNEL_TELEGRAM_ADAPTER`
- `CMP_CORE_AGENT_ORCHESTRATOR` (integration dependency)
- `CMP_OBSERVABILITY_LOGGING` (audit dependency)

## Scope

- Receive and validate Telegram updates.
- Enforce user allowlist checks.
- Normalize text, attachment, and voice events into internal event contracts.
- For voice messages, extract Telegram-provided voice-to-text transcription when available and pass transcript text to orchestrator.
- Send final assistant responses and operational notices to Telegram.
- Support interactive Telegram UI elements (inline keyboards/buttons) for guided user flows.
- Process callback query events from button clicks and map them to normalized events.
- Support session-resume selection flows where user can list recent sessions and pick one to continue.
- Apply retry and throttling behavior for channel reliability.

## Inputs

- Telegram webhook or polling updates.
- Runtime channel configuration and allowlist.
- Telegram voice message metadata including platform-provided transcript fields (when present).
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

- v1 should support a guided "resume session" flow in Telegram:
  - user requests recent sessions (for example: "resume", "show recent sessions"),
  - adapter/orchestrator returns an interactive message with latest N resumable sessions,
  - user selects one via inline button callback,
  - selected `session_id` becomes active for subsequent turns in the same chat context.
- Session list entries should include compact user-facing metadata:
  - session label (generated title or fallback),
  - last activity timestamp,
  - short preview snippet (bounded length, sanitized).
- Resume callback payload must include signed context with action and target `session_id`.
- Session selection must be scoped to the same allowlisted user/chat context; cross-user/session escalation is forbidden.

## Voice Handling Strategy

- v1 does not require a separate transcription service.
- Voice input path relies on Telegram-native transcription fields when Telegram provides them.
- Adapter converts transcript into canonical text payload used by main agent flow.
- If transcript is missing, adapter responds with: "I could not extract voice text from Telegram. Please resend as text or try another voice message."
- If transcript is partial or low confidence, adapter echoes parsed text and requests user confirmation before high-impact capability execution.

## Constraints

- Single-user allowlist model in v1.
- No direct business logic execution in adapter layer.
- Multimodal handling must degrade gracefully if processing fails.
- Do not introduce external speech-to-text infrastructure in v1 baseline.
- Callback payloads must be validated and protected against replay/tampering.

## Risks

- Delivery failures and API rate limits.
- Inconsistent media metadata from Telegram update variants.
- Callback payload version drift between sent UI and handler logic.

## Done Criteria

- Allowlisted requests are accepted and normalized.
- Unauthorized requests are blocked and logged.
- Text/attachment/voice messages flow through the same normalized contract.
- Voice messages with Telegram transcript are converted into text for main-agent processing.
- Interactive responses with inline buttons are rendered and callback actions are handled correctly.
- Session resume flow lists recent sessions and reliably switches active session on user selection.
- Outbound send failures follow retry policy and emit diagnostics.

