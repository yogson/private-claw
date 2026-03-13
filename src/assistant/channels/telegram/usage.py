"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Telegram usage statistics aggregation and cost calculation.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from genai_prices import Usage, calc_price

from assistant.channels.telegram.models import ChannelResponse, MessageType, NormalizedEvent
from assistant.store.filesystem.atomic import ensure_directory, file_append_lines
from assistant.store.interfaces import SessionStoreInterface
from assistant.store.models import SessionRecord, SessionRecordType

_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL_REF = "claude-sonnet-4-5"


@dataclass
class _UsageBucket:
    input_tokens: int
    output_tokens: int
    cost_usd: float


def _parse_session_chat_id(session_id: str) -> int | None:
    """Extract chat_id from tg:{chat_id} or tg:{chat_id}:* session_id."""
    if not session_id.startswith("tg:"):
        return None
    rest = session_id[3:]
    if ":" in rest:
        rest = rest.split(":")[0]
    try:
        return int(rest)
    except ValueError:
        return None


def _session_belongs_to_user(session_id: str, user_id: str, records: list[SessionRecord]) -> bool:
    """Return True if session belongs to the user (user-scoped or chat-derived)."""
    chat_id = _parse_session_chat_id(session_id)
    if chat_id is not None and str(chat_id) == str(user_id):
        return True
    for r in records:
        if r.record_type == SessionRecordType.USER_MESSAGE:
            payload = r.payload or {}
            if payload.get("user_id") == user_id:
                return True
    return False


def _turn_user_id_map(records: list[SessionRecord]) -> dict[str, str]:
    """Build turn_id -> user_id from user_message records for turn-level attribution."""
    mapping: dict[str, str] = {}
    for r in records:
        if r.record_type == SessionRecordType.USER_MESSAGE:
            uid = (r.payload or {}).get("user_id")
            if isinstance(uid, str) and uid:
                mapping[r.turn_id] = uid
    return mapping


def _record_date(record: SessionRecord) -> date | None:
    """Extract date from record timestamp in UTC."""
    ts = record.timestamp
    if ts is None:
        return None
    if hasattr(ts, "date"):
        return ts.date()
    return date(ts.year, ts.month, ts.day)


def _calc_cost(in_tok: int, out_tok: int, model_ref: str) -> float:
    """Compute cost in USD for given token counts and model."""
    ref = (model_ref or _DEFAULT_MODEL_REF).removeprefix("anthropic:")
    if not ref:
        ref = _DEFAULT_MODEL_REF
    try:
        price = calc_price(
            Usage(input_tokens=in_tok, output_tokens=out_tok),
            ref,
            provider_id=_DEFAULT_PROVIDER,
        )
        return float(price.total_price)
    except Exception:
        return 0.0


def _archive_path(archive_dir: Path, user_id: str) -> Path:
    """Path to user's usage archive file (JSONL)."""
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
    return archive_dir / f"{safe_id}.jsonl"


class UsageStatsService:
    """
    Aggregates token usage and cost for Telegram users across session/day/month.

    Uses persisted assistant_message records with usage payloads and genai-prices
    for cost calculation. User-scoped rollups use user_id from user_message
    payloads with fallback to chat-derived session matching.

    When a session is cleared via /reset, usage is archived before deletion so
    Today and This month stats persist.
    """

    def __init__(
        self,
        session_store: SessionStoreInterface,
        archive_dir: Path | None = None,
        default_model_id: str = "claude-sonnet-4-5",
    ) -> None:
        self._store = session_store
        self._archive_dir = archive_dir
        self._default_model_id = default_model_id

    async def archive_session_usage(self, session_id: str, user_id: str) -> None:
        """
        Extract usage from session records and append to user's archive.
        Call before clear_session so Today/Month stats persist after /reset.
        """
        if self._archive_dir is None:
            return
        records = await self._store.read_session(session_id)
        if not records:
            return
        if not _session_belongs_to_user(session_id, user_id, records):
            return
        turn_to_user = _turn_user_id_map(records)
        lines: list[str] = []
        for r in records:
            if r.record_type != SessionRecordType.ASSISTANT_MESSAGE:
                continue
            payload = r.payload or {}
            record_user_id = payload.get("user_id") or turn_to_user.get(r.turn_id)
            if record_user_id != user_id:
                continue
            usage = payload.get("usage")
            if not isinstance(usage, dict):
                continue
            in_tok = int(usage.get("input_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or 0)
            if in_tok == 0 and out_tok == 0:
                continue
            model_ref = str(payload.get("model_id") or self._default_model_id)
            cost = _calc_cost(in_tok, out_tok, model_ref)
            rec_date = _record_date(r)
            if rec_date is None:
                continue
            lines.append(
                json.dumps(
                    {
                        "date": rec_date.isoformat(),
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "cost_usd": cost,
                        "model_id": model_ref,
                    },
                    separators=(",", ":"),
                )
            )
        if not lines:
            return
        ensure_directory(self._archive_dir)
        path = _archive_path(self._archive_dir, user_id)
        await file_append_lines(path, lines)

    def _read_archive(self, user_id: str) -> list[tuple[date, int, int, float]]:
        """Read archived usage for user. Returns list of (date, in_tok, out_tok, cost)."""
        if self._archive_dir is None:
            return []
        path = _archive_path(self._archive_dir, user_id)
        if not path.exists():
            return []
        result: list[tuple[date, int, int, float]] = []
        for line in path.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                d = data.get("date")
                if not d:
                    continue
                rec_date = date.fromisoformat(d) if isinstance(d, str) else None
                if rec_date is None:
                    continue
                in_tok = int(data.get("input_tokens") or 0)
                out_tok = int(data.get("output_tokens") or 0)
                cost = float(data.get("cost_usd") or 0)
                result.append((rec_date, in_tok, out_tok, cost))
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        return result

    async def build_usage_response(self, event: NormalizedEvent) -> ChannelResponse:
        """Build a ChannelResponse with usage stats for current session, today, and month."""
        user_id = event.user_id
        session_id = event.session_id
        now = datetime.now(UTC)
        today = now.date()
        month_start = date(today.year, today.month, 1)

        session_bucket = _UsageBucket(0, 0, 0.0)
        day_bucket = _UsageBucket(0, 0, 0.0)
        month_bucket = _UsageBucket(0, 0, 0.0)

        all_sessions = await self._store.list_sessions()
        for sid in all_sessions:
            records = await self._store.read_session(sid)
            if not records:
                continue
            if not _session_belongs_to_user(sid, user_id, records):
                continue
            turn_to_user = _turn_user_id_map(records)
            for r in records:
                if r.record_type != SessionRecordType.ASSISTANT_MESSAGE:
                    continue
                payload = r.payload or {}
                record_user_id = payload.get("user_id")
                if record_user_id is None:
                    record_user_id = turn_to_user.get(r.turn_id)
                if record_user_id != user_id:
                    continue
                usage = payload.get("usage")
                if not isinstance(usage, dict):
                    continue
                in_tok = int(usage.get("input_tokens") or 0)
                out_tok = int(usage.get("output_tokens") or 0)
                if in_tok == 0 and out_tok == 0:
                    continue
                model_ref = payload.get("model_id") or self._default_model_id
                cost = _calc_cost(in_tok, out_tok, str(model_ref))
                if sid == session_id:
                    session_bucket = _UsageBucket(
                        session_bucket.input_tokens + in_tok,
                        session_bucket.output_tokens + out_tok,
                        session_bucket.cost_usd + cost,
                    )
                rec_date = _record_date(r)
                if rec_date is not None:
                    if rec_date == today:
                        day_bucket = _UsageBucket(
                            day_bucket.input_tokens + in_tok,
                            day_bucket.output_tokens + out_tok,
                            day_bucket.cost_usd + cost,
                        )
                    if rec_date >= month_start:
                        month_bucket = _UsageBucket(
                            month_bucket.input_tokens + in_tok,
                            month_bucket.output_tokens + out_tok,
                            month_bucket.cost_usd + cost,
                        )

        for rec_date, in_tok, out_tok, cost in self._read_archive(user_id):
            if rec_date == today:
                day_bucket = _UsageBucket(
                    day_bucket.input_tokens + in_tok,
                    day_bucket.output_tokens + out_tok,
                    day_bucket.cost_usd + cost,
                )
            if rec_date >= month_start:
                month_bucket = _UsageBucket(
                    month_bucket.input_tokens + in_tok,
                    month_bucket.output_tokens + out_tok,
                    month_bucket.cost_usd + cost,
                )

        lines = [
            "*Usage statistics*",
            "",
            "*Current session*",
            f"  Tokens: {session_bucket.input_tokens} in / {session_bucket.output_tokens} out",
            f"  Cost: ${session_bucket.cost_usd:.4f}",
            "",
            "*Today (UTC)*",
            f"  Tokens: {day_bucket.input_tokens} in / {day_bucket.output_tokens} out",
            f"  Cost: ${day_bucket.cost_usd:.4f}",
            "",
            "*This month (UTC)*",
            f"  Tokens: {month_bucket.input_tokens} in / {month_bucket.output_tokens} out",
            f"  Cost: ${month_bucket.cost_usd:.4f}",
        ]
        text = "\n".join(lines)
        if (
            session_bucket.input_tokens == 0
            and session_bucket.output_tokens == 0
            and day_bucket.input_tokens == 0
            and day_bucket.output_tokens == 0
            and month_bucket.input_tokens == 0
            and month_bucket.output_tokens == 0
        ):
            text = "No usage recorded yet. Send a message to start tracking."

        return ChannelResponse(
            response_id=str(uuid.uuid4()),
            channel="telegram",
            session_id=session_id,
            trace_id=event.trace_id,
            message_type=MessageType.TEXT,
            text=text,
            parse_mode="Markdown",
        )
