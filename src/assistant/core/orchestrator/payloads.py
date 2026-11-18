"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Payload helpers for user input extraction and multimodal attachment packaging.
"""

import base64
import json
from pathlib import Path
from typing import Any

import structlog

from assistant.agent.constants import MEMORY_TOOL_NAME
from assistant.agent.interfaces import LLMMessage, MessageRole
from assistant.core.confirmation.history import (
    is_confirmation_record,
    neutralize_confirmation_marker,
)
from assistant.core.events.models import AttachmentMeta, OrchestratorEvent
from assistant.core.orchestrator.attachments import AttachmentDownloaderInterface
from assistant.memory.retrieval.models import RetrievalResult
from assistant.store.models import SessionRecord, SessionRecordType

logger = structlog.get_logger(__name__)

_PLACEHOLDER_EMPTY = "[Empty or unsupported input]"

_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_PDF_MIME_TYPE = "application/pdf"
_TEXT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/javascript",
        "application/x-javascript",
    }
)
_TEXT_ATTACHMENT_CHAR_BUDGET = 12_000
_TEXT_ATTACHMENT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".csv",
        ".ini",
        ".toml",
        ".log",
    }
)
_FALLBACK_MIME_TYPE = "application/octet-stream"
_DEBUG_LOG = Path(__file__).resolve().parents[4] / ".cursor" / "debug-2322a9.log"


def _debug_log_ask_question_args(tool_name: str, args: dict[str, Any]) -> None:
    if tool_name != "ask_question":
        return
    opts = args.get("options")
    if opts is not None and isinstance(opts, str):
        try:
            with open(_DEBUG_LOG, "a") as f:
                f.write(
                    json.dumps(
                        {
                            "sessionId": "2322a9",
                            "hypothesisId": "H2",
                            "location": "payloads.py:records_to_messages",
                            "message": "ask_question options from history is string",
                            "data": {"source": "history_replay"},
                            "timestamp": __import__("time").time_ns() // 1_000_000,
                        }
                    )
                    + "\n"
                )
        except OSError:
            pass


def _extract_text_from_event(event: OrchestratorEvent, fallback: str | None) -> str | None:
    if event.text and event.text.strip():
        return event.text.strip()
    if event.voice and event.voice.transcript_text:
        return event.voice.transcript_text.strip()
    if event.attachment and event.attachment.caption:
        return event.attachment.caption.strip()
    if event.attachments:
        for att in event.attachments:
            if att.caption:
                return att.caption.strip()
    if event.callback_query:
        return f"[Callback: {event.callback_query.callback_data[:100]}]"
    return fallback


def extract_user_text(event: OrchestratorEvent) -> str:
    return _extract_text_from_event(event, _PLACEHOLDER_EMPTY) or _PLACEHOLDER_EMPTY


def extract_raw_text_for_multimodal(event: OrchestratorEvent) -> str | None:
    return _extract_text_from_event(event, None)


def gather_attachments(event: OrchestratorEvent) -> list[AttachmentMeta]:
    # When a media-group event is built, the first attachment is placed in both
    # ``attachment`` (backwards-compat singular field) and ``attachments`` (full
    # list).  Iterating both fields would therefore duplicate the first entry.
    # Prefer ``attachments`` when it is non-empty; fall back to the singular
    # field only for legacy single-attachment events.
    if event.attachments:
        return [att for att in event.attachments if att.file_id]
    if event.attachment and event.attachment.file_id:
        return [event.attachment]
    return []


def format_attachment_context(attachments: list[AttachmentMeta]) -> str:
    if not attachments:
        return ""
    parts = []
    for att in attachments:
        size_str = f"{att.file_size_bytes / 1024:.1f} KB" if att.file_size_bytes else "unknown size"
        media_type = "image" if att.mime_type.startswith("image/") else "file"
        name_part = f", name={att.file_name}" if att.file_name else ""
        parts.append(f"{media_type} ({att.mime_type}, {size_str}{name_part})")
    return "\n\n[User attached: " + "; ".join(parts) + "]"


def format_retrieved_memory_context(result: RetrievalResult, max_chars: int = 4000) -> str:
    """Render compact retrieved memory context for prompt injection."""
    if not result.scored_artifacts:
        return ""
    lines = ["[Relevant memory context] Use only if useful for this reply."]
    for scored in result.scored_artifacts:
        artifact = scored.artifact
        fm = artifact.frontmatter
        body = (artifact.body or "").strip() or "[empty]"
        entities = ", ".join(fm.entities) if fm.entities else "-"
        tags = ", ".join(fm.tags) if fm.tags else "-"
        lines.append(
            f"- type={fm.type.value}; id={fm.memory_id}; score={scored.score:.3f}; "
            f"entities={entities}; tags={tags}; body={body}"
        )
    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[: max_chars - 24] + "\n[Memory context truncated]"
    return text


def _is_multimodal_supported(mime_type: str) -> bool:
    return mime_type in _IMAGE_MIME_TYPES or mime_type == _PDF_MIME_TYPE


def _is_text_attachment(att: AttachmentMeta) -> bool:
    mime_type = att.mime_type
    if mime_type.startswith("text/") or mime_type in _TEXT_MIME_TYPES:
        return True
    if mime_type != _FALLBACK_MIME_TYPE or not att.file_name:
        return False
    lower_name = att.file_name.lower()
    return any(lower_name.endswith(ext) for ext in _TEXT_ATTACHMENT_EXTENSIONS)


def _build_text_attachment_block(att: AttachmentMeta, data: bytes) -> dict[str, str] | None:
    if not data:
        return None
    if att.mime_type == _FALLBACK_MIME_TYPE and b"\x00" in data[:2048]:
        return None
    decoded = data.decode("utf-8", errors="replace").strip()
    if not decoded:
        return None
    clipped = decoded[:_TEXT_ATTACHMENT_CHAR_BUDGET]
    if len(decoded) > _TEXT_ATTACHMENT_CHAR_BUDGET:
        clipped = clipped + "\n\n[Attachment text truncated]"
    label = att.mime_type
    if att.file_name:
        label = f"{label} ({att.file_name})"
    return {"type": "text", "text": f"[Attachment content: {label}]\n{clipped}"}


async def build_user_content_blocks(
    raw_text: str | None,
    attachments: list[AttachmentMeta],
    downloader: AttachmentDownloaderInterface | None,
    trace_id: str,
) -> list[dict[str, Any]] | None:
    if not downloader or not attachments:
        return None

    blocks: list[dict[str, Any]] = []
    if raw_text and raw_text.strip() and raw_text != _PLACEHOLDER_EMPTY:
        blocks.append({"type": "text", "text": raw_text.strip()})

    has_attachment_payload = False
    for att in attachments:
        is_multimodal = _is_multimodal_supported(att.mime_type)
        is_textual = _is_text_attachment(att)
        if not is_multimodal and not is_textual:
            continue
        try:
            data = await downloader.download(
                att.file_id, att.mime_type, att.file_size_bytes, trace_id
            )
        except Exception as exc:
            logger.warning(
                "orchestrator.attachment_download_error",
                file_id=att.file_id,
                error=str(exc),
                trace_id=trace_id,
            )
            continue
        if data is None:
            continue
        b64 = base64.standard_b64encode(data).decode("utf-8")
        if att.mime_type in _IMAGE_MIME_TYPES:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": att.mime_type,
                        "data": b64,
                    },
                }
            )
            has_attachment_payload = True
        elif att.mime_type == _PDF_MIME_TYPE:
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": b64,
                    },
                }
            )
            has_attachment_payload = True
        else:
            text_block = _build_text_attachment_block(att, data)
            if text_block is not None:
                blocks.append(text_block)
                has_attachment_payload = True

    if not has_attachment_payload:
        return None
    return blocks


def _record_to_tool_use_block(record: SessionRecord) -> dict[str, Any]:
    """Convert an ASSISTANT_TOOL_CALL record to a tool_use block."""
    tool_name = record.payload.get("tool_name", "")
    args_json = record.payload.get("arguments_json", "{}")
    try:
        args = json.loads(args_json)
    except json.JSONDecodeError:
        args = {}
    _debug_log_ask_question_args(tool_name, args)
    return {
        "type": "tool_use",
        "id": record.payload.get("tool_call_id", ""),
        "name": tool_name,
        "input": args,
    }


def _inline_memory_result_ids(records: list[SessionRecord]) -> set[str]:
    """Return memory tool_call_ids whose result was persisted inline with the call.

    Legacy sessions persisted memory_propose_update results out of order (after the
    assistant message, or appended later by the old confirmation flow). Replaying
    those would break tool_use/tool_result adjacency, so only inline pairs are kept.
    """
    ids: set[str] = set()
    for record in records:
        if (
            record.record_type == SessionRecordType.TOOL_RESULT
            and record.payload.get("tool_name") == MEMORY_TOOL_NAME
        ):
            tid = record.payload.get("tool_call_id", "")
            if tid and record.event_id == _inline_result_event_id(tid):
                ids.add(tid)
    return ids


def _inline_result_event_id(tool_call_id: str) -> str:
    # Matches the event_id written by agent.session_record_builder.
    return f"tool-result-{tool_call_id}"


def _is_replayable(record: SessionRecord, inline_memory_ids: set[str]) -> bool:
    if record.payload.get("tool_name") != MEMORY_TOOL_NAME:
        return True
    tid = record.payload.get("tool_call_id", "")
    if tid not in inline_memory_ids:
        return False
    return (
        record.record_type != SessionRecordType.TOOL_RESULT
        or record.event_id == _inline_result_event_id(tid)
    )


def records_to_messages(records: list[SessionRecord]) -> list[LLMMessage]:
    """Convert session records to LLM messages including tool-use and tool-result blocks."""
    inline_memory_ids = _inline_memory_result_ids(records)
    records = [
        r
        for r in records
        if r.record_type
        not in (SessionRecordType.ASSISTANT_TOOL_CALL, SessionRecordType.TOOL_RESULT)
        or _is_replayable(r, inline_memory_ids)
    ]
    messages: list[LLMMessage] = []
    i = 0
    while i < len(records):
        record = records[i]
        if record.record_type == SessionRecordType.USER_MESSAGE:
            content = record.payload.get("content", "")
            if content and not is_confirmation_record(record):
                # Only the recorder's synthetic turn may carry a resolution marker.
                content = neutralize_confirmation_marker(content)
            if content:
                messages.append(LLMMessage(role=MessageRole.USER, content=content))
            i += 1
        elif record.record_type == SessionRecordType.ASSISTANT_MESSAGE:
            content = record.payload.get("content", "")
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": content})
            j = i + 1
            while (
                j < len(records) and records[j].record_type == SessionRecordType.ASSISTANT_TOOL_CALL
            ):
                blocks.append(_record_to_tool_use_block(records[j]))
                j += 1
            if blocks:
                if len(blocks) == 1 and blocks[0].get("type") == "text":
                    messages.append(
                        LLMMessage(role=MessageRole.ASSISTANT, content=blocks[0].get("text", ""))
                    )
                else:
                    messages.append(
                        LLMMessage(
                            role=MessageRole.ASSISTANT,
                            content="",
                            content_blocks=blocks,
                        )
                    )
            i = j
        elif record.record_type == SessionRecordType.COMPACTION_SUMMARY:
            content = record.payload.get("content", "")
            if content:
                messages.append(LLMMessage(role=MessageRole.USER, content=content))
            i += 1
        elif record.record_type == SessionRecordType.ASSISTANT_TOOL_CALL:
            blocks = []
            j = i
            while (
                j < len(records) and records[j].record_type == SessionRecordType.ASSISTANT_TOOL_CALL
            ):
                blocks.append(_record_to_tool_use_block(records[j]))
                j += 1
            if blocks:
                messages.append(
                    LLMMessage(
                        role=MessageRole.ASSISTANT,
                        content="",
                        content_blocks=blocks,
                    )
                )
            i = j
        elif record.record_type == SessionRecordType.TOOL_RESULT:
            # Collect consecutive TOOL_RESULT records, deduplicating by tool_call_id
            # (Anthropic requires exactly one tool_result per tool_use; keep the last).
            seen: dict[str, dict[str, Any]] = {}
            j = i
            while j < len(records) and records[j].record_type == SessionRecordType.TOOL_RESULT:
                tr = records[j]
                tool_name = tr.payload.get("tool_name", "")
                tid = tr.payload.get("tool_call_id", "")
                result = tr.payload.get("result")
                error = tr.payload.get("error")
                content_str = (
                    json.dumps(result, separators=(",", ":")) if result is not None else ""
                )
                seen[tid] = {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "tool_name": tool_name,
                    "content": content_str,
                    "is_error": error is not None,
                }
                j += 1
            blocks = list(seen.values())
            if blocks:
                messages.append(
                    LLMMessage(
                        role=MessageRole.USER,
                        content="",
                        content_blocks=blocks,
                    )
                )
            i = j
        else:
            i += 1
    return messages
