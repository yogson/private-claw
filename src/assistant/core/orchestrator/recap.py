"""
Component ID: CMP_CORE_SESSION_RECAP

On-demand session recap for the ``/wtf`` command.

Uses a cheap LLM (claude-haiku-4-5 by default) in a one-shot call to recap the
user ↔ assistant dialogue of a session in a few dense lines. Tool calls, tool
results, system prompts and synthetic confirmation turns are excluded. The recap
is never persisted and never enters the main agent's context.
"""

from collections.abc import Sequence

import structlog
from pydantic_ai import Agent

from assistant.core.confirmation.history import is_confirmation_record
from assistant.core.orchestrator.persistence import FAILED_TURN_PLACEHOLDER
from assistant.store.models import SessionRecord, SessionRecordType

logger = structlog.get_logger(__name__)

RECAP_PROMPT = """\
You write a brief status recap of a conversation between a user and an AI assistant.
The reader has not seen the conversation — a colleague glancing over, or the user
returning after a break — and wants to understand in a few seconds what this work
is about and where it stands.

Describe the work at the level of the task: what problem is being solved or what is
being built, and why it matters. Tell progress as meaningful outcomes ("the fix is
reviewed and accepted") rather than as the individual steps and artifacts that
produced them. Name things by what they are and what they do — "the session recap
command", "the fix for duplicate log entries" — so every line makes sense without
knowing the project's internals. If the conversation touches several topics, focus
on the main one.

The input may begin with an "Earlier context" block — a summary of the older part of
this same conversation. Use it as background; where it differs from the dialogue that
follows, the dialogue shows the current state.

Write in the same language as the conversation, in plain, natural phrasing.

Format:
🎯 <one sentence: the task and its current stage>
✅ Done:
• <up to 5 short bullets — key results so far>
⏭ Next:
• <up to 3 short bullets — what remains to finish the task>
If the task is finished and nothing remains, end the recap after the ✅ Done section.

Output only the recap.\
"""

OMITTED_MARKER = "[earlier messages omitted]"
_TRUNCATED_SUFFIX = " […]"
_EARLIER_CONTEXT_HEADER = "Earlier context:"
_DIALOGUE_HEADER = "Dialogue:"
# Share of the input budget the earlier-context (compaction summary) block may take.
_EARLIER_CONTEXT_BUDGET_SHARE = 3

_DIALOGUE_ROLES: dict[SessionRecordType, str] = {
    SessionRecordType.USER_MESSAGE: "USER",
    SessionRecordType.ASSISTANT_MESSAGE: "ASSISTANT",
}


def dialogue_lines(records: Sequence[SessionRecord]) -> list[tuple[str, str]]:
    """Return ``(role, text)`` pairs for user/assistant messages in sequence order.

    Drops every other record type (tool calls, tool results, system messages, turn
    summaries/terminals, compaction summaries), synthetic confirmation turns, empty
    content, and the placeholder reply written for failed turns.
    """
    lines: list[tuple[str, str]] = []
    for record in sorted(records, key=lambda r: r.sequence):
        role = _DIALOGUE_ROLES.get(record.record_type)
        if role is None or is_confirmation_record(record):
            continue
        content = record.payload.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text or text == FAILED_TURN_PLACEHOLDER:
            continue
        lines.append((role, text))
    return lines


def earlier_context(records: Sequence[SessionRecord]) -> list[str]:
    """Return compaction summaries (oldest → newest): the only trace of compacted dialogue."""
    summaries: list[str] = []
    for record in sorted(records, key=lambda r: r.sequence):
        if record.record_type != SessionRecordType.COMPACTION_SUMMARY:
            continue
        content = record.payload.get("content")
        if isinstance(content, str) and content.strip():
            summaries.append(content.strip())
    return summaries


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` chars, marking the cut."""
    if len(text) <= limit:
        return text
    keep = max(limit - len(_TRUNCATED_SUFFIX), 0)
    return text[:keep].rstrip() + _TRUNCATED_SUFFIX


def _fit_recent(rendered: list[str], budget: int) -> tuple[list[str], bool]:
    """Keep the most recent rendered lines within ``budget`` chars (walking backwards).

    The newest line is always kept, truncated if it alone exceeds the budget.
    Returns the kept lines in original order and whether anything was trimmed.
    """
    kept: list[str] = []
    used = 0
    trimmed = False
    for line in reversed(rendered):
        cost = len(line) + (2 if kept else 0)  # "\n\n" separator
        if used + cost <= budget:
            kept.append(line)
            used += cost
            continue
        if not kept:
            kept.append(_truncate(line, budget))
        trimmed = True
        break
    kept.reverse()
    return kept, trimmed


def build_recap_input(records: Sequence[SessionRecord], max_input_chars: int) -> str | None:
    """Render the recap LLM input, or ``None`` when the session has nothing to recap.

    Compaction summaries come first as an "Earlier context" block (capped at a third
    of the budget, keeping the newest text); the dialogue fills the rest, newest
    messages first, with ``OMITTED_MARKER`` prepended when older messages were dropped.
    """
    lines = dialogue_lines(records)
    summaries = earlier_context(records)
    if not lines and not summaries:
        return None

    parts: list[str] = []
    remaining = max_input_chars
    if summaries:
        joined = "\n\n".join(summaries)
        cap = max_input_chars // _EARLIER_CONTEXT_BUDGET_SHARE if lines else max_input_chars
        if len(joined) > cap:
            keep = max(cap - len(_TRUNCATED_SUFFIX), 0)
            joined = _TRUNCATED_SUFFIX.strip() + " " + joined[len(joined) - keep :].lstrip()
        parts.append(f"{_EARLIER_CONTEXT_HEADER}\n{joined}")
        remaining -= len(joined)

    if lines:
        rendered = [f"{role}: {text}" for role, text in lines]
        kept, trimmed = _fit_recent(rendered, max(remaining, 1))
        if trimmed:
            kept.insert(0, OMITTED_MARKER)
        parts.append(f"{_DIALOGUE_HEADER}\n" + "\n\n".join(kept))
    return "\n\n".join(parts)


class SessionRecapService:
    """Recaps a session's dialogue using a lightweight one-shot LLM agent."""

    def __init__(
        self,
        model_id: str,
        max_tokens: int = 512,
        max_input_chars: int = 30_000,
        prompt: str = "",
    ) -> None:
        effective_prompt = prompt.strip() if prompt.strip() else RECAP_PROMPT
        self._agent: Agent[object, str] = Agent(
            f"anthropic:{model_id}",
            system_prompt=effective_prompt,
            output_type=str,
            # Recap is on by default: resolve the provider (and its API key) on first
            # use, so a missing key fails a /wtf call rather than orchestrator startup.
            defer_model_check=True,
        )
        self._max_tokens = max_tokens
        self._max_input_chars = max_input_chars

    async def recap(self, records: Sequence[SessionRecord], trace_id: str) -> str | None:
        """Recap the session records.

        Returns ``None`` (without calling the LLM) when there is no dialogue to recap.
        LLM errors propagate; the caller logs them as ``recap.failed``.
        """
        formatted = build_recap_input(records, self._max_input_chars)
        if formatted is None:
            logger.info("recap.empty", trace_id=trace_id, record_count=len(records))
            return None
        logger.info(
            "recap.start",
            trace_id=trace_id,
            record_count=len(records),
            input_chars=len(formatted),
        )
        result = await self._agent.run(formatted, model_settings={"max_tokens": self._max_tokens})
        recap = result.output.strip()
        logger.info("recap.complete", trace_id=trace_id, recap_chars=len(recap))
        return recap
