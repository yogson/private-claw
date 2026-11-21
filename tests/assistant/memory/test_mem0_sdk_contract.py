"""Contract tests: our Mem0 adapters against the real mem0ai MemoryClient.

The SDK runs for real, but HTTP goes to an in-process httpx.MockTransport, so no
network calls are made. This pins the request shapes the mem0ai 2.x client sends
to the Mem0 Platform v3 endpoints.
"""

import json
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from mem0 import MemoryClient

from assistant.core.config.schemas import MemoryConfig
from assistant.memory.mem0.retrieval import Mem0RetrievalService
from assistant.memory.mem0.write import Mem0MemoryWriteService
from assistant.memory.retrieval.models import RetrievalQuery
from assistant.memory.store.models import MemoryType
from assistant.memory.write.models import (
    MemoryUpdateAction,
    MemoryUpdateIntent,
    MemoryUpdateIntentCandidate,
    WriteStatus,
)


class FakeMem0Api:
    """Minimal in-process stand-in for the Mem0 Platform HTTP API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/v1/ping/":
            return httpx.Response(
                200, json={"org_id": "org-1", "project_id": "proj-1", "user_email": None}
            )
        if request.method == "POST" and path == "/v3/memories/search/":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "mem-1",
                            "memory": "User prefers dark mode.",
                            "score": 0.82,
                            "metadata": {"memory_type": "preferences"},
                            "categories": [],
                            "created_at": "2026-01-15T10:30:00Z",
                            "updated_at": "2026-01-15T10:30:00Z",
                        }
                    ]
                },
            )
        if request.method == "POST" and path == "/v3/memories/add/":
            return httpx.Response(200, json={"event_id": "evt-1", "status": "PENDING"})
        if request.method == "DELETE" and path == "/v1/memories/mem-1/":
            return httpx.Response(200, json={"message": "Memory deleted successfully!"})
        return httpx.Response(404, json={"detail": f"unexpected {request.method} {path}"})

    def last(self, method: str, path: str) -> httpx.Request:
        matches = [r for r in self.requests if r.method == method and r.url.path == path]
        assert matches, f"no {method} {path} request"
        return matches[-1]


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMem0Api]:
    """Route the real MemoryClient to FakeMem0Api and silence SDK telemetry."""
    api = FakeMem0Api()
    http_client = httpx.Client(transport=httpx.MockTransport(api))
    real_client = partial(MemoryClient, client=http_client)
    monkeypatch.setattr("assistant.memory.mem0.retrieval.MemoryClient", real_client)
    monkeypatch.setattr("assistant.memory.mem0.write.MemoryClient", real_client)
    monkeypatch.setattr("mem0.client.main.capture_client_event", lambda *a, **k: None)
    monkeypatch.setattr("mem0.client.main._maybe_alias_anon_to_email", lambda *a, **k: None)
    yield api
    http_client.close()


def _json(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body


def test_search_uses_v3_endpoint_with_filters(fake_api: FakeMem0Api) -> None:
    """Search sends user_id inside filters to /v3/memories/search/ and parses results."""
    config = MemoryConfig(api_key="test-key", org_id="org-x", project_id="proj-x")
    svc = Mem0RetrievalService(config)

    result = svc.retrieve(RetrievalQuery(user_id="alice", user_query_text="  my prefs?  "))

    payload = _json(fake_api.last("POST", "/v3/memories/search/"))
    assert payload == {"query": "my prefs?", "filters": {"user_id": "alice"}, "top_k": 10}
    assert result.audit.retrieval_mode == "mem0_search"
    assert len(result.scored_artifacts) == 1
    artifact = result.scored_artifacts[0]
    assert artifact.artifact.body == "User prefers dark mode."
    assert artifact.artifact.frontmatter.type == MemoryType.PREFERENCES
    assert artifact.score == 0.82


def test_add_uses_v3_endpoint_and_handles_event_response(
    fake_api: FakeMem0Api, tmp_path: Path
) -> None:
    """Upsert posts to /v3/memories/add/ with top-level user_id and infer=False."""
    svc = Mem0MemoryWriteService(MemoryConfig(api_key="test-key"), data_root=tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="intent-1",
        action=MemoryUpdateAction.UPSERT,
        memory_type=MemoryType.FACTS,
        reason="user asked to remember",
        candidate=MemoryUpdateIntentCandidate(
            tags=["work"], confidence=0.9, body_markdown="Important fact."
        ),
    )

    audit = svc.apply_intent(intent, user_id="alice")

    payload = _json(fake_api.last("POST", "/v3/memories/add/"))
    assert payload["user_id"] == "alice"
    assert payload["infer"] is False
    assert payload["metadata"] == {
        "memory_type": "facts",
        "intent_id": "intent-1",
        "tags": ["work"],
    }
    # One user message with only the confirmed text: no filler, no reason suffix (#58, D14).
    assert payload["messages"] == [{"role": "user", "content": "Important fact."}]
    for removed in ("async_mode", "output_format", "org_id", "project_id"):
        assert removed not in payload
    assert audit.status == WriteStatus.WRITTEN
    assert audit.memory_id == "facts-intent-1"


def test_delete_uses_v1_memory_endpoint(fake_api: FakeMem0Api, tmp_path: Path) -> None:
    """Delete calls DELETE /v1/memories/{id}/."""
    svc = Mem0MemoryWriteService(MemoryConfig(api_key="test-key"), data_root=tmp_path)
    intent = MemoryUpdateIntent(
        intent_id="intent-2",
        action=MemoryUpdateAction.DELETE,
        memory_type=MemoryType.FACTS,
        memory_id="mem-1",
    )

    audit = svc.apply_intent(intent, user_id="alice")

    request = fake_api.last("DELETE", "/v1/memories/mem-1/")
    assert "org_id" not in request.url.params
    assert audit.status == WriteStatus.DELETED
