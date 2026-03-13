"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Dedicated memory proposal capability models and validation helpers.
"""

from typing import Any

from assistant.memory.write.models import MemoryUpdateIntent


class MemoryProposalToolCall(MemoryUpdateIntent):
    """Input contract for memory_propose_update capability proposals."""

    requires_user_confirmation: bool = True


def normalize_candidate_for_upsert(candidate: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize loosely structured model candidate into memory schema-friendly payload."""
    payload = dict(candidate or {})
    body = payload.get("body_markdown")
    if isinstance(body, str) and body.strip():
        payload["body_markdown"] = body.strip()
        return payload

    reserved = {"tags", "entities", "priority", "confidence", "body_markdown"}
    details: list[tuple[str, Any]] = []
    for key, value in payload.items():
        if key in reserved:
            continue
        if value in (None, "", [], {}):
            continue
        details.append((key, value))
    if details:
        payload["body_markdown"] = "\n".join(f"- {k}: {v}" for k, v in details)
    else:
        payload["body_markdown"] = "[missing details]"

    tags = payload.get("tags")
    if not isinstance(tags, list):
        payload["tags"] = []
    entities = payload.get("entities")
    if not isinstance(entities, list):
        payload["entities"] = []
    name = payload.get("name")
    if isinstance(name, str) and name.strip() and name not in payload["entities"]:
        payload["entities"].append(name.strip())
    if not payload["tags"]:
        payload["tags"] = ["user_profile"]
    return payload


def canonicalize_memory_args(arguments: dict[str, Any]) -> None:
    """In-place canonicalize model output to valid schema. Shared by provider and confirmation."""
    valid_actions = {"upsert", "delete", "touch"}
    valid_types = {"profile", "preferences", "projects", "tasks", "facts", "summaries"}
    valid_sources = {"explicit_user_request", "agent_inferred", "capability_output", "scheduler"}

    if arguments.get("action") not in valid_actions:
        arguments["action"] = "upsert"
    if arguments.get("memory_type") not in valid_types:
        arguments["memory_type"] = "facts"
    if arguments.get("source") not in valid_sources:
        s = str(arguments.get("source", "")).lower()
        arguments["source"] = (
            "explicit_user_request"
            if any(x in s for x in ("user", "explicit", "stated"))
            else "agent_inferred"
        )


def memory_propose_update(arguments: dict[str, Any]) -> MemoryUpdateIntent:
    """Validate and normalize a memory proposal payload to MemoryUpdateIntent."""
    args = dict(arguments)
    canonicalize_memory_args(args)
    if args.get("action") == "upsert":
        candidate = args.get("candidate")
        if isinstance(candidate, dict):
            args["candidate"] = normalize_candidate_for_upsert(candidate)
    tool_call = MemoryProposalToolCall(**args)
    return MemoryUpdateIntent.model_validate(
        tool_call.model_dump(exclude={"requires_user_confirmation"})
    )
