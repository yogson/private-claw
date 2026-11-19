"""
Component ID: CMP_CORE_CAPABILITIES

Human-readable summary of a capability manifest change (for the ``capability_edit``
confirmation prompt).

The summary is a generic structural diff of the parsed manifests (``model_dump`` of the
before and after definitions, so string shorthands such as ``command_allowlist: [git]``
show up with their effective defaults). Every changed leaf gets its own line with the
concrete before → after values; lists of entries are matched by their identity field
(``tool_id``, ``id``, ``command_pattern``, ``name``), lists of strings show the items
added (``+``) and removed (``−``). Long values are cut with an explicit
``(+N chars hidden)`` marker. Values under MCP ``env``/``headers`` and secret-looking keys
are masked: only the key names are shown.

Every top-level field except ``prompt`` is security-sensitive (tools, overrides, MCP
servers, nested capabilities, Claude Code settings, and any field added later): its lines
are marked ``⚠`` and listed first. ``prompt`` shows size statistics and an excerpt of
the added text instead of a full diff.

The result fits ``max_units`` UTF-16 code units (Telegram counts message length in
UTF-16). When it does not, non-sensitive lines are dropped with an explicit
``(+N more changes not shown)`` line; if even the ⚠ lines do not fit,
:class:`SummaryTooLargeError` is raised and the proposal must be split. Nothing is ever
filtered silently: "No manifest fields change" appears only when the parsed manifests
are identical.
"""

import difflib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

from assistant.core.capabilities.schemas import CapabilityDefinition

SENSITIVE_WARNING = (
    "⚠ Security-sensitive changes (tools / overrides / MCP / permissions): check every ⚠ line"
)
NO_CHANGES_TEXT = "No manifest fields change (comments or formatting only)."
REASON_MAX_CHARS = 300
EXCERPT_MAX_CHARS = 300
VALUE_MAX_CHARS = 120
MASKED = "<hidden>"
_NON_SENSITIVE_FIELDS = frozenset({"prompt"})
_IDENTITY_FIELDS = ("tool_id", "id", "command_pattern", "name")
_SECRET_CONTAINERS = frozenset({"env", "headers"})
_SECRET_KEY_RE = re.compile(
    r"token|secret|passw|api[_-]?key|auth|credential|cookie|private[_-]?key", re.I
)
_PLAIN_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_KEY_MAX_CHARS = 80
# Control, format (bidi controls, zero-width), surrogate, private-use, unassigned, and
# line / paragraph separators.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_SENSITIVE_PREFIX = "  ⚠ "
_PLAIN_PREFIX = "  • "


class SummaryTooLargeError(Exception):
    """The security-sensitive changes do not all fit one confirmation message."""

    def __init__(self, sensitive_changes: int, max_units: int) -> None:
        super().__init__(
            f"{sensitive_changes} security-sensitive changes do not fit one confirmation "
            f"({max_units} UTF-16 units)"
        )
        self.sensitive_changes = sensitive_changes


@dataclass(frozen=True)
class _Change:
    keys: tuple[str, ...]  # raw key names (list entries: their identity or index)
    path: str  # rendered path
    kind: str  # changed | added | removed | item_added | item_removed | reordered | new_entry
    old: Any = None
    new: Any = None

    @property
    def sensitive(self) -> bool:
        return self.keys[0] not in _NON_SENSITIVE_FIELDS

    @property
    def masked(self) -> bool:
        """Values inside a Claude Code MCP server's env/headers or under secret-like keys."""
        if self.keys[:2] != ("claude_code_settings", "mcp_servers") or len(self.keys) < 4:
            return False
        return any(k in _SECRET_CONTAINERS for k in self.keys[3:-1]) or bool(
            _SECRET_KEY_RE.search(self.keys[-1])
        )


def capability_change_summary(
    *,
    capability_id: str,
    file_name: str,
    reason: str,
    before: CapabilityDefinition,
    after: CapabilityDefinition,
    headline: str = "Capability edit",
    max_units: int = 3500,
) -> str:
    """Render the approver-facing summary of ``before`` → ``after``.

    Raises :class:`SummaryTooLargeError` when the ⚠ lines alone exceed ``max_units``.
    """
    old, new = _dump(before), _dump(after)
    changes = diff_manifests(old, new)
    head = [f"{headline}: {inline(capability_id)} ({inline(file_name)})"]
    if reason.strip():
        head.append(f"Reason: {clip(inline(reason), REASON_MAX_CHARS)}")
    if old == new:
        return fit_utf16("\n".join([*head, NO_CHANGES_TEXT]), max_units)

    sensitive = [_render(c) for c in changes if c.sensitive]
    plain: list[str] = []
    for change in changes:
        if change.sensitive:
            continue
        if change.keys == ("prompt",) and change.kind == "changed":
            plain.extend(_prompt_lines(str(change.old), str(change.new)))
        else:
            plain.append(_render(change))
    if sensitive:
        head.append(SENSITIVE_WARNING)
    head.append("Changed fields:")
    return _fit_lines(head, sensitive, plain, max_units)


def diff_manifests(old: dict[str, Any], new: dict[str, Any]) -> list[_Change]:
    """Leaf-level changes between two manifest dumps, in document order."""
    out: list[_Change] = []
    _diff((), "", old, new, out)
    return out


def _dump(definition: CapabilityDefinition) -> dict[str, Any]:
    return definition.model_dump(mode="json", exclude_none=True)


# ---------------------------------------------------------------------------
# Structural diff
# ---------------------------------------------------------------------------


def _diff(keys: tuple[str, ...], path: str, old: Any, new: Any, out: list[_Change]) -> None:
    if old == new:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in [*old, *(k for k in new if k not in old)]:
            sub_keys, sub_path = (*keys, str(key)), _join(path, key)
            if key not in new:
                out.append(_Change(sub_keys, sub_path, "removed", old=old[key]))
            elif key not in old:
                _added(sub_keys, sub_path, new[key], out)
            else:
                _diff(sub_keys, sub_path, old[key], new[key], out)
        return
    if isinstance(old, list) and isinstance(new, list):
        _diff_lists(keys, path, old, new, out)
        return
    out.append(_Change(keys, path, "changed", old=old, new=new))


def _diff_lists(
    keys: tuple[str, ...], path: str, old: list[Any], new: list[Any], out: list[_Change]
) -> None:
    if all(not isinstance(x, dict | list) for x in (*old, *new)):
        added = Counter(map(_hashable, new)) - Counter(map(_hashable, old))
        removed = Counter(map(_hashable, old)) - Counter(map(_hashable, new))
        for item in _ordered(new, added):
            out.append(_Change(keys, path, "item_added", new=item))
        for item in _ordered(old, removed):
            out.append(_Change(keys, path, "item_removed", old=item))
        if not added and not removed:
            out.append(_Change(keys, path, "reordered"))
        return
    identity = _identity_field(old, new)
    if identity is None:
        for index in range(max(len(old), len(new))):
            sub_keys, sub_path = (*keys, str(index)), f"{path}[{index}]"
            if index >= len(new):
                out.append(_Change(sub_keys, sub_path, "removed", old=old[index]))
            elif index >= len(old):
                _added(sub_keys, sub_path, new[index], out)
            else:
                _diff(sub_keys, sub_path, old[index], new[index], out)
        return
    old_by = {item[identity]: item for item in old}
    new_by = {item[identity]: item for item in new}
    for ident in [*old_by, *(i for i in new_by if i not in old_by)]:
        sub_keys, sub_path = (*keys, str(ident)), f"{path}[{_key(ident)}]"
        if ident not in new_by:
            out.append(_Change(sub_keys, sub_path, "removed", old=old_by[ident]))
        elif ident not in old_by:
            entry = {k: v for k, v in new_by[ident].items() if k != identity}
            if entry:
                _added(sub_keys, sub_path, entry, out)
            else:
                out.append(_Change(sub_keys, sub_path, "new_entry"))
        else:
            _diff(sub_keys, sub_path, old_by[ident], new_by[ident], out)
    common_old = [i for i in old_by if i in new_by]
    common_new = [i for i in new_by if i in old_by]
    if common_old != common_new:
        out.append(_Change(keys, path, "reordered"))


def _added(keys: tuple[str, ...], path: str, value: Any, out: list[_Change]) -> None:
    if isinstance(value, dict) and value:
        for key, sub in value.items():
            _added((*keys, str(key)), _join(path, key), sub, out)
    elif isinstance(value, list) and value:
        _diff_lists(keys, path, [], value, out)
    else:
        out.append(_Change(keys, path, "added", new=value))


def _identity_field(old: list[Any], new: list[Any]) -> str | None:
    for items in (old, new):
        if not all(isinstance(x, dict) for x in items):
            return None
    for field in _IDENTITY_FIELDS:
        if all(_unique(items, field) for items in (old, new)):
            return field
    return None


def _unique(items: list[dict[str, Any]], field: str) -> bool:
    values = [item.get(field) for item in items]
    return all(isinstance(v, str | int) for v in values) and len(set(values)) == len(values)


def _hashable(value: Any) -> Any:
    return json.dumps(value, sort_keys=True)


def _ordered(items: list[Any], counts: Counter[Any]) -> list[Any]:
    remaining = Counter(counts)
    result = []
    for item in items:
        key = _hashable(item)
        if remaining[key] > 0:
            remaining[key] -= 1
            result.append(item)
    return result


def _join(path: str, key: Any) -> str:
    rendered = _key(key)
    if not path:
        return rendered
    return f"{path}.{rendered}" if _PLAIN_KEY_RE.fullmatch(str(key)) else f"{path}[{rendered}]"


def _key(key: Any) -> str:
    text = str(key)
    return text if _PLAIN_KEY_RE.fullmatch(text) else quote(text, _KEY_MAX_CHARS)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(change: _Change) -> str:
    prefix = _SENSITIVE_PREFIX if change.sensitive else _PLAIN_PREFIX
    fmt = _masked if change.masked else _value
    if change.kind == "changed":
        body = f"{fmt(change.old)} → {fmt(change.new)}"
    elif change.kind in ("added", "item_added"):
        body = f"+ {fmt(change.new)}"
    elif change.kind == "item_removed":
        body = f"− {fmt(change.old)}"
    elif change.kind == "removed":
        is_container = isinstance(change.old, dict | list) and change.old
        body = "− removed" if is_container else f"− removed (was {fmt(change.old)})"
    elif change.kind == "new_entry":
        body = "+ new entry"
    else:
        body = "order changed"
    return f"{prefix}{change.path}: {body}"


def _masked(_value: Any) -> str:
    return MASKED


def _prompt_lines(old: str, new: str) -> list[str]:
    added_lines = removed_lines = 0
    added_text: list[str] = []
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added_lines += 1
            added_text.append(line[2:].strip())
        elif line.startswith("- "):
            removed_lines += 1
    result = [
        f"{_PLAIN_PREFIX}prompt: {len(old)} → {len(new)} chars "
        f"(+{added_lines} / −{removed_lines} lines)"
    ]
    excerpt = inline(" ".join(part for part in added_text if part))
    if excerpt:
        if len(excerpt) <= EXCERPT_MAX_CHARS:
            result.append(f'    excerpt of the added text: "{excerpt}"')
        else:
            result.append(
                f"    excerpt (first {EXCERPT_MAX_CHARS} of {len(excerpt)} added chars): "
                f'"{excerpt[:EXCERPT_MAX_CHARS]}…"'
            )
    return result


def _value(value: Any) -> str:
    if isinstance(value, str):
        return quote(value, VALUE_MAX_CHARS)
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict | list) and not value:
        return json.dumps(value)
    return clip(inline(json.dumps(value, ensure_ascii=False, sort_keys=True)), VALUE_MAX_CHARS)


def quote(text: str, limit: int) -> str:
    """``text`` as a double-quoted single-line literal, cut at ``limit`` chars."""
    escaped = escape(text)
    if len(escaped) <= limit:
        return f'"{escaped}"'
    return f'"{escaped[:limit]}…" (+{len(escaped) - limit} chars hidden)'


def clip(text: str, limit: int) -> str:
    """Cut ``text`` at ``limit`` chars with an explicit count of what was cut."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} chars hidden)"


def escape(text: str) -> str:
    """Escape quotes, backslashes, control and format characters (one line, unambiguous).

    Manifest values are agent-controlled: a newline, U+2028 or a bidi override inside a
    list item or key could otherwise forge extra summary lines or reorder what is shown.
    """
    return _escape_unsafe(json.dumps(text, ensure_ascii=False)[1:-1])


def inline(text: str) -> str:
    """One line of free text: whitespace runs (newlines included) collapse to one space,
    remaining control / format (bidi, zero-width) characters are escaped as ``\\uXXXX``."""
    return _escape_unsafe(" ".join(text.split()))


def _escape_unsafe(text: str) -> str:
    return "".join(
        (f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
        if unicodedata.category(c) in _UNSAFE_CATEGORIES
        else c
        for c in text
    )


def _fit_lines(head: list[str], sensitive: list[str], plain: list[str], max_units: int) -> str:
    text = "\n".join([*head, *sensitive, *plain])
    if utf16_len(text) <= max_units:
        return text
    lines = [*head, *sensitive]
    for index, line in enumerate(plain):
        note = _more_note(len(plain) - index - 1)
        if utf16_len("\n".join([*lines, line, note])) > max_units:
            break
        lines.append(line)
    hidden = len(plain) - (len(lines) - len(head) - len(sensitive))
    lines.append(_more_note(hidden))
    text = "\n".join(lines)
    if utf16_len(text) > max_units:
        raise SummaryTooLargeError(len(sensitive), max_units)
    return text


def _more_note(hidden: int) -> str:
    return f"(+{hidden} more change{'' if hidden == 1 else 's'} not shown)"


def utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units (how Telegram measures messages)."""
    return len(text.encode("utf-16-le")) // 2


_TRUNCATED_NOTE = "\n… [summary truncated]"


def fit_utf16(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-16 units, marking the cut explicitly."""
    if utf16_len(text) <= limit:
        return text
    budget = limit - utf16_len(_TRUNCATED_NOTE)
    used = cut = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > budget:
            break
        used += width
        cut += 1
    return text[:cut] + _TRUNCATED_NOTE
