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
- Apply retry and throttling behavior for channel reliability.

## Inputs

- Telegram webhook or polling updates.
- Runtime channel configuration and allowlist.
- Telegram voice message metadata including platform-provided transcript fields (when present).

## Outputs

- Normalized inbound event objects for orchestrator processing.
- Outbound message delivery events.
- Channel audit logs for authorization and delivery outcomes.

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

## Risks

- Delivery failures and API rate limits.
- Inconsistent media metadata from Telegram update variants.

## Done Criteria

- Allowlisted requests are accepted and normalized.
- Unauthorized requests are blocked and logged.
- Text/attachment/voice messages flow through the same normalized contract.
- Voice messages with Telegram transcript are converted into text for main-agent processing.
- Outbound send failures follow retry policy and emit diagnostics.

