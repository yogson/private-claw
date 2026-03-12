"""Tests for CMP_TOOL_RUNTIME_REGISTRY memory proposal capability helpers."""

from assistant.extensions.first_party.memory import memory_propose_update
from assistant.memory.write.models import MemoryUpdateAction


def test_memory_propose_update_validates_payload() -> None:
    intent = memory_propose_update(
        {
            "intent_id": "intent-1",
            "action": "upsert",
            "memory_type": "preferences",
            "candidate": {
                "tags": ["style"],
                "entities": ["assistant"],
                "priority": 6,
                "confidence": 0.9,
                "body_markdown": "User prefers concise responses.",
            },
            "reason": "Capture explicit preference",
            "source": "explicit_user_request",
            "requires_user_confirmation": True,
        }
    )
    assert intent.intent_id == "intent-1"
    assert intent.action == MemoryUpdateAction.UPSERT
    assert intent.memory_type.value == "preferences"


def test_memory_propose_update_defaults_confirmation_flag() -> None:
    intent = memory_propose_update(
        {
            "intent_id": "intent-3",
            "action": "upsert",
            "memory_type": "preferences",
            "reason": "missing confirmation",
            "source": "agent_inferred",
        }
    )
    assert intent.intent_id == "intent-3"
