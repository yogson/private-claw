import json as _json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, cast

import structlog
from pydantic import ValidationError
from pydantic_ai import ModelHTTPError, UnexpectedModelBehavior

from assistant.api.utils import build_text_channel_response, build_webapp_button_channel_response
from assistant.channels.telegram import ChannelResponse, NormalizedEvent, TelegramAdapter
from assistant.channels.telegram.models import EventType
from assistant.channels.telegram.polling import CancellationRegistry
from assistant.channels.telegram.verbose_state import VerboseStateService
from assistant.core.events.mapper import NormalizedEventMapper
from assistant.core.orchestrator.confirmation import MemoryConfirmationService
from assistant.core.orchestrator.service import Orchestrator
from assistant.extensions.language_learning.models import (
    CardResult,
    ExerciseResultPayload,
    FillBlanksResultPayload,
)
from assistant.extensions.language_learning.store import VocabularyStore
from assistant.subagents.coordinator import DelegationCoordinator

logger = structlog.get_logger(__name__)


def _build_orchestrator_handler(
    adapter: TelegramAdapter,
    orchestrator: Orchestrator,
    delegation_coordinator: DelegationCoordinator | None = None,
    memory_confirmations: MemoryConfirmationService | None = None,
    usage_service: Any = None,
    cancellation_registry: CancellationRegistry | None = None,
    verbose_state: VerboseStateService | None = None,
    vocabulary_store: VocabularyStore | None = None,
) -> Callable[[NormalizedEvent], Awaitable[ChannelResponse | None]]:
    mapper = NormalizedEventMapper()

    async def _handler(event: NormalizedEvent) -> ChannelResponse | None:
        # If a streaming delegation task is waiting for user input on this session,
        # treat the incoming message as the answer rather than a normal turn.
        if (
            event.callback_query is None
            and event.text
            and delegation_coordinator is not None
            and delegation_coordinator.has_pending_question(event.session_id)
        ):
            submitted = delegation_coordinator.submit_delegation_answer(
                event.session_id, event.text
            )
            if submitted:
                return build_text_channel_response(
                    text="Answer received. The task will continue.",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
            logger.warning(
                "delegation.answer.submit_failed_unexpected",
                session_id=event.session_id,
                trace_id=event.trace_id,
            )
            return build_text_channel_response(
                text="Could not deliver your answer to the task. Please try again.",
                session_id=event.session_id,
                trace_id=event.trace_id,
            )
        # Route inline-keyboard button taps for delegation question options.
        if event.callback_query is not None and adapter.is_delegation_question_callback(event):
            resolution = adapter.consume_delegation_question_callback(event)
            if resolution is None:
                logger.info("delegation.question.token_invalid", trace_id=event.trace_id)
                return build_text_channel_response(
                    text="Invalid or expired delegation answer.",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
            q_session_id, answer_text = resolution
            if delegation_coordinator is None or not delegation_coordinator.has_pending_question(
                q_session_id
            ):
                logger.info(
                    "delegation.question.expired", session_id=q_session_id, trace_id=event.trace_id
                )
                return build_text_channel_response(
                    text="The question has already been answered or timed out.",
                    session_id=q_session_id,
                    trace_id=event.trace_id,
                )
            submitted = delegation_coordinator.submit_delegation_answer(q_session_id, answer_text)
            if submitted:
                logger.info(
                    "delegation.question.answered", session_id=q_session_id, trace_id=event.trace_id
                )
                return build_text_channel_response(
                    text="Answer received. The task will continue.",
                    session_id=q_session_id,
                    trace_id=event.trace_id,
                )
            logger.warning(
                "delegation.question.submit_failed",
                session_id=q_session_id,
                trace_id=event.trace_id,
            )
            return build_text_channel_response(
                text="Could not submit answer. The task may have already completed.",
                session_id=q_session_id,
                trace_id=event.trace_id,
            )
        if adapter.is_stop_request(event):
            cancelled = (
                cancellation_registry.cancel(event.session_id)
                if cancellation_registry is not None
                else False
            )
            return build_text_channel_response(
                text="Stopped." if cancelled else "Nothing is currently running.",
                session_id=event.session_id,
                trace_id=event.trace_id,
            )
        if adapter.is_verbose_request(event):
            chat_id = int(event.metadata.get("chat_id", 0))
            now_on = (
                verbose_state.toggle(chat_id) if verbose_state is not None and chat_id else False
            )
            return build_text_channel_response(
                text=f"Verbose mode {'*on* — tool calls will be shown' if now_on else '*off*'}.",
                parse_mode="Markdown",
                session_id=event.session_id,
                trace_id=event.trace_id,
            )
        if adapter.is_session_new_request(event):
            session_id = await adapter.start_new_session(event)
            if session_id is None:
                return build_text_channel_response(
                    text="Could not start a new session for this chat.",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
            return build_text_channel_response(
                text="Started a new session. Continue your conversation.",
                session_id=session_id,
                trace_id=event.trace_id,
            )

        if adapter.is_session_reset_request(event):
            if not adapter.is_session_reset_available():
                return build_text_channel_response(
                    text="Session reset is not available.",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
            if usage_service is not None:
                await usage_service.archive_session_usage(event.session_id, event.user_id)
            cleared = await adapter.reset_session_context(event)
            return build_text_channel_response(
                text=(
                    "Session context reset. Starting fresh."
                    if cleared
                    else "Session context is already empty."
                ),
                session_id=event.session_id,
                trace_id=event.trace_id,
            )
        if adapter.is_session_resume_request(event):
            chat_id = int(event.metadata.get("chat_id", 0))
            return await adapter.build_session_menu_response(
                chat_id, event.session_id, event.trace_id
            )
        if adapter.is_session_resume_callback(event):
            session_id = adapter.handle_session_resume_callback(event)
            if session_id:
                return build_text_channel_response(
                    text="Switched to session. Continue your conversation.",
                    session_id=session_id,
                    trace_id=event.trace_id,
                )
            return build_text_channel_response(
                text="Invalid or expired session selection.",
                session_id=event.session_id,
                trace_id=event.trace_id,
            )
        if adapter.is_model_request(event):
            chat_id = int(event.metadata.get("chat_id", 0))
            return await adapter.build_model_menu_response(
                chat_id, event.session_id, event.trace_id
            )
        if adapter.is_model_callback_request(event):
            model_id = adapter.handle_model_callback(event)
            if model_id:
                return build_text_channel_response(
                    text=f"Model set to `{model_id}`. Continue your conversation.",
                    parse_mode="Markdown",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
            return build_text_channel_response(
                text="Invalid or expired model selection.",
                session_id=event.session_id,
                trace_id=event.trace_id,
            )
        if adapter.is_capabilities_request(event):
            chat_id = int(event.metadata.get("chat_id", 0))
            return await adapter.build_capabilities_menu_response(
                chat_id, event.session_id, event.trace_id
            )
        if adapter.is_capabilities_callback_request(event):
            chat_id = int(event.metadata.get("chat_id", 0))
            capability_id = adapter.handle_capabilities_callback(event)
            if capability_id is None:
                return build_text_channel_response(
                    text="Invalid or expired capability selection.",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
            return await adapter.build_capabilities_menu_response(
                chat_id, event.session_id, event.trace_id
            )
        if adapter.is_memory_confirmation_callback(event):
            if memory_confirmations is None:
                return build_text_channel_response(
                    text="Memory confirmation is not available.",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
            mem_resolution = adapter.consume_memory_confirmation_callback(event)
            if mem_resolution is None:
                return build_text_channel_response(
                    text="Invalid or expired confirmation action.",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
            mem_session_id, mem_tool_call_id, approve = mem_resolution
            ok, message = await memory_confirmations.resolve_pending(
                session_id=mem_session_id,
                tool_call_id=mem_tool_call_id,
                approve=approve,
                user_id=event.user_id,
            )
            return build_text_channel_response(
                text=message,
                session_id=mem_session_id if ok else event.session_id,
                trace_id=event.trace_id,
            )
        if event.callback_query is not None and adapter.is_task_callback(event):
            parsed = adapter.parse_task_callback(event)
            if parsed is None:
                return build_text_channel_response(
                    text="Invalid or expired task action.",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
            task_id, action = parsed
            task = None
            if delegation_coordinator is not None:
                task = await delegation_coordinator.get_task(task_id)
            if task is None:
                return build_text_channel_response(
                    text=f"Task `{task_id}` not found.",
                    parse_mode="Markdown",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
            if action == "status":
                text = (
                    f"Task `{task.task_id}` status: *{task.status.value}*\nType: `{task.task_type}`"
                )
                if task.error:
                    text += f"\nError: {task.error}"
            else:
                summary = ""
                if isinstance(task.result, dict):
                    summary = str(task.result.get("summary", "")).strip()
                text = summary or f"Task `{task.task_id}` has no summary yet."
            return build_text_channel_response(
                text=text,
                parse_mode="Markdown",
                session_id=event.session_id,
                trace_id=event.trace_id,
            )
        if adapter.is_usage_request(event):
            if usage_service is not None:
                return cast(
                    ChannelResponse,
                    await usage_service.build_usage_response(event),
                )
            return build_text_channel_response(
                text="Usage stats not available.",
                session_id=event.session_id,
                trace_id=event.trace_id,
            )

        if event.event_type == EventType.EXERCISE_RESULTS:
            return await _handle_exercise_results(event, vocabulary_store)

        if event.event_type == EventType.FILL_BLANKS_RESULTS:
            return await _handle_fill_blanks_results(event, vocabulary_store)

        orch_event = mapper.map(event)
        chat_id = int(event.metadata.get("chat_id", 0))
        model_override = adapter.get_model_override(chat_id) if chat_id else None
        if model_override:
            orch_event = orch_event.model_copy(update={"model_id_override": model_override})
        capabilities_override = adapter.get_capabilities_override(event.session_id)
        if capabilities_override is not None:
            orch_event = orch_event.model_copy(
                update={"capabilities_override": capabilities_override}
            )
        notifier = None
        if chat_id and verbose_state is not None and verbose_state.is_enabled(chat_id):
            notifier = _build_tool_call_notifier(
                adapter, chat_id, orch_event.session_id, event.trace_id
            )
        text_notifier = None
        if chat_id:
            text_notifier = _build_streaming_text_notifier(
                adapter, chat_id, orch_event.session_id, event.trace_id
            )
        try:
            orch_result = await orchestrator.execute_turn(
                orch_event,
                tool_call_notifier=notifier,
                streaming_text_notifier=text_notifier,
            )
        except ModelHTTPError as exc:
            if _is_token_limit_error(exc):
                logger.warning(
                    "orchestrator.token_limit_exceeded",
                    session_id=orch_event.session_id,
                    trace_id=event.trace_id,
                    status_code=exc.status_code,
                )
                if usage_service is not None:
                    await usage_service.archive_session_usage(orch_event.session_id, event.user_id)
                if adapter.is_session_reset_available():
                    await adapter.reset_session_context(event)
                    reset_msg = "Session has been reset. Please try your message again."
                else:
                    reset_msg = (
                        "Session reset is not available. Please use /reset or start a new session."
                    )
                return build_text_channel_response(
                    text=f"Conversation history exceeded the model limit. {reset_msg}",
                    session_id=orch_event.session_id,
                    trace_id=event.trace_id,
                )
            logger.warning(
                "orchestrator.model_http_error",
                session_id=orch_event.session_id,
                trace_id=event.trace_id,
                status_code=exc.status_code,
            )
            return build_text_channel_response(
                text=f"The AI model returned an error (HTTP {exc.status_code}). Please try again.",
                session_id=orch_event.session_id,
                trace_id=event.trace_id,
            )
        except UnexpectedModelBehavior as exc:
            logger.warning(
                "orchestrator.unexpected_model_behavior",
                session_id=orch_event.session_id,
                trace_id=event.trace_id,
                error=str(exc),
            )
            return build_text_channel_response(
                text=(
                    "The assistant encountered a tool error and could not complete the request. "
                    "Please try again, or start a fresh session if the problem persists."
                ),
                session_id=orch_event.session_id,
                trace_id=event.trace_id,
            )
        if orch_result is None:
            return None
        session_id = orch_event.session_id
        response_text = orch_result.text
        if orch_result.pending_webapp_buttons:
            webapp_text = (
                response_text.strip()
                or orch_result.pending_webapp_message
                or "Tap the button below to start."
            )
            return build_webapp_button_channel_response(
                text=webapp_text,
                session_id=session_id,
                trace_id=event.trace_id,
                buttons=orch_result.pending_webapp_buttons,
            )
        if orch_result.pending_ask is not None:
            prompt_text = orch_result.pending_ask.question
            if response_text.strip():
                prompt_text = f"{response_text}\n\n{prompt_text}"
            return adapter.build_ask_question_response(
                session_id=session_id,
                trace_id=event.trace_id,
                question=prompt_text,
                options=orch_result.pending_ask.options,
            )
        if memory_confirmations is not None:
            pending = await memory_confirmations.list_pending(session_id)
            if not pending:
                logger.debug(
                    "memory.confirmation.no_pending",
                    session_id=session_id,
                    trace_id=event.trace_id,
                )
            if pending:
                chat_id = int(event.metadata.get("chat_id", 0))
                if not chat_id:
                    logger.warning(
                        "memory.confirmation.skipped_no_chat_id",
                        session_id=session_id,
                        trace_id=event.trace_id,
                    )
                if chat_id:
                    logger.info(
                        "memory.confirmation.showing_ui",
                        session_id=session_id,
                        tool_call_id=pending[0].tool_call_id,
                        trace_id=event.trace_id,
                    )
                    prompt_text = response_text
                    proposal_reason = pending[0].proposal.reason.strip()
                    if proposal_reason:
                        prompt_text = f"{response_text}\n\nPending memory update: {proposal_reason}"
                    return adapter.build_memory_confirmation_response(
                        chat_id=chat_id,
                        session_id=session_id,
                        trace_id=event.trace_id,
                        tool_call_id=pending[0].tool_call_id,
                        prompt_text=prompt_text,
                    )
        if not response_text.strip():
            # Already sent in full via streaming_text_notifier; nothing left to send.
            return None
        return build_text_channel_response(
            text=response_text,
            session_id=orch_event.session_id,
            trace_id=event.trace_id,
        )

    return _handler


def _build_tool_call_notifier(
    adapter: TelegramAdapter,
    chat_id: int,
    session_id: str,
    trace_id: str,
) -> "Callable[[str, str], Awaitable[None]]":
    """Build an async notifier that sends a verbose tool-call message to Telegram."""
    import json as _json

    async def _notifier(tool_name: str, args_json: str) -> None:
        try:
            args = _json.loads(args_json) if args_json and args_json != "{}" else {}
        except Exception:
            args = {}
        if args:
            pretty = _json.dumps(args, ensure_ascii=False, indent=None)
            text = f"⚙️ `{tool_name}`\n```\n{pretty}\n```"
        else:
            text = f"⚙️ `{tool_name}`"
        response = build_text_channel_response(
            text=text,
            parse_mode="Markdown",
            session_id=session_id,
            trace_id=trace_id,
        )
        with suppress(Exception):
            await adapter.send_response(response, chat_id=chat_id)

    return _notifier


def _build_streaming_text_notifier(
    adapter: TelegramAdapter,
    chat_id: int,
    session_id: str,
    trace_id: str,
) -> "Callable[[str], Awaitable[None]]":
    """Build an async notifier that sends intermediate agent text to Telegram immediately."""

    async def _notifier(text: str) -> None:
        if not text.strip():
            return
        response = build_text_channel_response(
            text=text,
            session_id=session_id,
            trace_id=trace_id,
        )
        with suppress(Exception):
            await adapter.send_response(response, chat_id=chat_id)

    return _notifier


async def _handle_exercise_results(
    event: NormalizedEvent,
    vocabulary_store: VocabularyStore | None,
) -> ChannelResponse:
    """Process exercise results directly, without LLM involvement."""
    if vocabulary_store is None:
        return build_text_channel_response(
            text="Ошибка: хранилище словаря недоступно.",
            session_id=event.session_id,
            trace_id=event.trace_id,
        )

    try:
        raw = _json.loads(event.text or "")
        payload = ExerciseResultPayload.model_validate(raw)
    except (ValueError, TypeError):
        return build_text_channel_response(
            text="Ошибка: неверный формат данных (невалидный JSON).",
            session_id=event.session_id,
            trace_id=event.trace_id,
        )
    except ValidationError as exc:
        return build_text_channel_response(
            text=f"Ошибка: неверная схема данных. {exc}",
            session_id=event.session_id,
            trace_id=event.trace_id,
        )

    try:
        updated = await vocabulary_store.process_exercise_results(
            user_id=event.user_id,
            results=payload.results,
        )
    except Exception as exc:
        logger.error(
            "exercise_results.direct.store_error",
            error=str(exc),
            trace_id=event.trace_id,
        )
        return build_text_channel_response(
            text=f"Ошибка при сохранении результатов: {exc}",
            session_id=event.session_id,
            trace_id=event.trace_id,
        )

    updated_count = sum(1 for v in updated.values() if v is not None)
    return build_text_channel_response(
        text=_plural_updated(updated_count),
        session_id=event.session_id,
        trace_id=event.trace_id,
    )


async def _handle_fill_blanks_results(
    event: NormalizedEvent,
    vocabulary_store: VocabularyStore | None,
) -> ChannelResponse:
    """Process fill-in-the-blanks results directly, without LLM involvement."""
    if vocabulary_store is None:
        return build_text_channel_response(
            text="Ошибка: хранилище словаря недоступно.",
            session_id=event.session_id,
            trace_id=event.trace_id,
        )

    try:
        raw = _json.loads(event.text or "")
        payload = FillBlanksResultPayload.model_validate(raw)
    except (ValueError, TypeError):
        return build_text_channel_response(
            text="Ошибка: неверный формат данных (невалидный JSON).",
            session_id=event.session_id,
            trace_id=event.trace_id,
        )
    except ValidationError as exc:
        return build_text_channel_response(
            text=f"Ошибка: неверная схема данных. {exc}",
            session_id=event.session_id,
            trace_id=event.trace_id,
        )

    # Convert fill-blanks results to CardResult objects:
    # correct placement → Good (2), incorrect → Again (0)
    card_results = [
        CardResult(
            word_id=r.word_id,
            rating=2 if r.correct else 0,
            time_ms=r.time_ms,
            direction=payload.direction,
        )
        for r in payload.results
    ]

    try:
        updated = await vocabulary_store.process_exercise_results(
            user_id=event.user_id,
            results=card_results,
        )
    except Exception as exc:
        logger.error(
            "fill_blanks_results.direct.store_error",
            error=str(exc),
            trace_id=event.trace_id,
        )
        return build_text_channel_response(
            text=f"Ошибка при сохранении результатов: {exc}",
            session_id=event.session_id,
            trace_id=event.trace_id,
        )

    updated_count = sum(1 for v in updated.values() if v is not None)
    return build_text_channel_response(
        text=_plural_updated(updated_count),
        session_id=event.session_id,
        trace_id=event.trace_id,
    )


def _plural_updated(n: int) -> str:
    mod10, mod100 = n % 10, n % 100
    if mod10 == 1 and mod100 != 11:
        return f"Обновлено {n} слово"
    if 2 <= mod10 <= 4 and (mod100 < 10 or mod100 >= 20):
        return f"Обновлено {n} слова"
    return f"Обновлено {n} слов"


def _is_token_limit_error(exc: ModelHTTPError) -> bool:
    """Return True if the error indicates the prompt exceeded the model's token limit.

    Detection is tuned for Anthropic-style error bodies (dict with error.message).
    Assumes exc.body is a dict or None; other types are treated as empty.
    """
    if exc.status_code != 400:
        return False
    body = exc.body if isinstance(exc.body, dict) else {}
    raw_error = body.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    msg = str(error.get("message", "")).lower()
    return "prompt is too long" in msg or "token" in msg and "maximum" in msg
