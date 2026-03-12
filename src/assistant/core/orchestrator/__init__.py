"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Compatibility facade for orchestrator service and payload helper symbols.
"""

from assistant.core.orchestrator.payloads import (
    _PLACEHOLDER_EMPTY,
    build_user_content_blocks,
    extract_raw_text_for_multimodal,
    extract_user_text,
    format_attachment_context,
    gather_attachments,
    records_to_messages,
)

_extract_user_text = extract_user_text
_extract_raw_text_for_multimodal = extract_raw_text_for_multimodal
_gather_attachments = gather_attachments
_format_attachment_context = format_attachment_context
_build_user_content_blocks = build_user_content_blocks
_records_to_messages = records_to_messages


def __getattr__(name: str) -> object:
    if name == "Orchestrator":
        from assistant.core.orchestrator.service import Orchestrator

        return Orchestrator
    raise AttributeError(name)


__all__ = [
    "Orchestrator",
    "_PLACEHOLDER_EMPTY",
    "_extract_user_text",
    "_extract_raw_text_for_multimodal",
    "_gather_attachments",
    "_format_attachment_context",
    "_build_user_content_blocks",
    "_records_to_messages",
]
