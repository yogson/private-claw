"""Unit tests for Mem0 memory retrieval adapter."""

from unittest.mock import MagicMock, patch

import pytest

from assistant.core.config.schemas import MemoryConfig
from assistant.memory.mem0.retrieval import Mem0RetrievalService
from assistant.memory.retrieval.models import RetrievalQuery


@patch("assistant.memory.mem0.retrieval.MemoryClient")
def test_mem0_retrieval_service_requires_api_key(mock_client: MagicMock) -> None:
    """Mem0RetrievalService raises when api_key is empty."""
    config = MemoryConfig(api_key="")
    with pytest.raises(ValueError, match="api_key"):
        Mem0RetrievalService(config)


@patch("assistant.memory.mem0.retrieval.MemoryClient")
def test_retrieve_maps_query_to_mem0_search(mock_client_class: MagicMock) -> None:
    """Retrieve maps RetrievalQuery to Mem0 search with user_id filter."""
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {
                "id": "mem-1",
                "memory": "User prefers dark mode.",
                "score": 0.85,
                "metadata": {"memory_type": "preferences"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        ]
    }
    mock_client_class.return_value = mock_client

    config = MemoryConfig(api_key="test-key")
    svc = Mem0RetrievalService(config)

    query = RetrievalQuery(
        user_id="alice",
        user_query_text="What are my preferences?",
    )
    result = svc.retrieve(query)

    assert len(result.scored_artifacts) == 1
    assert result.scored_artifacts[0].artifact.body == "User prefers dark mode."
    assert result.scored_artifacts[0].score == 0.85
    mock_client.search.assert_called_once()
    call_args = mock_client.search.call_args
    assert call_args[0][0] == "What are my preferences?"
    assert call_args[1]["filters"]["user_id"] == "alice"


@patch("assistant.memory.mem0.retrieval.MemoryClient")
def test_retrieve_handles_empty_results(mock_client_class: MagicMock) -> None:
    """Retrieve returns empty result when Mem0 returns no memories."""
    mock_client = MagicMock()
    mock_client.search.return_value = {"results": []}
    mock_client_class.return_value = mock_client

    config = MemoryConfig(api_key="test-key")
    svc = Mem0RetrievalService(config)

    query = RetrievalQuery(user_id="alice", user_query_text="anything")
    result = svc.retrieve(query)

    assert len(result.scored_artifacts) == 0
    assert result.audit.retrieval_mode == "mem0_search"


@patch("assistant.memory.mem0.retrieval.MemoryClient")
def test_ensure_indexes_is_noop(mock_client_class: MagicMock) -> None:
    """ensure_indexes is a no-op for Mem0."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    config = MemoryConfig(api_key="test-key")
    svc = Mem0RetrievalService(config)
    svc.ensure_indexes()
    mock_client.assert_not_called()
