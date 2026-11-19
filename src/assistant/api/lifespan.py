import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import structlog
from fastapi import FastAPI

from assistant.agent.adapter_cache import TurnAdapterCache
from assistant.agent.tools.registry import active_capabilities_for, collect_enabled_mcp_server_ids
from assistant.api.delegation_feedback_handler import _build_delegation_feedback_handler
from assistant.api.deps import set_runtime_config
from assistant.api.orchestrator_handler import _build_orchestrator_handler
from assistant.api.question_relay_handler import _build_question_relay_handler
from assistant.api.utils import build_text_channel_response
from assistant.channels.telegram import ChannelResponse, NormalizedEvent, TelegramAdapter
from assistant.channels.telegram.ingestion.factory import build_transcription_service
from assistant.channels.telegram.ingestion.file_downloader import TelegramFileDownloader
from assistant.channels.telegram.polling import CancellationRegistry, run_polling
from assistant.channels.telegram.usage import UsageStatsService
from assistant.channels.telegram.verbose_state import VerboseStateService
from assistant.core.bootstrap import bootstrap_with_catalog
from assistant.core.capabilities import ClaudeCodeSettingsSync, SubagentMcpServersSync
from assistant.core.capabilities.edit_service import CapabilityEditService
from assistant.core.capabilities.resolver import resolve_policy
from assistant.core.confirmation import (
    CapabilityEditHandler,
    ConfirmationHandlerRegistry,
    ConfirmationService,
    MemoryUpdateHandler,
    SessionConfirmationRecorder,
)
from assistant.core.orchestrator.service import Orchestrator
from assistant.core.session import SessionContextFactory
from assistant.core.session_context import (
    ActiveSessionContextInterface,
    ActiveSessionContextService,
    SessionCapabilityContextService,
    SessionModelContextService,
)
from assistant.extensions.language_learning import VocabularyStore
from assistant.extensions.mcp.toolsets import mcp_toolsets
from assistant.memory.mem0 import Mem0MemoryWriteService, Mem0RetrievalService
from assistant.observability.logfire import configure_logfire
from assistant.observability.logging import configure_logging
from assistant.store import StoreFacade
from assistant.store.idempotency import IngressIdempotencyService
from assistant.subagents.backends import ClaudeCodeBackendAdapter, ClaudeCodeStreamingBackendAdapter
from assistant.subagents.coordinator import DelegationCoordinator

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    runtime_config, capability_catalog = bootstrap_with_catalog()
    set_runtime_config(runtime_config)
    log_path = configure_logging(runtime_config.app)
    logger.info("logging.started", log_file=str(log_path))
    logfire_configured = configure_logfire(runtime_config.app)
    logger.info("logfire.status", configured=logfire_configured)
    app.state.runtime_config = runtime_config
    app.state.telegram_adapter = None
    app.state.attachment_downloader = None
    app.state.store = None
    app.state.delegation_coordinator = None

    app.state.capability_catalog = capability_catalog
    default_policy = runtime_config.capabilities
    # ~/.claude/settings.json gets revision 1 now and then follows revisions that change the
    # active capabilities' claude_code_settings. A skipped write (unusable or unwritable
    # file) never aborts startup and is retried with the next revision.
    settings_sync = ClaudeCodeSettingsSync(
        resolve_active=lambda snapshot: resolve_policy(default_policy, snapshot),
    )
    settings_sync.apply(capability_catalog.current())
    capability_catalog.subscribe(settings_sync)

    store: StoreFacade | None = None
    polling_task: asyncio.Task[None] | None = None
    polling_stop = asyncio.Event()

    if runtime_config.telegram.enabled:
        data_root = Path(runtime_config.app.data_root)

        store = StoreFacade(
            data_root=data_root,
            lock_ttl_seconds=runtime_config.store.lock_ttl_seconds,
            idempotency_ttl_seconds=runtime_config.store.idempotency_retention_seconds,
        )
        await store.initialize()
        app.state.store = store

        idempotency = IngressIdempotencyService(
            store.idempotency,
            default_ttl_seconds=runtime_config.store.idempotency_retention_seconds,
        )
        if not runtime_config.memory.api_key.strip():
            raise SystemExit(
                "Mem0 api_key is required. Set ASSISTANT_MEMORY_API_KEY or "
                "configure api_key in config/memory.yaml"
            ) from None
        memory_writer = Mem0MemoryWriteService(runtime_config.memory, data_root=data_root)
        memory_retrieval = Mem0RetrievalService(runtime_config.memory)
        # Agent-proposed manifest edits (kind capability_edit): applied only on approval,
        # then picked up live through capability_catalog.reload().
        capability_edit_service = CapabilityEditService(
            capability_catalog, retention=runtime_config.app.capability_history_retention
        )
        confirmations = ConfirmationService(
            handlers=ConfirmationHandlerRegistry(
                [MemoryUpdateHandler(memory_writer), CapabilityEditHandler(capability_edit_service)]
            ),
            recorder=SessionConfirmationRecorder(store.sessions),
            ttl_seconds=runtime_config.app.confirmation_ttl_seconds,
        )
        attachment_downloader = TelegramFileDownloader(
            bot_token=runtime_config.telegram.bot_token,
            max_size_bytes=runtime_config.telegram.max_attachment_size_bytes,
        )
        app.state.attachment_downloader = attachment_downloader

        model_id = runtime_config.model.default_model_id
        if not model_id.startswith("anthropic:"):
            model_id = f"anthropic:{model_id}"
        # Build the adapter cache (Option C).  The default capability set is
        # pre-populated at construction time; additional capability sets are
        # built lazily on the first turn that uses them and then reused.
        adapter_cache = TurnAdapterCache(
            model_id=model_id,
            max_tokens=runtime_config.model.max_tokens_default,
            base_config=runtime_config,
            snapshot=capability_catalog.current(),
        )

        transcription_service = build_transcription_service(runtime_config.telegram)
        active_session_context = ActiveSessionContextService(
            data_root / "runtime" / "active_session_context.json"
        )
        model_context = SessionModelContextService(
            data_root / "runtime" / "active_model_context.json"
        )
        capability_context = SessionCapabilityContextService(
            data_root / "runtime" / "active_capability_context.json"
        )
        session_factory = SessionContextFactory(
            store=store,
            active_context=active_session_context,
            model_context=model_context,
            capability_context=capability_context,
            metadata_store=store.session_metadata,
        )

        adapter = TelegramAdapter(
            runtime_config.telegram,
            transcription_service=transcription_service,
            session_store=store.sessions,
            active_session_context=active_session_context,
            model_context=model_context,
            model_allowlist=runtime_config.model.model_allowlist,
            default_model_id=runtime_config.model.default_model_id,
            capability_catalog=capability_catalog,
            default_capabilities=runtime_config.capabilities.enabled_capabilities,
            capability_context=capability_context,
            session_factory=session_factory,
        )
        app.state.telegram_adapter = adapter

        # MCP servers declared by the active capabilities are handed to the
        # sub-agents explicitly: Claude Code does not read "mcpServers" from
        # settings.json, and the SDK backend loads no filesystem settings at all.
        # They are recomputed on every new capability snapshot revision.
        claude_code_backend = ClaudeCodeBackendAdapter()
        claude_code_streaming_backend = ClaudeCodeStreamingBackendAdapter()
        subagent_mcp_sync = SubagentMcpServersSync(
            resolve_active=lambda snapshot: resolve_policy(default_policy, snapshot),
            targets=[claude_code_backend, claude_code_streaming_backend],
        )
        subagent_mcp_sync(capability_catalog.current())
        capability_catalog.subscribe(subagent_mcp_sync)
        delegation_coordinator = DelegationCoordinator(
            store=store,
            config=runtime_config,
            backends=[claude_code_backend, claude_code_streaming_backend],
        )
        await delegation_coordinator.start()
        app.state.delegation_coordinator = delegation_coordinator
        vocabulary_store = VocabularyStore(vocabulary_dir=data_root / "vocabulary")
        orchestrator = Orchestrator(
            store=store,
            config=runtime_config,
            idempotency=idempotency,
            attachment_downloader=attachment_downloader,
            memory_retrieval=memory_retrieval,
            delegation_coordinator=delegation_coordinator,
            adapter_cache=adapter_cache,
            session_factory=session_factory,
            vocabulary_store=vocabulary_store,
            capability_catalog=capability_catalog,
            confirmation_service=confirmations,
            capability_edit_service=capability_edit_service,
        )
        delegation_coordinator.set_completion_callback(
            _build_delegation_feedback_handler(orchestrator, adapter, confirmations)
        )
        delegation_coordinator.set_question_relay_callback(_build_question_relay_handler(adapter))

        usage_service = UsageStatsService(
            session_store=store.sessions,
            archive_dir=data_root / "runtime" / "usage_archive",
            default_model_id=runtime_config.model.default_model_id,
        )
        cancellation_registry = CancellationRegistry()
        verbose_state = VerboseStateService(
            storage_path=data_root / "runtime" / "verbose_state.json"
        )
        handler = _build_orchestrator_handler(
            adapter,
            orchestrator,
            delegation_coordinator,
            confirmations,
            usage_service=usage_service,
            cancellation_registry=cancellation_registry,
            verbose_state=verbose_state,
            vocabulary_store=vocabulary_store,
        )

        async def _dispatch(event: NormalizedEvent) -> ChannelResponse | None:
            return await handler(event)

        await _notify_system_started(adapter, active_session_context)

        polling_task = asyncio.create_task(
            run_polling(
                adapter,
                runtime_config.telegram,
                _dispatch,
                stop_event=polling_stop,
                cancellation_registry=cancellation_registry,
            )
        )

    # Connect the MCP servers the active capabilities reference once, for the whole app
    # lifetime (stdio servers are spawned here, not per turn). Runs in the background so a
    # slow or dead server never delays startup; a failed server is retried lazily with
    # backoff by the first turn that needs it. Servers only reachable through per-session
    # capability overrides connect on first use. Started right before the try/finally that
    # cancels it, so no failed init step above can leave a spawned stdio server orphaned.
    mcp_start_task = asyncio.create_task(
        mcp_toolsets.start(
            runtime_config.mcp_servers,
            collect_enabled_mcp_server_ids(
                active_capabilities_for(runtime_config, capability_catalog.current())
            ),
        )
    )

    try:
        yield
    finally:
        mcp_start_task.cancel()
        with suppress(asyncio.CancelledError):
            await mcp_start_task
        await mcp_toolsets.close()
        if polling_task is not None:
            polling_stop.set()
            try:
                await asyncio.wait_for(polling_task, timeout=10.0)
            except TimeoutError:
                polling_task.cancel()
                with suppress(asyncio.CancelledError):
                    await polling_task
        adapter = app.state.telegram_adapter
        if adapter is not None:
            await adapter.close()
            logger.info("telegram.polling.stopped")
        downloader = app.state.attachment_downloader
        if downloader is not None:
            await downloader.close()
        if store is not None:
            coordinator = app.state.delegation_coordinator
            if coordinator is not None:
                await coordinator.stop()
            await store.shutdown()


async def _notify_system_started(
    adapter: TelegramAdapter,
    active_session_context: ActiveSessionContextInterface,
) -> None:
    """Send 'System started.' to every chat that has an active session.

    Chat IDs are derived from the persisted session context, which maps
    'telegram:<chat_id>' context keys to session IDs.  Unknown or unparseable
    keys are silently skipped.

    Note: ``list_context_ids()`` returns *all* persisted sessions, including
    stale ones from previous runs.  No TTL filter is applied, so users whose
    sessions have long since expired will still receive the notification.
    """
    context_ids = active_session_context.list_context_ids()
    for context_id in context_ids:
        # Context IDs for Telegram take the form 'telegram:<chat_id>'
        if not context_id.startswith("telegram:"):
            continue
        raw_chat_id = context_id.removeprefix("telegram:")
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            continue

        response = build_text_channel_response(
            text="System started.",
            session_id="__system__",
            trace_id=str(uuid.uuid4()),
        )
        try:
            await adapter.send_response(response, chat_id=chat_id)
            logger.info("system.started.notified", chat_id=chat_id)
        except Exception:
            logger.warning("system.started.notify_failed", chat_id=chat_id)
