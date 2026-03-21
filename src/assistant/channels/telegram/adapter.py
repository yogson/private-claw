"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

TelegramAdapter: composed entry point for Telegram ingress and egress.
Wires together AllowlistGuard, TelegramIngress (with optional transcription),
TelegramEgress, ChannelThrottleGuard, ChannelAuditLogger, and
SessionResumeService for the session-resume selection flow.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram.types import Update

from assistant.channels.telegram.allowlist import AllowlistGuard, UnauthorizedUserError
from assistant.channels.telegram.ask_question_callbacks import (
    sign_ask_question_callback,
    verify_ask_question_callback,
)
from assistant.channels.telegram.commands import TelegramCommand, extract_supported_command
from assistant.channels.telegram.egress import TelegramEgress
from assistant.channels.telegram.ingestion.transcription import VoiceTranscriptionService
from assistant.channels.telegram.ingress import TelegramIngress
from assistant.channels.telegram.memory_confirmation_callbacks import (
    sign_memory_confirmation_callback,
    verify_memory_confirmation_callback,
)
from assistant.channels.telegram.capability_select_service import CapabilitySelectService
from assistant.channels.telegram.model_select import ModelSelectService
from assistant.channels.telegram.models import (
    ActionButton,
    ChannelResponse,
    MessageType,
    NormalizedEvent,
)
from assistant.channels.telegram.reliability.audit import ChannelAuditLogger
from assistant.channels.telegram.reliability.throttle import ChannelThrottleGuard
from assistant.channels.telegram.session_resume import SessionResumeService
from assistant.channels.telegram.task_callbacks import sign_task_callback, verify_task_callback
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.config.schemas import TelegramChannelConfig
from assistant.core.session_context import (
    ActiveSessionContextInterface,
    ActiveSessionContextService,
    SessionModelContextInterface,
    SessionModelContextService,
)
from assistant.store.interfaces import SessionStoreInterface

logger = structlog.get_logger(__name__)
_MEMORY_CONFIRMATION_TTL_SECONDS = 3600


@dataclass(slots=True)
class _MemoryConfirmationToken:
    session_id: str
    tool_call_id: str
    approve: bool
    expires_at: datetime


@dataclass(slots=True)
class _DelegationQuestionToken:
    session_id: str
    answer_text: str
    expires_at: datetime


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
    Active session routing is delegated to ActiveSessionContextInterface so
    the selected session can survive process restarts.
    """

    def __init__(
        self,
        config: TelegramChannelConfig,
        transcription_service: VoiceTranscriptionService | None = None,
        session_store: SessionStoreInterface | None = None,
        active_session_context: ActiveSessionContextInterface | None = None,
        model_context: SessionModelContextInterface | None = None,
        model_allowlist: list[str] | None = None,
        default_model_id: str | None = None,
        capability_definitions: dict[str, CapabilityDefinition] | None = None,
        default_capabilities: list[str] | None = None,
    ) -> None:
        self._config = config
        self._session_store = session_store
        self._active_session_context = active_session_context or ActiveSessionContextService()
        self._model_context = model_context or SessionModelContextService()
        self._model_allowlist = model_allowlist or []
        self._default_model_id = default_model_id or ""
        self._default_capabilities: list[str] = list(default_capabilities or [])
        self._capability_overrides: dict[str, list[str]] = {}
        secret = config.session_resume_hmac_secret or config.bot_token
        self._model_select: ModelSelectService | None = None
        if self._model_allowlist and secret:
            self._model_select = ModelSelectService(
                model_allowlist=self._model_allowlist,
                hmac_secret=secret,
            )
        self._capability_select: CapabilitySelectService | None = None
        if capability_definitions and secret:
            self._capability_select = CapabilitySelectService(
                capability_definitions=capability_definitions,
                hmac_secret=secret,
            )
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
        self._memory_confirmation_tokens: dict[str, _MemoryConfirmationToken] = {}
        self._delegation_question_tokens: dict[str, _DelegationQuestionToken] = {}

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
        return extract_supported_command(event.text) == TelegramCommand.SESSIONS

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

    def is_memory_confirmation_callback(self, event: NormalizedEvent) -> bool:
        """Return True if callback_data is a valid signed memory confirmation token."""
        if event.callback_query is None:
            return False
        token = self._verify_memory_confirmation_callback_token(event)
        if token is None:
            return False
        self._cleanup_expired_memory_confirmation_tokens()
        return token in self._memory_confirmation_tokens

    def is_task_callback(self, event: NormalizedEvent) -> bool:
        """Return True if callback_data is a valid signed delegated-task callback."""
        if event.callback_query is None:
            return False
        chat_id = int(event.metadata.get("chat_id", 0))
        token = verify_task_callback(
            callback_data=event.callback_query.callback_data,
            expected_chat_id=chat_id,
            secret=self._task_callback_secret(),
        )
        return token is not None

    def parse_task_callback(self, event: NormalizedEvent) -> tuple[str, str] | None:
        """Parse a task callback and return (task_id, action)."""
        if event.callback_query is None:
            return None
        chat_id = int(event.metadata.get("chat_id", 0))
        return verify_task_callback(
            callback_data=event.callback_query.callback_data,
            expected_chat_id=chat_id,
            secret=self._task_callback_secret(),
        )

    def consume_memory_confirmation_callback(
        self, event: NormalizedEvent
    ) -> tuple[str, str, bool] | None:
        """Consume a signed memory confirmation callback token and return resolution tuple."""
        if event.callback_query is None:
            return None
        token = self._verify_memory_confirmation_callback_token(event)
        if token is None:
            return None
        self._cleanup_expired_memory_confirmation_tokens()
        item = self._memory_confirmation_tokens.pop(token, None)
        if item is None:
            return None
        return item.session_id, item.tool_call_id, item.approve

    def build_ask_question_response(
        self,
        *,
        session_id: str,
        trace_id: str,
        question: str,
        options: list[dict[str, str]],
    ) -> ChannelResponse:
        """Build message with reply keyboard; button text is sent as user message when tapped."""
        actions: list[ActionButton] = []
        for i, opt in enumerate(options):
            raw = (opt.get("label", "") or "").strip()
            label = raw if raw else f"Option {i}"
            actions.append(
                ActionButton(
                    label=label,
                    callback_id="",
                    callback_data="",
                )
            )
        return ChannelResponse(
            response_id=str(uuid.uuid4()),
            channel="telegram",
            session_id=session_id,
            trace_id=trace_id,
            message_type=MessageType.INTERACTIVE,
            text=question,
            ui_kind="reply_keyboard",
            actions=actions,
        )

    def build_delegation_question_response(
        self,
        *,
        chat_id: int,
        session_id: str,
        trace_id: str,
        question: str,
        options: list[str],
    ) -> ChannelResponse:
        """Build a delegation question message.

        Sends an inline keyboard with one button per option when options are
        provided, or a plain text message when no options are given.
        The button callback data is a signed token stored in the adapter so
        that incoming callback queries can be resolved back to an answer string.
        """
        if not options:
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=session_id,
                trace_id=trace_id,
                message_type=MessageType.TEXT,
                text=question,
            )
        actions: list[ActionButton] = []
        for option_text in options:
            token = self._register_delegation_question_token(
                session_id=session_id, answer_text=option_text
            )
            cb = self._sign_delegation_question_callback(token, chat_id)
            actions.append(
                ActionButton(
                    label=option_text,
                    callback_id=f"aq-{token}",
                    callback_data=cb,
                )
            )
        return ChannelResponse(
            response_id=str(uuid.uuid4()),
            channel="telegram",
            session_id=session_id,
            trace_id=trace_id,
            message_type=MessageType.INTERACTIVE,
            text=question,
            ui_kind="inline_keyboard",
            actions=actions,
        )

    def is_delegation_question_callback(self, event: NormalizedEvent) -> bool:
        """Return True if the event is a valid signed delegation question callback."""
        if event.callback_query is None:
            return False
        token = self._verify_delegation_question_callback_token(event)
        if token is None:
            return False
        self._cleanup_expired_delegation_question_tokens()
        return token in self._delegation_question_tokens

    def consume_delegation_question_callback(
        self, event: NormalizedEvent
    ) -> tuple[str, str] | None:
        """Consume a signed delegation question callback and return (session_id, answer_text)."""
        if event.callback_query is None:
            return None
        token = self._verify_delegation_question_callback_token(event)
        if token is None:
            return None
        self._cleanup_expired_delegation_question_tokens()
        item = self._delegation_question_tokens.pop(token, None)
        if item is None:
            return None
        return item.session_id, item.answer_text

    def build_memory_confirmation_response(
        self,
        *,
        chat_id: int,
        session_id: str,
        trace_id: str,
        tool_call_id: str,
        prompt_text: str,
    ) -> ChannelResponse:
        """Build interactive confirmation message for pending memory update intent."""
        confirm_token = self._register_memory_confirmation_token(
            session_id=session_id, tool_call_id=tool_call_id, approve=True
        )
        reject_token = self._register_memory_confirmation_token(
            session_id=session_id, tool_call_id=tool_call_id, approve=False
        )
        confirm_cb = self._sign_memory_confirmation_callback_token(confirm_token, chat_id)
        reject_cb = self._sign_memory_confirmation_callback_token(reject_token, chat_id)
        return ChannelResponse(
            response_id=str(uuid.uuid4()),
            channel="telegram",
            session_id=session_id,
            trace_id=trace_id,
            message_type=MessageType.INTERACTIVE,
            text=prompt_text,
            ui_kind="inline_keyboard",
            actions=[
                ActionButton(
                    label="Confirm memory update",
                    callback_id=f"confirm-{tool_call_id}",
                    callback_data=confirm_cb,
                ),
                ActionButton(
                    label="Reject",
                    callback_id=f"reject-{tool_call_id}",
                    callback_data=reject_cb,
                ),
            ],
        )

    def build_task_result_response(
        self,
        *,
        chat_id: int,
        session_id: str,
        trace_id: str,
        task_id: str,
        status: str,
        summary: str = "",
        fallback_text: str = "",
    ) -> ChannelResponse:
        """Build task completion message with inline actions for status and summary."""
        text = fallback_text.strip() or f"Delegated task `{task_id}` status: *{status}*."
        if summary.strip():
            text = f"{text}\n\n{summary.strip()}"
        status_cb = sign_task_callback(
            task_id=task_id,
            action="status",
            chat_id=chat_id,
            secret=self._task_callback_secret(),
        )
        summary_cb = sign_task_callback(
            task_id=task_id,
            action="summary",
            chat_id=chat_id,
            secret=self._task_callback_secret(),
        )
        return ChannelResponse(
            response_id=str(uuid.uuid4()),
            channel="telegram",
            session_id=session_id,
            trace_id=trace_id,
            message_type=MessageType.INTERACTIVE,
            text=text,
            parse_mode="Markdown",
            ui_kind="inline_keyboard",
            actions=[
                ActionButton(
                    label="Show status",
                    callback_id=f"task-status-{task_id}",
                    callback_data=status_cb,
                ),
                ActionButton(
                    label="Show summary",
                    callback_id=f"task-summary-{task_id}",
                    callback_data=summary_cb,
                ),
            ],
        )

    def is_session_reset_request(self, event: NormalizedEvent) -> bool:
        """Return True when the event text is the /reset command."""
        return extract_supported_command(event.text) == TelegramCommand.RESET

    def is_session_new_request(self, event: NormalizedEvent) -> bool:
        """Return True when the event text is the /new command."""
        return extract_supported_command(event.text) == TelegramCommand.NEW

    def is_model_request(self, event: NormalizedEvent) -> bool:
        """Return True when the event text is the /model command."""
        if self._model_select is None:
            return False
        return extract_supported_command(event.text) == TelegramCommand.MODEL

    def is_model_callback_request(self, event: NormalizedEvent) -> bool:
        """Return True if the event looks like a model-select callback (by prefix)."""
        if self._model_select is None or event.callback_query is None:
            return False
        data = event.callback_query.callback_data or ""
        return data.startswith("ms:")

    def is_capabilities_request(self, event: NormalizedEvent) -> bool:
        """Return True when the event text is the /capabilities command."""
        if self._capability_select is None:
            return False
        return extract_supported_command(event.text) == TelegramCommand.CAPABILITIES

    def is_capabilities_callback_request(self, event: NormalizedEvent) -> bool:
        """Return True if the event looks like a capability-select callback (by prefix)."""
        if self._capability_select is None or event.callback_query is None:
            return False
        data = event.callback_query.callback_data or ""
        return data.startswith("cs:")

    def is_usage_request(self, event: NormalizedEvent) -> bool:
        """Return True when the event text is the /usage command."""
        return extract_supported_command(event.text) == TelegramCommand.USAGE

    def is_stop_request(self, event: NormalizedEvent) -> bool:
        """Return True when the event text is the /stop command."""
        return extract_supported_command(event.text) == TelegramCommand.STOP

    def is_verbose_request(self, event: NormalizedEvent) -> bool:
        """Return True when the event text is the /verbose command."""
        return extract_supported_command(event.text) == TelegramCommand.VERBOSE

    async def build_model_menu_response(
        self, chat_id: int, session_id: str, trace_id: str
    ) -> ChannelResponse:
        """Build and return an interactive ChannelResponse listing available models."""
        if self._model_select is None:
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=session_id,
                trace_id=trace_id,
                message_type=MessageType.TEXT,
                text="Model selection is not available.",
            )
        current_model = (
            self._model_context.get_model_override(self._build_session_context_id(chat_id))
            or self._default_model_id
        )
        return self._model_select.build_model_menu(
            current_session_id=session_id,
            chat_id=chat_id,
            trace_id=trace_id,
            current_model_id=current_model,
        )

    async def build_capabilities_menu_response(
        self, chat_id: int, session_id: str, trace_id: str
    ) -> ChannelResponse:
        """Build and return an interactive ChannelResponse listing available capabilities."""
        if self._capability_select is None:
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=session_id,
                trace_id=trace_id,
                message_type=MessageType.TEXT,
                text="Capability selection is not available.",
            )
        enabled = self._capability_overrides.get(
            self._build_session_context_id(chat_id), self._default_capabilities
        )
        return self._capability_select.build_capabilities_menu(
            current_session_id=session_id,
            chat_id=chat_id,
            trace_id=trace_id,
            enabled_capabilities=enabled,
        )

    def handle_capabilities_callback(self, event: NormalizedEvent) -> str | None:
        """
        Process a signed capability-select callback and toggle the capability for the chat.

        Returns the toggled capability_id on success, None if invalid.
        """
        if self._capability_select is None or event.callback_query is None:
            return None
        chat_id = int(event.metadata.get("chat_id", 0))
        capability_id = self._capability_select.verify_callback(
            event.callback_query.callback_data, expected_chat_id=chat_id
        )
        if capability_id is None:
            logger.warning(
                "telegram.adapter.capability_select.invalid_callback",
                trace_id=event.trace_id,
            )
            return None
        if chat_id != 0:
            context_id = self._build_session_context_id(chat_id)
            current = list(
                self._capability_overrides.get(context_id, self._default_capabilities)
            )
            if capability_id in current:
                current.remove(capability_id)
            else:
                current.append(capability_id)
            self._capability_overrides[context_id] = current
            logger.info(
                "telegram.adapter.capability_select.toggled",
                chat_id=chat_id,
                capability_id=capability_id,
                enabled_capabilities=current,
                trace_id=event.trace_id,
            )
        return capability_id

    def get_capabilities_override(self, chat_id: int) -> list[str] | None:
        """Return capability overrides for the given chat, or None if not customized."""
        if chat_id == 0:
            return None
        context_id = self._build_session_context_id(chat_id)
        return self._capability_overrides.get(context_id)

    def clear_capabilities_override(self, chat_id: int) -> None:
        """Remove any capability override for the given chat."""
        if chat_id <= 0:
            return
        self._capability_overrides.pop(self._build_session_context_id(chat_id), None)

    def handle_model_callback(self, event: NormalizedEvent) -> str | None:
        """
        Process a signed model-select callback and update the model override for the chat.

        Returns the selected model_id on success, None if invalid.
        """
        if self._model_select is None or event.callback_query is None:
            return None
        chat_id = int(event.metadata.get("chat_id", 0))
        model_id = self._model_select.verify_callback(
            event.callback_query.callback_data, expected_chat_id=chat_id
        )
        if model_id is None or model_id not in self._model_allowlist:
            logger.warning(
                "telegram.adapter.model_select.invalid_callback",
                trace_id=event.trace_id,
            )
            return None
        if chat_id != 0:
            self._model_context.set_model_override(
                self._build_session_context_id(chat_id), model_id
            )
            logger.info(
                "telegram.adapter.model_select.activated",
                chat_id=chat_id,
                model_id=model_id,
                trace_id=event.trace_id,
            )
        return model_id

    def get_model_override(self, chat_id: int) -> str | None:
        """Return the model override for the given chat, or None."""
        if chat_id == 0:
            return None
        return self._model_context.get_model_override(self._build_session_context_id(chat_id))

    def start_new_session(self, event: NormalizedEvent) -> str | None:
        """Create and activate a new session id for the event chat context."""
        try:
            chat_id = int(event.metadata.get("chat_id", 0))
        except (TypeError, ValueError):
            return None
        if chat_id <= 0:
            return None
        session_id = f"tg:{chat_id}:{uuid.uuid4().hex[:12]}"
        self._active_session_context.set_active_session(
            self._build_session_context_id(chat_id), session_id
        )
        self.clear_capabilities_override(chat_id)
        logger.info(
            "telegram.adapter.session_new.activated",
            chat_id=chat_id,
            session_id=session_id,
            trace_id=event.trace_id,
        )
        return session_id

    def is_session_reset_available(self) -> bool:
        """Return True when session reset can be executed in this runtime."""
        return self._session_store is not None

    async def reset_session_context(self, event: NormalizedEvent) -> bool:
        """Clear persisted context for the event's active session."""
        if self._session_store is None:
            return False
        cleared = await self._session_store.clear_session(event.session_id)
        logger.info(
            "telegram.adapter.session_reset",
            session_id=event.session_id,
            chat_id=event.metadata.get("chat_id"),
            trace_id=event.trace_id,
            cleared=cleared,
        )
        return cleared

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
        The active session is stored in the configured session-context service.
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
            self._active_session_context.set_active_session(
                self._build_session_context_id(chat_id), session_id
            )
            logger.info(
                "telegram.adapter.session_resume.activated",
                chat_id=chat_id,
                session_id=session_id,
                trace_id=event.trace_id,
            )
        return session_id

    def get_active_session(self, chat_id: int) -> str | None:
        """Return the currently active session override for the given chat, or None."""
        if chat_id <= 0:
            return None
        return self._active_session_context.get_active_session(
            self._build_session_context_id(chat_id)
        )

    def clear_active_session(self, chat_id: int) -> None:
        """Remove any active session override for the given chat."""
        if chat_id <= 0:
            return
        self._active_session_context.clear_active_session(self._build_session_context_id(chat_id))

    def _apply_session_context(self, event: NormalizedEvent | None) -> NormalizedEvent | None:
        if event is None:
            return None
        chat_id = int(event.metadata.get("chat_id", 0))
        active = self.get_active_session(chat_id)
        if active and active != event.session_id:
            return event.model_copy(update={"session_id": active})
        return event

    async def close(self) -> None:
        """Closes underlying network resources."""
        await self._egress.close()

    def _register_memory_confirmation_token(
        self, *, session_id: str, tool_call_id: str, approve: bool
    ) -> str:
        self._cleanup_expired_memory_confirmation_tokens()
        token = uuid.uuid4().hex[:10]
        self._memory_confirmation_tokens[token] = _MemoryConfirmationToken(
            session_id=session_id,
            tool_call_id=tool_call_id,
            approve=approve,
            expires_at=datetime.now(UTC) + timedelta(seconds=_MEMORY_CONFIRMATION_TTL_SECONDS),
        )
        return token

    def _sign_memory_confirmation_callback_token(self, token: str, chat_id: int) -> str:
        secret = (self._config.session_resume_hmac_secret or "").encode()
        if not secret:
            secret = self._config.bot_token.encode()
        return sign_memory_confirmation_callback(token=token, chat_id=chat_id, secret=secret)

    def _verify_memory_confirmation_callback_token(self, event: NormalizedEvent) -> str | None:
        if event.callback_query is None:
            return None
        secret = (self._config.session_resume_hmac_secret or "").encode()
        if not secret:
            secret = self._config.bot_token.encode()
        chat_id = int(event.metadata.get("chat_id", 0))
        return verify_memory_confirmation_callback(
            callback_data=event.callback_query.callback_data,
            expected_chat_id=chat_id,
            secret=secret,
        )

    def _cleanup_expired_memory_confirmation_tokens(self) -> None:
        now = datetime.now(UTC)
        expired = [k for k, v in self._memory_confirmation_tokens.items() if v.expires_at <= now]
        for key in expired:
            self._memory_confirmation_tokens.pop(key, None)

    def _build_session_context_id(self, chat_id: int) -> str:
        return f"telegram:{chat_id}"

    def _register_delegation_question_token(
        self, *, session_id: str, answer_text: str
    ) -> str:
        self._cleanup_expired_delegation_question_tokens()
        token = uuid.uuid4().hex[:10]
        self._delegation_question_tokens[token] = _DelegationQuestionToken(
            session_id=session_id,
            answer_text=answer_text,
            expires_at=datetime.now(UTC) + timedelta(seconds=_MEMORY_CONFIRMATION_TTL_SECONDS),
        )
        return token

    def _sign_delegation_question_callback(self, token: str, chat_id: int) -> str:
        return sign_ask_question_callback(
            token=token, chat_id=chat_id, secret=self._task_callback_secret()
        )

    def _verify_delegation_question_callback_token(self, event: NormalizedEvent) -> str | None:
        if event.callback_query is None:
            return None
        chat_id = int(event.metadata.get("chat_id", 0))
        return verify_ask_question_callback(
            callback_data=event.callback_query.callback_data,
            expected_chat_id=chat_id,
            secret=self._task_callback_secret(),
        )

    def _cleanup_expired_delegation_question_tokens(self) -> None:
        now = datetime.now(UTC)
        expired = [
            k for k, v in self._delegation_question_tokens.items() if v.expires_at <= now
        ]
        for key in expired:
            self._delegation_question_tokens.pop(key, None)

    def _task_callback_secret(self) -> bytes:
        secret = (self._config.session_resume_hmac_secret or "").encode()
        if not secret:
            secret = self._config.bot_token.encode()
        return secret
