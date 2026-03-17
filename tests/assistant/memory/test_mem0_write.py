"""Unit tests for Mem0 memory write adapter."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from assistant.core.config.schemas import MemoryConfig
from assistant.memory.mem0.write import Mem0MemoryWriteService
from assistant.memory.store.models import MemoryType
from assistant.memory.write.models import (
    MemoryUpdateAction,
    MemoryUpdateIntent,
    MemoryUpdateIntentCandidate,
    WriteStatus,
)


@patch("assistant.memory.mem0.write.MemoryClient")
def test_mem0_write_service_requires_api_key(mock_client: MagicMock, tmp_path: Path) -> None:
    """Mem0MemoryWriteService raises when api_key is empty."""
    config = MemoryConfig(api_key="")
    with pytest.raises(ValueError, match="api_key"):
        Mem0MemoryWriteService(config, data_root=tmp_path)


@patch("assistant.memory.mem0.write.MemoryClient")
def test_upsert_maps_intent_to_mem0_add(mock_client_class: MagicMock, tmp_path: Path) -> None:
    """Upsert maps intent to Mem0 add with messages and metadata."""
    mock_client = MagicMock()
    mock_client.add.return_value = [{"id": "mem-123"}]
    mock_client_class.return_value = mock_client

    config = MemoryConfig(api_key="test-key")
    svc = Mem0MemoryWriteService(config, data_root=tmp_path)

    intent = MemoryUpdateIntent(
        intent_id="i1",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        candidate=MemoryUpdateIntentCandidate(
            tags=["work"],
            entities=["project-x"],
            confidence=0.9,
            body_markdown="Important fact.",
        ),
    )
    audit = svc.apply_intent(intent, user_id="alice")

    assert audit.status == WriteStatus.WRITTEN
    assert audit.memory_id == "mem-123"
    mock_client.add.assert_called_once()
    call_kwargs = mock_client.add.call_args[1]
    assert call_kwargs["user_id"] == "alice"
    assert call_kwargs["metadata"]["memory_type"] == "facts"
    assert call_kwargs["metadata"]["intent_id"] == "i1"
    assert call_kwargs["infer"] is False


@patch("assistant.memory.mem0.write.MemoryClient")
def test_upsert_low_confidence_skipped(mock_client_class: MagicMock, tmp_path: Path) -> None:
    """Low confidence upsert is skipped without calling Mem0."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    config = MemoryConfig(api_key="test-key")
    svc = Mem0MemoryWriteService(config, data_root=tmp_path)

    intent = MemoryUpdateIntent(
        intent_id="i2",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        candidate=MemoryUpdateIntentCandidate(
            confidence=0.3,
            body_markdown="Low confidence.",
        ),
    )
    audit = svc.apply_intent(intent, user_id="alice")

    assert audit.status == WriteStatus.SKIPPED_LOW_CONFIDENCE
    mock_client.add.assert_not_called()


@patch("assistant.memory.mem0.write.MemoryClient")
def test_delete_calls_mem0_delete(mock_client_class: MagicMock, tmp_path: Path) -> None:
    """Delete maps to Mem0 delete by memory_id."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    config = MemoryConfig(api_key="test-key")
    svc = Mem0MemoryWriteService(config, data_root=tmp_path)

    intent = MemoryUpdateIntent(
        intent_id="i3",
        action=MemoryUpdateAction.DELETE,
        memory_type=MemoryType.FACTS,
        memory_id="mem-456",
    )
    audit = svc.apply_intent(intent, user_id="alice")

    assert audit.status == WriteStatus.DELETED
    assert audit.memory_id == "mem-456"
    mock_client.delete.assert_called_once_with("mem-456")


@patch("assistant.memory.mem0.write.MemoryClient")
def test_touch_returns_touched_without_api_call(
    mock_client_class: MagicMock, tmp_path: Path
) -> None:
    """Touch returns TOUCHED status (no-op for Mem0)."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    config = MemoryConfig(api_key="test-key")
    svc = Mem0MemoryWriteService(config, data_root=tmp_path)

    intent = MemoryUpdateIntent(
        intent_id="i4",
        action=MemoryUpdateAction.TOUCH,
        memory_type=MemoryType.FACTS,
        memory_id="mem-789",
    )
    audit = svc.apply_intent(intent, user_id="alice")

    assert audit.status == WriteStatus.TOUCHED
    assert audit.memory_id == "mem-789"
    mock_client.update.assert_not_called()


@patch("assistant.memory.mem0.write.MemoryClient")
def test_duplicate_intent_id_returns_idempotent_noop(
    mock_client_class: MagicMock, tmp_path: Path
) -> None:
    """Duplicate intent_id returns IDEMPOTENT_NOOP without calling Mem0."""
    mock_client = MagicMock()
    mock_client.add.return_value = [{"id": "mem-1"}]
    mock_client_class.return_value = mock_client

    config = MemoryConfig(api_key="test-key")
    svc = Mem0MemoryWriteService(config, data_root=tmp_path)

    intent = MemoryUpdateIntent(
        intent_id="dup-1",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        candidate=MemoryUpdateIntentCandidate(
            confidence=0.9,
            body_markdown="First write.",
        ),
    )
    audit1 = svc.apply_intent(intent, user_id="alice")
    assert audit1.status == WriteStatus.WRITTEN
    assert mock_client.add.call_count == 1

    audit2 = svc.apply_intent(intent, user_id="alice")
    assert audit2.status == WriteStatus.IDEMPOTENT_NOOP
    assert mock_client.add.call_count == 1
