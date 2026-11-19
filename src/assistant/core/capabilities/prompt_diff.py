"""
Component ID: CMP_CORE_CAPABILITIES

Full, line-based diff of a capability ``prompt`` change, split into message-sized parts.

The approver must see every added and removed prompt line in full, so the prompt change
is not part of the (size-capped) summary: it is delivered as separate plain-text parts
(``ConfirmationAttachment``) before the Approve / Reject buttons.

Rendering:

- unified style: ``@@ -a,b +c,d @@`` hunk headers, then one line per prompt line
  prefixed ``- `` (removed), ``+ `` (added) or two spaces (context, up to
  ``CONTEXT_LINES`` around each change). An added or removed prompt (``""`` on one side)
  is one hunk with every line. Content lines always start with one of those prefixes,
  so no prompt text can produce a hunk header;
- lines are split at ``\\n`` only. Every other line break (``\\r``, U+2028, ...),
  control, format (bidi, zero-width), non-U+0020 space and blank-rendering character is
  escaped as ``\\uXXXX`` (the same character set as ``edit_summary.inline``). Other
  non-ASCII text is kept as is (prompts are often Russian; ``\\uXXXX`` for every Cyrillic
  letter would make them unreadable). Unlike the ⚠ lines, this view therefore does not
  expose homoglyphs: the prompt is free text the model reads, not an identifier that a
  lookalike could redirect;
- a line longer than one part is split across parts; its continuation pieces start with
  the same marker followed by ``…`` (``+…``, ``-…``, `` …``) instead of a space;
- a part never starts with a blank context line (it could then be blank as a whole,
  which a message cannot carry): it starts with an empty continuation (``+…``, `` …``,
  ...) of the previous part's last line instead.

Bounds: each side may have at most ``MAX_PROMPT_LINES`` lines (``PromptDiffTooLargeError``
otherwise), and the diff is computed with ``difflib.SequenceMatcher`` (default autojunk)
on the lines between the common prefix and suffix, which is near-linear for prompt-sized
inputs. ``difflib.ndiff``'s pairwise similarity search is super-linear and is not used.
"""

import difflib
from dataclasses import dataclass

from assistant.core.capabilities.text_safety import escape_unsafe, utf16_len

CONTEXT_LINES = 3
MAX_PROMPT_LINES = 5000
_CONTINUATION = "…"
# A line that must be split anyway only fills the rest of a part if this much is left.
_MIN_SPLIT_ROOM = 64


class PromptDiffTooLargeError(Exception):
    """A side of the prompt change has more lines than the diff accepts."""

    def __init__(self, lines: int, max_lines: int) -> None:
        super().__init__(f"the prompt has {lines} lines (limit {max_lines})")
        self.lines = lines
        self.max_lines = max_lines


@dataclass(frozen=True)
class PromptDiff:
    """Rendered diff lines (sanitised, prefixed) and the added / removed line counts."""

    lines: tuple[str, ...]
    added: int
    removed: int


def prompt_lines(text: str) -> list[str]:
    """The lines of a prompt, split at ``\\n`` only (``""`` has no lines)."""
    return text.split("\n") if text else []


def prompt_diff(old: str, new: str, *, context: int = CONTEXT_LINES) -> PromptDiff:
    """Diff of two prompts; every added and removed line is included in full."""
    a, b = prompt_lines(old), prompt_lines(new)
    for side in (a, b):
        if len(side) > MAX_PROMPT_LINES:
            raise PromptDiffTooLargeError(len(side), MAX_PROMPT_LINES)
    # The common prefix / suffix is matched in linear time; SequenceMatcher only sees the
    # changed middle.
    head = 0
    while head < min(len(a), len(b)) and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < min(len(a), len(b)) - head and a[-1 - tail] == b[-1 - tail]:
        tail += 1
    matcher = difflib.SequenceMatcher(None, a[head : len(a) - tail], b[head : len(b) - tail])
    opcodes: list[tuple[str, int, int, int, int]] = []
    if head:
        opcodes.append(("equal", 0, head, 0, head))
    opcodes.extend(
        (tag, i1 + head, i2 + head, j1 + head, j2 + head)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
    )
    if tail:
        opcodes.append(("equal", len(a) - tail, len(a), len(b) - tail, len(b)))

    lines: list[str] = []
    added = removed = 0
    for group in _grouped(opcodes, context):
        first, last = group[0], group[-1]
        lines.append(_hunk_header(first[1], last[2], first[3], last[4]))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines.extend(_line(" ", text) for text in a[i1:i2])
                continue
            lines.extend(_line("-", text) for text in a[i1:i2])
            lines.extend(_line("+", text) for text in b[j1:j2])
            removed += i2 - i1
            added += j2 - j1
    return PromptDiff(lines=tuple(lines), added=added, removed=removed)


def _grouped(
    opcodes: list[tuple[str, int, int, int, int]], context: int
) -> list[list[tuple[str, int, int, int, int]]]:
    """``SequenceMatcher.get_grouped_opcodes`` on a precomputed opcode list."""
    codes = [c for c in opcodes if c[1] != c[2] or c[3] != c[4]]
    if not any(tag != "equal" for tag, *_ in codes):
        return []
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = (tag, max(i1, i2 - context), i2, max(j1, j2 - context), j2)
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = (tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context))
    groups: list[list[tuple[str, int, int, int, int]]] = []
    group: list[tuple[str, int, int, int, int]] = []
    span = 2 * context
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > span:
            group.append((tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)))
            groups.append(group)
            group = []
            i1, j1 = max(i1, i2 - context), max(j1, j2 - context)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        groups.append(group)
    return groups


def _hunk_header(i1: int, i2: int, j1: int, j2: int) -> str:
    def span(start: int, stop: int) -> str:
        length = stop - start
        return f"{start + 1 if length else start},{length}"

    return f"@@ -{span(i1, i2)} +{span(j1, j2)} @@"


def _line(marker: str, text: str) -> str:
    return f"{marker} {escape_unsafe(text)}"


def split_parts(lines: tuple[str, ...] | list[str], max_units: int) -> list[str]:
    """Pack ``lines`` into parts of at most ``max_units`` UTF-16 units, in order.

    Lines are joined with ``\\n``. A line that fits one part is never split (it moves to
    the next part instead); a longer line fills the rest of the current part and
    continues in the next ones, each continuation piece starting with its marker and
    ``…`` (never between the two halves of an astral character). Nothing is dropped.

    No part after the first starts with a whitespace-only piece (an empty or all-space
    context line): such a part could be entirely blank, which a message cannot carry
    (``ConfirmationAttachment`` refuses it). The part starts with an empty continuation
    of the previous part's last line instead (its marker and ``…``, nothing after), so
    every part has visible text and joining the continuations back still gives
    ``lines`` exactly. The first part starts with ``lines[0]`` (a hunk header in a diff).
    """
    if max_units < _MIN_SPLIT_ROOM:
        raise ValueError(f"max_units must be at least {_MIN_SPLIT_ROOM}")
    parts: list[str] = []
    current: list[str] = []
    used = 0
    last = ""  # the last piece placed
    pinned = False  # current holds only an empty continuation: it must not be flushed
    for line in lines:
        piece = line
        while True:
            if not current and parts and not piece.strip():
                marker = last[:1] + _CONTINUATION
                current, used, pinned = [marker], utf16_len(marker), True
            sep = 1 if current else 0
            width = utf16_len(piece)
            if used + sep + width <= max_units:
                current.append(piece)
                used += sep + width
                last, pinned = piece, False
                break
            room = max_units - used - sep
            if current and not pinned and (width <= max_units or room < _MIN_SPLIT_ROOM):
                parts.append("\n".join(current))
                current, used = [], 0
                continue
            head, rest = _cut(piece, room)
            current.append(head)
            parts.append("\n".join(current))
            current, used, last, pinned = [], 0, head, False
            piece = piece[:1] + _CONTINUATION + rest
    if current:
        parts.append("\n".join(current))
    return parts


def _cut(piece: str, room: int) -> tuple[str, str]:
    """``piece`` cut after at most ``room`` units (its 2-char prefix kept on the head)."""
    used = 2
    for index, char in enumerate(piece[2:], start=2):
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > room:
            return piece[:index], piece[index:]
        used += width
    return piece, ""
