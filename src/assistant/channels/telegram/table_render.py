"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Mobile-friendly rendering of GFM pipe tables for the legacy (entity-based) send path.

telegramify-markdown renders a table as a ``pre`` block sized to its longest cells, which
wraps badly on a phone. ``render_tables`` rewrites every GFM table outside fenced code
blocks into Markdown that fits a narrow screen, choosing a layout by display width:

- grid: the table fits ``TABLE_WIDTH_BUDGET`` cells; compact monospace grid in a code
  block, inline Markdown stripped from cells;
- lines: two columns; one ``**<col1>** — <col2>`` line per row;
- cards: three or more columns; one card per row (bold first cell, then
  ``<header>: <value>`` lines), cards separated by a blank line.

Lines and cards keep inline Markdown in cells, so the formatter still produces entities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from wcwidth import wcswidth, wcwidth

# Monospace cells that fit on one line of a phone screen in Telegram.
TABLE_WIDTH_BUDGET: Final[int] = 38

_FENCE_OPEN_RE: Final = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_DELIMITER_CELL_RE: Final = re.compile(r"^:?-+:?$")
_LIST_OR_QUOTE_RE: Final = re.compile(r"^(?:>|[-+*]\s|\d{1,9}[.)]\s)")
_UNESCAPED_PIPE_RE: Final = re.compile(r"(?<!\\)\|")
_CODE_SPAN_RE: Final = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
_IMAGE_RE: Final = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE: Final = re.compile(r"\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])")
_AUTOLINK_RE: Final = re.compile(r"<((?:https?|mailto|tg):[^>\s]+)>")
_EMPHASIS_RES: Final = (
    re.compile(r"\*\*(.+?)\*\*"),
    re.compile(r"(?<!\w)__(.+?)__(?!\w)"),
    re.compile(r"~~(.+?)~~"),
    re.compile(r"\*(?!\s)(.+?)(?<!\s)\*"),
    re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"),
)
_BACKSLASH_ESCAPE_RE: Final = re.compile(r"\\([!-/:-@\[-`{-~])")
_BOLD_MARKER_RE: Final = re.compile(r"\*\*|(?<!\w)__|__(?!\w)")
# Line starts that would turn a rendered line into a heading, quote, list or rule.
_BLOCK_START_RE: Final = re.compile(
    r"^(?:#{1,6}(?=\s|$)|>|[-+*](?=\s|$)|=+\s*$|-+\s*$|\*{3,}\s*$|\d{1,9}(?=[.)](?:\s|$)))"
)
_CODE_PLACEHOLDER: Final = "\x00{}\x00"


class _Align(Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


@dataclass(frozen=True)
class _Table:
    header: list[str]
    aligns: list[_Align]
    rows: list[list[str]]


def display_width(text: str) -> int:
    """Monospace cells ``text`` occupies: emoji and East Asian wide characters count 2,
    combining marks, zero-width joiners and variation selectors count 0."""
    width = wcswidth(text)
    if width >= 0:
        return width
    # wcswidth returns -1 on control characters; count those as zero width.
    return sum(max(wcwidth(char), 0) for char in text)


def render_tables(markdown: str) -> str:
    """Rewrite GFM pipe tables outside fenced code blocks into a mobile-friendly layout."""
    lines = markdown.split("\n")
    out: list[str] = []
    fence: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if fence is not None:
            out.append(line)
            if _closes_fence(line, fence):
                fence = None
            i += 1
            continue
        opened = _FENCE_OPEN_RE.match(line)
        if opened:
            fence = opened.group(1)
            out.append(line)
            i += 1
            continue
        table, end = _parse_table(lines, i)
        if table is None:
            out.append(line)
            i += 1
            continue
        block = _render_table(table)
        if out and out[-1].strip():
            out.append("")
        out.extend(block.split("\n"))
        if end < len(lines) and lines[end].strip():
            out.append("")
        i = end
    return "\n".join(out)


def _closes_fence(line: str, fence: str) -> bool:
    stripped = line.strip()
    if len(line) - len(line.lstrip(" ")) > 3 or not stripped:
        return False
    return set(stripped) == {fence[0]} and len(stripped) >= len(fence)


def _has_pipe(line: str) -> bool:
    if len(line) - len(line.lstrip(" ")) > 3:
        return False
    return _UNESCAPED_PIPE_RE.search(line) is not None


def _is_row_line(line: str) -> bool:
    """A header or body row: has a pipe and does not start another block."""
    stripped = line.strip()
    if _FENCE_OPEN_RE.match(stripped) or _LIST_OR_QUOTE_RE.match(stripped):
        return False
    return _has_pipe(line)


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in _UNESCAPED_PIPE_RE.split(stripped)]


def _parse_aligns(line: str) -> list[_Align] | None:
    cells = _split_row(line)
    aligns: list[_Align] = []
    for cell in cells:
        if not _DELIMITER_CELL_RE.match(cell):
            return None
        if cell.startswith(":") and cell.endswith(":"):
            aligns.append(_Align.CENTER)
        elif cell.endswith(":"):
            aligns.append(_Align.RIGHT)
        else:
            aligns.append(_Align.LEFT)
    return aligns


def _parse_table(lines: list[str], start: int) -> tuple[_Table | None, int]:
    """Parse a table whose header row is ``lines[start]``; return it and the end index."""
    if start + 1 >= len(lines):
        return None, start
    header_line, delimiter_line = lines[start], lines[start + 1]
    if not _is_row_line(header_line) or not _has_pipe(delimiter_line):
        return None, start
    aligns = _parse_aligns(delimiter_line)
    header = _split_row(header_line)
    if aligns is None or len(aligns) != len(header):
        return None, start
    rows: list[list[str]] = []
    end = start + 2
    while end < len(lines) and _is_row_line(lines[end]):
        cells = _split_row(lines[end])[: len(header)]
        rows.append(cells + [""] * (len(header) - len(cells)))
        end += 1
    return _Table(header=header, aligns=aligns, rows=rows), end


def _render_table(table: _Table) -> str:
    grid = _render_grid(table)
    if grid is not None:
        return grid
    if not table.rows:
        return " · ".join(_bold(cell) for cell in table.header if cell)
    if len(table.header) <= 2:
        return _render_lines(table)
    return _render_cards(table)


def _render_grid(table: _Table) -> str | None:
    """Compact monospace grid, or None if it does not fit ``TABLE_WIDTH_BUDGET``."""
    plain_rows = [
        [strip_inline_markdown(cell) for cell in row] for row in [table.header, *table.rows]
    ]
    widths = [
        max(display_width(row[col]) for row in plain_rows) for col in range(len(table.header))
    ]
    if sum(widths) + 3 * (len(widths) - 1) > TABLE_WIDTH_BUDGET:
        return None
    rendered = [
        " | ".join(
            _pad(cell, width, align)
            for cell, width, align in zip(row, widths, table.aligns, strict=True)
        ).rstrip()
        for row in plain_rows
    ]
    rendered.insert(1, "-+-".join("-" * width for width in widths))
    body = "\n".join(rendered)
    longest_backticks = max((len(run) for run in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest_backticks + 1)
    return f"{fence}\n{body}\n{fence}"


def _pad(cell: str, width: int, align: _Align) -> str:
    gap = width - display_width(cell)
    if align is _Align.RIGHT:
        return " " * gap + cell
    if align is _Align.CENTER:
        left = gap // 2
        return " " * left + cell + " " * (gap - left)
    return cell + " " * gap


def _render_lines(table: _Table) -> str:
    if len(table.header) == 1:
        title = [_bold(table.header[0])] if table.header[0] else []
        return "\n".join(title + [_guard_line_start(row[0]) for row in table.rows if row[0]])
    lines: list[str] = []
    for row in table.rows:
        key, value = row
        if key and value:
            lines.append(f"{_bold(key)} — {value}")
        elif key:
            lines.append(_bold(key))
        elif value:
            lines.append(_guard_line_start(value))
    return "\n".join(lines)


def _render_cards(table: _Table) -> str:
    cards: list[str] = []
    for row in table.rows:
        card: list[str] = []
        if row[0]:
            card.append(_bold(row[0]))
        for header, value in zip(table.header[1:], row[1:], strict=True):
            if not value:
                continue
            card.append(f"{header}: {value}" if header else value)
        if card:
            cards.append("\n".join(_guard_line_start(line) for line in card))
    return "\n\n".join(cards)


def _bold(cell: str) -> str:
    """Wrap a cell in bold, dropping bold markers already inside it."""
    inner = _BOLD_MARKER_RE.sub("", cell).strip()
    return f"**{inner}**" if inner else ""


def _guard_line_start(line: str) -> str:
    """Escape a leading character that would start a heading, quote, list or rule."""
    match = _BLOCK_START_RE.match(line)
    if match is None:
        return line
    if match.group(0).isdigit():
        end = match.end()
        return f"{line[:end]}\\{line[end:]}"
    return f"\\{line}"


def strip_inline_markdown(cell: str) -> str:
    """Plain text of a table cell: links become their text, emphasis and code markers go."""
    codes: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        codes.append(match.group(2).strip() or match.group(2))
        return _CODE_PLACEHOLDER.format(len(codes) - 1)

    text = _CODE_SPAN_RE.sub(_stash, cell)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _AUTOLINK_RE.sub(r"\1", text)
    for pattern in _EMPHASIS_RES:
        text = pattern.sub(r"\1", text)
    text = _BACKSLASH_ESCAPE_RE.sub(r"\1", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)
    return text.replace("\t", " ")
