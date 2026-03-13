"""Tests for CMP_TOOL_RUNTIME_REGISTRY memory proposal capability helpers."""

from assistant.extensions.first_party.memory import (
    canonicalize_memory_args,
    memory_propose_update,
)
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


def test_canonicalize_memory_args_maps_create_pet_to_upsert_facts() -> None:
    """Invalid action/memory_type/source are auto-corrected to valid values."""
    args = {
        "intent_id": "x",
        "action": "create",
        "memory_type": "pet",
        "source": "user explicitly stated their cat's name is Kis",
        "candidate": {"body_markdown": "Cat named Kis"},
    }
    canonicalize_memory_args(args)
    assert args["action"] == "upsert"
    assert args["memory_type"] == "facts"
    assert args["source"] == "explicit_user_request"


def test_memory_propose_update_normalizes_model_candidate_to_body_markdown() -> None:
    """Model may send fact/type instead of body_markdown; normalization produces body."""
    intent = memory_propose_update(
        {
            "intent_id": "store_cat_name",
            "action": "upsert",
            "memory_type": "facts",
            "reason": "User shared their cat's name: Кис (Kiss)",
            "source": "explicit_user_request",
            "requires_user_confirmation": False,
            "candidate": {
                "fact": "У пользователя есть кошка по имени Кис (Kiss)",
                "type": "pet_info",
            },
        }
    )
    assert intent.candidate is not None
    assert "Кис" in intent.candidate.body_markdown
    assert "pet_info" in intent.candidate.body_markdown
