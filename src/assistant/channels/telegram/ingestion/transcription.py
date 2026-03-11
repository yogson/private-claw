"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

VoiceTranscriptionService: wraps the MTProto transcription worker with
timeout control and structured audit logging for the voice intake pipeline.
"""

import asyncio

import structlog

from assistant.channels.telegram.ingestion.interfaces import TranscriptionWorkerInterface

logger = structlog.get_logger(__name__)

_REASON_TIMEOUT = "timeout"
_REASON_EMPTY = "empty_result"


class VoiceTranscriptionService:
    """
    Wraps a TranscriptionWorkerInterface with configurable timeout and
    structured audit logging.

    Returns a (transcript_text, failure_reason) tuple so the caller can
    always proceed regardless of transcription outcome.
    """

    def __init__(self, worker: TranscriptionWorkerInterface, timeout_seconds: int = 10) -> None:
        self._worker = worker
        self._timeout = timeout_seconds

    async def transcribe(
        self, file_id: str, duration_seconds: int, trace_id: str
    ) -> tuple[str | None, str | None]:
        """
        Attempt voice transcription with timeout.

        Returns (transcript_text, None) on success.
        Returns (None, failure_reason) on timeout, empty result, or error.
        Never raises; all failures are captured and audited.
        """
        try:
            transcript = await asyncio.wait_for(
                self._worker.transcribe(file_id, duration_seconds),
                timeout=float(self._timeout),
            )
        except TimeoutError:
            logger.warning(
                "telegram.transcription.timeout",
                file_id=file_id,
                timeout_seconds=self._timeout,
                trace_id=trace_id,
            )
            return None, _REASON_TIMEOUT
        except Exception as exc:
            reason = f"error:{type(exc).__name__}"
            logger.warning(
                "telegram.transcription.error",
                file_id=file_id,
                error=str(exc),
                reason=reason,
                trace_id=trace_id,
            )
            return None, reason

        if not transcript:
            logger.info(
                "telegram.transcription.empty",
                file_id=file_id,
                trace_id=trace_id,
            )
            return None, _REASON_EMPTY

        logger.info(
            "telegram.transcription.success",
            file_id=file_id,
            trace_id=trace_id,
        )
        return transcript, None
