"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Payload helpers for user input extraction and multimodal attachment packaging.
"""

import base64
from typing import Any

import structlog

from assistant.core.attachments import AttachmentDownloaderInterface
from assistant.core.events.models import AttachmentMeta, OrchestratorEvent
from assistant.providers.interfaces import LLMMessage, MessageRole
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


def extract_user_text(event: OrchestratorEvent) -> str:
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
    return _PLACEHOLDER_EMPTY


def extract_raw_text_for_multimodal(event: OrchestratorEvent) -> str | None:
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
    return None


def gather_attachments(event: OrchestratorEvent) -> list[AttachmentMeta]:
    out: list[AttachmentMeta] = []
    if event.attachment and event.attachment.file_id:
        out.append(event.attachment)
    for att in event.attachments or []:
        if att.file_id:
            out.append(att)
    return out


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


def records_to_messages(records: list[SessionRecord]) -> list[LLMMessage]:
    messages: list[LLMMessage] = []
    for record in records:
        if record.record_type == SessionRecordType.USER_MESSAGE:
            content = record.payload.get("content", "")
            if content:
                messages.append(LLMMessage(role=MessageRole.USER, content=content))
        elif record.record_type == SessionRecordType.ASSISTANT_MESSAGE:
            content = record.payload.get("content", "")
            if content:
                messages.append(LLMMessage(role=MessageRole.ASSISTANT, content=content))
    return messages
