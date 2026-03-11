"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

TelegramAdapter: composed entry point for Telegram ingress and egress.
Wires together AllowlistGuard, TelegramIngress (with optional transcription),
TelegramEgress, ChannelThrottleGuard, ChannelAuditLogger, and
SessionResumeService for the session-resume selection flow.
"""

import uuid
from typing import Any

import structlog
from aiogram.types import Update

from assistant.channels.telegram.allowlist import AllowlistGuard, UnauthorizedUserError
from assistant.channels.telegram.egress import TelegramEgress
from assistant.channels.telegram.ingestion.transcription import VoiceTranscriptionService
from assistant.channels.telegram.ingress import TelegramIngress
from assistant.channels.telegram.models import ChannelResponse, MessageType, NormalizedEvent
from assistant.channels.telegram.reliability.audit import ChannelAuditLogger
from assistant.channels.telegram.reliability.throttle import ChannelThrottleGuard
from assistant.channels.telegram.session_resume import SessionResumeService
from assistant.core.config.schemas import TelegramChannelConfig
from assistant.store.interfaces import SessionStoreInterface

logger = structlog.get_logger(__name__)


class TelegramAdapter:
    """
    Composed Telegram channel adapter.

    Exposes process_update() for sync ingress normalization and
    process_update_async() for voice-transcription-enriched normalization.
    Enforces allowlist and per-user throttle on every inbound update.
    Emits structured audit telemetry for ingress and egress events.

    Pass a VoiceTranscriptionService at construction to enable synchronous
    MTProto transcript enrichment for voice messages.

    Pass a SessionStoreInterface to enable the guided session-resume flow:
    users can list recent sessions and switch the active session via signed
    inline keyboard callbacks.
    """

    def __init__(
        self,
        config: TelegramChannelConfig,
        transcription_service: VoiceTranscriptionService | None = None,
        session_store: SessionStoreInterface | None = None,
    ) -> None:
        self._config = config
        guard = AllowlistGuard(config.allowlist)
        audit_logger = ChannelAuditLogger()
        throttle_guard = ChannelThrottleGuard(max_per_window=config.throttle_max_per_minute)
        self._ingress = TelegramIngress(
            guard,
            transcription_service=transcription_service,
            throttle_guard=throttle_guard,
            audit_logger=audit_logger,
        )
        self._egress = TelegramEgress(
            bot_token=config.bot_token,
            audit_logger=audit_logger,
        )
        self._session_resume: SessionResumeService | None = None
        if session_store is not None:
            secret = config.session_resume_hmac_secret
            self._session_resume = SessionResumeService(
                session_store=session_store,
                hmac_secret=secret,
                max_sessions=config.session_resume_max_sessions,
            )
        # chat_id -> active session_id override (in-memory, resets on restart)
        self._active_sessions: dict[int, str] = {}

    def process_update(self, update: dict[str, Any] | Update) -> NormalizedEvent | None:
        """
        Normalize a raw Telegram update dict into a NormalizedEvent.

        Returns None for unsupported update types.
        Unauthorized users are rejected; UnauthorizedUserError is logged and
        re-raised so the caller can handle the response appropriately.
        Voice events will not have MTProto transcript; use process_update_async().
        Active session overrides (set via session-resume flow) are applied to
        the returned event's session_id.
        """
        try:
            update_payload = (
                update.model_dump(mode="python", exclude_none=True, by_alias=True)
                if isinstance(update, Update)
                else update
            )
            event = self._ingress.normalize(update_payload)
            return self._apply_session_context(event)
        except UnauthorizedUserError:
            raise
        except Exception:
            logger.exception("telegram.adapter.process_update.error")
            return None

    async def process_update_async(self, update: dict[str, Any] | Update) -> NormalizedEvent | None:
        """
        Normalize a Telegram update with MTProto transcription enrichment for voice.

        For voice messages, calls the configured VoiceTranscriptionService before
        returning the event. Falls back to sync normalization when transcription
        is not configured or fails. Active session overrides are applied.
        """
        try:
            update_payload = (
                update.model_dump(mode="python", exclude_none=True, by_alias=True)
                if isinstance(update, Update)
                else update
            )
            event = await self._ingress.normalize_async(update_payload)
            return self._apply_session_context(event)
        except UnauthorizedUserError:
            raise
        except Exception:
            logger.exception("telegram.adapter.process_update_async.error")
            return None

    async def send_response(self, response: ChannelResponse, chat_id: int) -> bool:
        """
        Deliver a ChannelResponse to the specified Telegram chat.

        Returns True on successful delivery. Raises TelegramSendError after
        all retry attempts are exhausted.
        """
        return await self._egress.send(response, chat_id)

    async def acknowledge_callback(self, callback_id: str) -> None:
        """Acknowledges a callback query so Telegram client stops the loading spinner."""
        await self._egress.acknowledge_callback(callback_id)

    def is_session_resume_request(self, event: NormalizedEvent) -> bool:
        """Return True if the event is a session-resume listing request."""
        if self._session_resume is None:
            return False
        return self._session_resume.is_resume_request(event.text)

    def is_session_resume_callback(self, event: NormalizedEvent) -> bool:
        """Return True if the event is a valid, chat-scoped signed session-resume callback."""
        if self._session_resume is None or event.callback_query is None:
            return False
        chat_id = int(event.metadata.get("chat_id", 0))
        return (
            self._session_resume.verify_callback(
                event.callback_query.callback_data, expected_chat_id=chat_id
            )
            is not None
        )

    async def build_session_menu_response(
        self, chat_id: int, session_id: str, trace_id: str
    ) -> ChannelResponse:
        """
        Build and return an interactive ChannelResponse listing recent sessions.

        Returns a plain-text 'not available' response when session resume is not configured.
        """
        if self._session_resume is None:
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=session_id,
                trace_id=trace_id,
                message_type=MessageType.TEXT,
                text="Session resume is not available.",
            )
        entries = await self._session_resume.list_recent_sessions(chat_id)
        return self._session_resume.build_session_menu(entries, session_id, chat_id, trace_id)

    def handle_session_resume_callback(self, event: NormalizedEvent) -> str | None:
        """
        Process a signed session-resume callback and update the active session for the chat.

        Returns the selected session_id on success, None if the payload is invalid.
        The active session persists in memory for subsequent events from the same chat.
        """
        if self._session_resume is None or event.callback_query is None:
            return None
        chat_id = int(event.metadata.get("chat_id", 0))
        session_id = self._session_resume.verify_callback(
            event.callback_query.callback_data, expected_chat_id=chat_id
        )
        if session_id is None:
            logger.warning(
                "telegram.adapter.session_resume.invalid_callback",
                trace_id=event.trace_id,
            )
            return None
        if chat_id:
            self._active_sessions[chat_id] = session_id
            logger.info(
                "telegram.adapter.session_resume.activated",
                chat_id=chat_id,
                session_id=session_id,
                trace_id=event.trace_id,
            )
        return session_id

    def get_active_session(self, chat_id: int) -> str | None:
        """Return the currently active session override for the given chat, or None."""
        return self._active_sessions.get(chat_id)

    def clear_active_session(self, chat_id: int) -> None:
        """Remove any active session override for the given chat."""
        self._active_sessions.pop(chat_id, None)

    def _apply_session_context(self, event: NormalizedEvent | None) -> NormalizedEvent | None:
        if event is None:
            return None
        chat_id = int(event.metadata.get("chat_id", 0))
        active = self._active_sessions.get(chat_id) if chat_id else None
        if active and active != event.session_id:
            return event.model_copy(update={"session_id": active})
        return event

    async def close(self) -> None:
        """Closes underlying network resources."""
        await self._egress.close()
