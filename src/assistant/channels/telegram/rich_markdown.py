"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Helpers for sending agent Markdown as Telegram rich messages (Bot API sendRichMessage).

Detects GFM pipe tables outside fenced code blocks and splits Markdown that exceeds the
rich message limits at block boundaries, never inside a table: an oversized table is split
into row groups, each repeating the header and separator rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

# Bot API rich message limits: 32768 UTF-8 characters (counted here as UTF-8 bytes, which is
# never fewer), 500 blocks including list items and table rows, 20 table columns.
RICH_MAX_BYTES: Final[int] = 32768
RICH_MAX_BLOCKS: Final[int] = 500
RICH_MAX_TABLE_COLUMNS: Final[int] = 20

_SEPARATOR: Final[str] = "\n\n"
_SEPARATOR_BYTES: Final[int] = len(_SEPARATOR)
_FENCE_RE: Final = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_UNESCAPED_PIPE_RE: Final = re.compile(r"(?<!\\)\|")
_DELIMITER_CELL_RE: Final = re.compile(r"^\s*:?-+:?\s*$")

_UnitKind = Literal["text", "code", "table"]


@dataclass(frozen=True)
class _Unit:
    kind: _UnitKind
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def blocks(self) -> int:
        """Conservative block estimate: every text line may be a paragraph or list item; a
        table counts itself plus each row (header included, separator excluded)."""
        if self.kind == "code":
            return 1
        if self.kind == "table":
            return len(self.lines)
        return max(1, sum(1 for line in self.lines if line.strip()))


def contains_gfm_table(markdown: str) -> bool:
    """True if the Markdown has a GFM pipe table outside fenced code blocks."""
    return any(unit.kind == "table" for unit in _parse_units(markdown))


def split_rich_markdown(
    markdown: str,
    max_bytes: int = RICH_MAX_BYTES,
    max_blocks: int = RICH_MAX_BLOCKS,
) -> list[str] | None:
    """Split Markdown into chunks that each fit one rich message.

    Returns the Markdown unchanged as a single chunk when it fits. Returns None when it
    cannot be sent as rich messages safely (a table wider than the column limit, or a code
    block, line or table header too large for one message); the caller then uses the
    regular entity path.
    """
    units = _parse_units(markdown)
    if any(
        unit.kind == "table" and _column_count(unit.lines[0]) > RICH_MAX_TABLE_COLUMNS
        for unit in units
    ):
        return None
    if (
        len(markdown.encode("utf-8")) <= max_bytes
        and sum(unit.blocks for unit in units) <= max_blocks
    ):
        return [markdown]

    pieces: list[_Unit] = []
    for unit in units:
        if unit.size <= max_bytes and unit.blocks <= max_blocks:
            pieces.append(unit)
            continue
        split = _split_oversized(unit, max_bytes, max_blocks)
        if split is None:
            return None
        pieces.extend(split)
    return _pack(pieces, max_bytes, max_blocks)


def _pack(pieces: list[_Unit], max_bytes: int, max_blocks: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    current_blocks = 0
    for piece in pieces:
        added_bytes = piece.size + (_SEPARATOR_BYTES if current else 0)
        if current and (
            current_bytes + added_bytes > max_bytes or current_blocks + piece.blocks > max_blocks
        ):
            chunks.append(_SEPARATOR.join(current))
            current, current_bytes, current_blocks = [], 0, 0
            added_bytes = piece.size
        current.append(piece.text)
        current_bytes += added_bytes
        current_blocks += piece.blocks
    if current:
        chunks.append(_SEPARATOR.join(current))
    return chunks


def _split_oversized(unit: _Unit, max_bytes: int, max_blocks: int) -> list[_Unit] | None:
    if unit.kind == "code":
        return None
    if unit.kind == "text":
        return _group_lines("text", (), unit.lines, 0, max_bytes, max_blocks)
    # Each table group repeats the header and separator rows; it costs two blocks on top of
    # its body rows (the table itself and the header row).
    return _group_lines("table", unit.lines[:2], unit.lines[2:], 2, max_bytes, max_blocks)


def _group_lines(
    kind: _UnitKind,
    prefix: tuple[str, ...],
    lines: tuple[str, ...],
    prefix_blocks: int,
    max_bytes: int,
    max_blocks: int,
) -> list[_Unit] | None:
    """Greedily group lines (one block each) into units of `prefix` + lines within limits.

    Returns None when a single line does not fit next to the prefix.
    """
    prefix_bytes = len("\n".join(prefix).encode("utf-8"))
    groups: list[_Unit] = []
    group: list[str] = []
    group_bytes = prefix_bytes
    for line in lines:
        line_bytes = len(line.encode("utf-8"))
        joined_bytes = line_bytes + (1 if prefix or group else 0)
        if prefix_bytes + line_bytes + (1 if prefix else 0) > max_bytes:
            return None
        if prefix_blocks + 1 > max_blocks:
            return None
        if group and (
            group_bytes + joined_bytes > max_bytes or prefix_blocks + len(group) + 1 > max_blocks
        ):
            groups.append(_Unit(kind, (*prefix, *group)))
            group, group_bytes = [], prefix_bytes
            joined_bytes = line_bytes + (1 if prefix else 0)
        group.append(line)
        group_bytes += joined_bytes
    if group:
        groups.append(_Unit(kind, (*prefix, *group)))
    return groups or None


def _parse_units(markdown: str) -> list[_Unit]:
    """Split Markdown into blank-line separated text units, fenced code blocks and tables."""
    lines = markdown.splitlines()
    units: list[_Unit] = []
    text: list[str] = []

    def flush_text() -> None:
        if text:
            units.append(_Unit("text", tuple(text)))
            text.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence is not None:
            flush_text()
            end = _find_fence_end(lines, i, fence.group(1))
            units.append(_Unit("code", tuple(lines[i : end + 1])))
            i = end + 1
            continue
        if i + 1 < len(lines) and _is_table_start(line, lines[i + 1]):
            flush_text()
            end = i + 2
            while end < len(lines) and lines[end].strip() and "|" in lines[end]:
                end += 1
            units.append(_Unit("table", tuple(lines[i:end])))
            i = end
            continue
        if line.strip():
            text.append(line)
        else:
            flush_text()
        i += 1
    flush_text()
    return units


def _find_fence_end(lines: list[str], start: int, opener: str) -> int:
    """Index of the line closing the fence opened at `start` (last line if unclosed)."""
    closer = re.compile(rf"^ {{0,3}}{re.escape(opener[0])}{{{len(opener)},}}\s*$")
    for j in range(start + 1, len(lines)):
        if closer.match(lines[j]):
            return j
    return len(lines) - 1


def _split_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return _UNESCAPED_PIPE_RE.split(stripped)


def _column_count(header: str) -> int:
    return len(_split_cells(header))


def _is_table_start(header: str, delimiter: str) -> bool:
    if header.startswith("    ") or not _UNESCAPED_PIPE_RE.search(header):
        return False
    if "|" not in delimiter or "-" not in delimiter:
        return False
    cells = _split_cells(delimiter)
    if not all(_DELIMITER_CELL_RE.match(cell) for cell in cells):
        return False
    return len(cells) == _column_count(header)
