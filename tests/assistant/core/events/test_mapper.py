"""
Unit tests for NormalizedEventMapper.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from assistant.channels.telegram.models import (
    AttachmentMeta as ChannelAttachMeta,
)
from assistant.channels.telegram.models import (
    CallbackQueryMeta as ChannelCallbackMeta,
)
from assistant.channels.telegram.models import (
    NormalizedEvent,
)
from assistant.channels.telegram.models import (
    VoiceMeta as ChannelVoiceMeta,
)
from assistant.core.events.mapper import NormalizedEventMapper
from assistant.core.events.models import (
    AttachmentMeta,
    CallbackQueryMeta,
    EventSource,
    EventType,
    OrchestratorEvent,
    SchedulerMeta,
    SchedulerTriggerKind,
    VoiceMeta,
)


def _base_event(**kwargs: object) -> NormalizedEvent:
    defaults = dict(
        event_id="evt-001",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="sess-001",
        user_id="123456",
        created_at=datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC),
        trace_id="trace-001",
    )
    defaults.update(kwargs)
    return NormalizedEvent(**defaults)


@pytest.fixture
def mapper() -> NormalizedEventMapper:
    return NormalizedEventMapper()


class TestNormalizedEventMapper:
    def test_maps_required_fields(self, mapper: NormalizedEventMapper) -> None:
        event = _base_event()
        result = mapper.map(event)

        assert isinstance(result, OrchestratorEvent)
        assert result.event_id == "evt-001"
        assert result.event_type == EventType.USER_TEXT_MESSAGE
        assert result.source == EventSource.TELEGRAM
        assert result.session_id == "sess-001"
        assert result.user_id == "123456"
        assert result.trace_id == "trace-001"
        assert result.created_at == datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)

    def test_maps_text_message(self, mapper: NormalizedEventMapper) -> None:
        event = _base_event(text="Hello world", idempotency_key="idem-001")
        result = mapper.map(event)

        assert result.text == "Hello world"
        assert result.idempotency_key == "idem-001"
        assert result.scheduler is None

    def test_maps_voice_message(self, mapper: NormalizedEventMapper) -> None:
        event = _base_event(
            event_type=EventType.USER_VOICE_MESSAGE,
            voice=ChannelVoiceMeta(
                file_id="file-voice-001",
                duration_seconds=15,
                transcript_text="voice transcript",
                transcript_confidence=0.95,
            ),
        )
        result = mapper.map(event)

        assert result.voice is not None
        assert isinstance(result.voice, VoiceMeta)
        assert result.voice.file_id == "file-voice-001"
        assert result.voice.duration_seconds == 15
        assert result.voice.transcript_text == "voice transcript"
        assert result.voice.transcript_confidence == 0.95

    def test_maps_voice_without_transcript(self, mapper: NormalizedEventMapper) -> None:
        event = _base_event(
            event_type=EventType.USER_VOICE_MESSAGE,
            voice=ChannelVoiceMeta(file_id="f1", duration_seconds=5),
        )
        result = mapper.map(event)

        assert result.voice is not None
        assert result.voice.transcript_text is None
        assert result.voice.transcript_confidence is None

    def test_maps_attachment_message(self, mapper: NormalizedEventMapper) -> None:
        event = _base_event(
            event_type=EventType.USER_ATTACHMENT_MESSAGE,
            attachment=ChannelAttachMeta(
                file_id="file-doc-001",
                mime_type="application/pdf",
                file_size_bytes=1024,
                caption="my doc",
            ),
        )
        result = mapper.map(event)

        assert result.attachment is not None
        assert isinstance(result.attachment, AttachmentMeta)
        assert result.attachment.file_id == "file-doc-001"
        assert result.attachment.mime_type == "application/pdf"
        assert result.attachment.file_size_bytes == 1024
        assert result.attachment.caption == "my doc"

    def test_maps_callback_query(self, mapper: NormalizedEventMapper) -> None:
        event = _base_event(
            event_type=EventType.USER_CALLBACK_QUERY,
            callback_query=ChannelCallbackMeta(
                callback_id="cb-001",
                callback_data='{"action":"resume_session","target_session_id":"sess-old"}',
                origin_message_id=42,
                ui_version="2",
            ),
        )
        result = mapper.map(event)

        assert result.callback_query is not None
        assert isinstance(result.callback_query, CallbackQueryMeta)
        assert result.callback_query.callback_id == "cb-001"
        assert result.callback_query.origin_message_id == 42
        assert result.callback_query.ui_version == "2"

    def test_maps_attachments_list(self, mapper: NormalizedEventMapper) -> None:
        attachments = [
            ChannelAttachMeta(file_id="f1", mime_type="image/png", file_size_bytes=512),
            ChannelAttachMeta(file_id="f2", mime_type="image/jpeg", file_size_bytes=256),
        ]
        event = _base_event(
            event_type=EventType.USER_ATTACHMENT_MESSAGE,
            attachments=attachments,
        )
        result = mapper.map(event)

        assert len(result.attachments) == 2
        assert result.attachments[0].file_id == "f1"
        assert result.attachments[1].file_id == "f2"

    def test_maps_metadata(self, mapper: NormalizedEventMapper) -> None:
        event = _base_event(metadata={"chat_id": 99, "lang": "en"})
        result = mapper.map(event)

        assert result.metadata == {"chat_id": 99, "lang": "en"}

    def test_no_scheduler_field_for_channel_events(self, mapper: NormalizedEventMapper) -> None:
        result = mapper.map(_base_event())
        assert result.scheduler is None

    def test_none_optional_fields_pass_through(self, mapper: NormalizedEventMapper) -> None:
        event = _base_event()
        result = mapper.map(event)

        assert result.text is None
        assert result.voice is None
        assert result.attachment is None
        assert result.callback_query is None
        assert result.attachments == []
        assert result.idempotency_key is None


class TestOrchestratorEventModels:
    def test_event_type_values(self) -> None:
        assert EventType.USER_TEXT_MESSAGE == "user_text_message"
        assert EventType.SCHEDULER_TRIGGER == "scheduler_trigger"
        assert EventType.USER_CALLBACK_QUERY == "user_callback_query"

    def test_event_source_values(self) -> None:
        assert EventSource.TELEGRAM == "telegram"
        assert EventSource.SCHEDULER == "scheduler"
        assert EventSource.API == "api"
        assert EventSource.SYSTEM == "system"

    def test_orchestrator_event_scheduler_fields(self) -> None:
        scheduler = SchedulerMeta(
            job_id="job-001",
            trigger_kind=SchedulerTriggerKind.REMINDER,
            scheduled_for=datetime(2026, 3, 11, 9, 0, 0, tzinfo=UTC),
            attempt_number=1,
        )
        event = OrchestratorEvent(
            event_id="evt-sch-001",
            event_type=EventType.SCHEDULER_TRIGGER,
            source=EventSource.SCHEDULER,
            session_id="sess-001",
            user_id="system",
            created_at=datetime(2026, 3, 11, 9, 0, 1, tzinfo=UTC),
            trace_id="trace-sch-001",
            scheduler=scheduler,
        )

        assert event.scheduler is not None
        assert event.scheduler.job_id == "job-001"
        assert event.scheduler.trigger_kind == SchedulerTriggerKind.REMINDER
        assert event.scheduler.attempt_number == 1

    def test_orchestrator_event_requires_mandatory_fields(self) -> None:
        with pytest.raises(ValidationError):
            OrchestratorEvent(event_id="x")  # type: ignore[call-arg]

    def test_channel_models_backward_compat(self) -> None:
        """EventType and sub-models imported from channels.telegram.models still work."""
        assert ChannelVoiceMeta is VoiceMeta
        assert ChannelAttachMeta is AttachmentMeta
        assert ChannelCallbackMeta is CallbackQueryMeta


class TestNormalizedEventSourceValidation:
    """Boundary enforcement: invalid source values must be rejected at NormalizedEvent."""

    def _valid_base(self) -> dict:  # type: ignore[return]
        return dict(
            event_id="evt-x",
            event_type=EventType.USER_TEXT_MESSAGE,
            source=EventSource.TELEGRAM,
            session_id="sess-x",
            user_id="1",
            created_at=datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC),
            trace_id="trace-x",
        )

    def test_valid_source_telegram(self) -> None:
        e = NormalizedEvent(**{**self._valid_base(), "source": "telegram"})
        assert e.source == EventSource.TELEGRAM

    def test_valid_source_scheduler(self) -> None:
        e = NormalizedEvent(**{**self._valid_base(), "source": "scheduler"})
        assert e.source == EventSource.SCHEDULER

    def test_valid_source_api(self) -> None:
        e = NormalizedEvent(**{**self._valid_base(), "source": "api"})
        assert e.source == EventSource.API

    def test_valid_source_system(self) -> None:
        e = NormalizedEvent(**{**self._valid_base(), "source": "system"})
        assert e.source == EventSource.SYSTEM

    def test_invalid_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NormalizedEvent(**{**self._valid_base(), "source": "unknown_channel"})

    def test_empty_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NormalizedEvent(**{**self._valid_base(), "source": ""})

    def test_invalid_event_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NormalizedEvent(**{**self._valid_base(), "event_type": "not_a_type"})
