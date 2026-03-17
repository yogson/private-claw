"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Pending memory confirmation query and apply/reject lifecycle helpers.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from assistant.extensions.first_party.memory import (
    MemoryProposalToolCall,
    canonicalize_memory_args,
    memory_propose_update,
)
from assistant.memory.interfaces import MemoryWriterInterface
from assistant.store.interfaces import SessionStoreInterface
from assistant.store.models import SessionRecord, SessionRecordType, ToolResultPayload

_PENDING_STATUS = "pending_confirmation"
_USER_SAFE_APPLY_FAILED = "Memory update could not be applied. Please try again later."
logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class PendingMemoryConfirmation:
    session_id: str
    turn_id: str
    tool_call_id: str
    proposal: MemoryProposalToolCall
    raw_arguments_json: str


class MemoryConfirmationService:
    """Reads pending memory intents from session logs and applies user decisions."""

    def __init__(
        self, sessions: SessionStoreInterface, memory_writer: MemoryWriterInterface
    ) -> None:
        self._sessions = sessions
        self._memory_writer = memory_writer

    async def list_pending(self, session_id: str) -> list[PendingMemoryConfirmation]:
        records = await self._sessions.read_session(session_id)
        call_records: dict[str, SessionRecord] = {}
        status_by_call: dict[str, str] = {}
        memory_calls = 0
        memory_results = 0
        for record in records:
            if record.record_type == SessionRecordType.ASSISTANT_TOOL_CALL:
                if record.payload.get("tool_name") == "memory_propose_update":
                    memory_calls += 1
                    call_records[record.payload.get("tool_call_id", "")] = record
            elif record.record_type == SessionRecordType.TOOL_RESULT:
                if record.payload.get("tool_name") != "memory_propose_update":
                    continue
                memory_results += 1
                result = record.payload.get("result")
                if isinstance(result, dict):
                    status = result.get("status")
                    if isinstance(status, str):
                        status_by_call[record.payload.get("tool_call_id", "")] = status

        pending: list[PendingMemoryConfirmation] = []
        for tool_call_id, record in call_records.items():
            if status_by_call.get(tool_call_id) != _PENDING_STATUS:
                continue
            raw_json = record.payload.get("arguments_json", "{}")
            try:
                raw_args = json.loads(raw_json)
                canonicalize_memory_args(raw_args)
                proposal = MemoryProposalToolCall.model_validate(raw_args)
            except Exception:
                continue
            pending.append(
                PendingMemoryConfirmation(
                    session_id=session_id,
                    turn_id=record.turn_id,
                    tool_call_id=tool_call_id,
                    proposal=proposal,
                    raw_arguments_json=raw_json,
                )
            )
        if memory_calls > 0 or memory_results > 0:
            logger.info(
                "memory.confirmation.list_pending",
                session_id=session_id,
                record_count=len(records),
                memory_calls=memory_calls,
                memory_results=memory_results,
                status_by_call=status_by_call,
                pending_count=len(pending),
            )
        return pending

    async def resolve_pending(
        self,
        session_id: str,
        tool_call_id: str,
        approve: bool,
        user_id: str | None = None,
    ) -> tuple[bool, str]:
        pending = await self.list_pending(session_id)
        target = next((p for p in pending if p.tool_call_id == tool_call_id), None)
        if target is None:
            return False, "Confirmation expired or already resolved."

        if not approve:
            await self._append_result(
                session_id=session_id,
                turn_id=target.turn_id,
                tool_call_id=tool_call_id,
                result={"status": "rejected_by_user", "reason": "User rejected memory update"},
                error=None,
            )
            return True, "Memory update rejected."

        try:
            raw_args = json.loads(target.raw_arguments_json)
            raw_args["intent_id"] = tool_call_id
            intent = memory_propose_update(raw_args)
            effective_user_id = user_id if user_id else session_id
            audit = self._memory_writer.apply_intent(intent, user_id=effective_user_id)
            await self._append_result(
                session_id=session_id,
                turn_id=target.turn_id,
                tool_call_id=tool_call_id,
                result=json.loads(audit.model_dump_json()),
                error=None,
            )
            return True, "Memory update confirmed and applied."
        except Exception as exc:
            logger.warning(
                "confirmation.apply_failed",
                tool_call_id=tool_call_id,
                session_id=session_id,
                error=str(exc),
            )
            await self._append_result(
                session_id=session_id,
                turn_id=target.turn_id,
                tool_call_id=tool_call_id,
                result={
                    "status": "failed",
                    "reason": str(exc),
                    "requires_user_confirmation": False,
                },
                error=str(exc),
            )
            return True, _USER_SAFE_APPLY_FAILED

    async def _append_result(
        self,
        *,
        session_id: str,
        turn_id: str,
        tool_call_id: str,
        result: dict[str, object],
        error: str | None,
    ) -> None:
        next_seq = await self._sessions.get_next_sequence(session_id)
        now = datetime.now(UTC)
        payload = ToolResultPayload(
            message_id=f"msg-confirmation-result-{tool_call_id}",
            tool_call_id=tool_call_id,
            tool_name="memory_propose_update",
            result=result,
            error=error,
        )
        record = SessionRecord(
            session_id=session_id,
            sequence=next_seq,
            event_id=f"memory-confirmation-{tool_call_id}-{uuid.uuid4().hex[:8]}",
            turn_id=turn_id,
            timestamp=now,
            record_type=SessionRecordType.TOOL_RESULT,
            payload=payload.model_dump(),
        )
        await self._sessions.append([record])
