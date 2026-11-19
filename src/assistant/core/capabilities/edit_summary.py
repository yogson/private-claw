"""
Component ID: CMP_CORE_CAPABILITIES

Human-readable summary of a capability manifest change (for the ``capability_edit``
confirmation prompt).

The summary is a generic structural diff of the parsed manifests (``model_dump`` of the
before and after definitions, so string shorthands such as ``command_allowlist: [git]``
show up with their effective defaults). Every changed leaf gets its own line with the
concrete before → after values; lists of entries are matched by their identity field
(``tool_id``, ``id``, ``command_pattern``, ``name``), lists of strings show the items
added (``+``) and removed (``−``) element by element.

Secrets: inside ``claude_code_settings.mcp_servers.<server>``, every value at or below an
``env`` / ``headers`` key or a secret-looking key (``token``, ``secret``, ``auth``, ...) is
masked as ``<hidden>``, whatever its type: dicts keep their key names, ``KEY=value``
list items keep ``KEY``, everything else is hidden. This also holds when the agent
changes the type of such a value (``env: {...}`` → ``"x"``) or replaces an enclosing
value. A change that only touches hidden values says ``(secret value changed)``.

Values on ``⚠`` lines are never cut. A changed string longer than ``VALUE_MAX_CHARS`` is
shown as a diff excerpt: a little of the unchanged context around the differing segment,
and the removed / added segments in full (``…"ctx"[−"old"][+"new"]"ctx"…``). A ⚠ value
(or differing segment, or key) longer than ``SENSITIVE_VALUE_MAX_CHARS`` cannot be shown
in full, so :class:`SensitiveValueTooLongError` is raised and the proposal is refused.
Non-sensitive values are cut with an explicit ``(+N chars hidden)`` marker.

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
# Changed strings up to this length are shown whole (before → after); longer ones as a
# diff excerpt. Non-sensitive values are cut at this length.
VALUE_MAX_CHARS = 120
# ⚠ values, differing segments and keys are shown in full up to this length; longer ones
# make the proposal fail (they would have to be cut, which could hide the change).
SENSITIVE_VALUE_MAX_CHARS = 400
# Unchanged chars shown on each side of the differing segment of a long changed string.
DIFF_CONTEXT_CHARS = 20
MASKED = "<hidden>"
_NON_SENSITIVE_FIELDS = frozenset({"prompt"})
_IDENTITY_FIELDS = ("tool_id", "id", "command_pattern", "name")
_MCP_SERVERS_PATH = ("claude_code_settings", "mcp_servers")
_SECRET_CONTAINERS = frozenset({"env", "headers"})
# ``KEY=value`` items of a hidden list keep ``KEY`` only when it looks like a variable name.
_ENV_ITEM_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]{0,79})=.*", re.S)
_SECRET_KEY_RE = re.compile(
    r"token|secret|passw|api[_-]?key|auth|credential|cookie|private[_-]?key", re.I
)
_PLAIN_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
# Control, format (bidi controls, zero-width), surrogate, private-use, unassigned, and
# line / paragraph separators.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# Characters that render blank but are letters, marks or symbols, so the categories above
# miss them: Hangul fillers, Braille blank, and the Default_Ignorable_Code_Point marks
# (combining grapheme joiner, Khmer / Mongolian invisibles, variation selectors, tags).
_INVISIBLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xE0000, 0xE0FFF),
)
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


class SensitiveValueTooLongError(Exception):
    """A security-sensitive value (or its changed segment) is too long to show in full."""

    def __init__(self, path: str, chars: int, limit: int = SENSITIVE_VALUE_MAX_CHARS) -> None:
        super().__init__(
            f"security-sensitive value at {path} needs {chars} chars to show in full "
            f"(limit {limit})"
        )
        self.path = path
        self.chars = chars
        self.limit = limit


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
        """The changed value itself is secret (see :func:`is_secret_path`)."""
        return is_secret_path(self.keys)


def is_secret_path(keys: tuple[str, ...]) -> bool:
    """True for ``claude_code_settings.mcp_servers.<server>.…`` paths that pass through an
    ``env`` / ``headers`` key or a secret-looking key (the server name itself excluded).

    ``keys[3:]`` includes the last key, so a change *at* ``env`` (e.g. a type change from
    a dict to a string) is secret too, not only the leaves below it.
    """
    if keys[:2] != _MCP_SERVERS_PATH:
        return False
    return any(k.lower() in _SECRET_CONTAINERS or _SECRET_KEY_RE.search(k) for k in keys[3:])


class _Hidden:
    """A masked secret value; ``key`` is the visible name of a ``KEY=value`` list item."""

    __slots__ = ("key",)

    def __init__(self, key: str | None = None) -> None:
        self.key = key


def _redact(value: Any, keys: tuple[str, ...], *, item: bool = False) -> Any:
    """``value`` (found at ``keys``; ``item``: an element of the list at ``keys``) with
    every secret part replaced by :class:`_Hidden`."""
    if is_secret_path(keys):
        return _hide_item(value) if item else _hide(value)
    depth = min(len(keys), len(_MCP_SERVERS_PATH))
    if keys[:depth] != _MCP_SERVERS_PATH[:depth]:
        return value  # not in (or above) the free-form MCP server settings
    if isinstance(value, dict):
        return {k: _redact(v, (*keys, str(k))) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, keys if item else (*keys, str(i))) for i, v in enumerate(value)]
    return value


def _hide(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _hide(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_hide_item(v) for v in value]
    return _Hidden()


def _hide_item(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_ITEM_RE.fullmatch(value)
        return _Hidden(match.group(1) if match else None)
    return _hide(value)


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
    """Leaves of an added subtree; empty default containers inside it are skipped, but a
    subtree with no other leaves is still reported as one ``added`` line."""
    leaves: list[_Change] = []
    _added_leaves(keys, path, value, leaves)
    out.extend(leaves or [_Change(keys, path, "added", new=value)])


def _added_leaves(keys: tuple[str, ...], path: str, value: Any, out: list[_Change]) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            _added_leaves((*keys, str(key)), _join(path, key), sub, out)
    elif isinstance(value, list):
        if value:
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
    """A path component; every key is on a ⚠ line, so a key that cannot be shown in full
    refuses the proposal."""
    text = str(key)
    if _PLAIN_KEY_RE.fullmatch(text) and len(text) <= SENSITIVE_VALUE_MAX_CHARS:
        return text
    rendered = f'"{escape(text)}"'
    if len(rendered) > SENSITIVE_VALUE_MAX_CHARS:
        raise SensitiveValueTooLongError(f"key {rendered[:40]}…", len(rendered))
    return rendered


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(change: _Change) -> str:
    prefix = _SENSITIVE_PREFIX if change.sensitive else _PLAIN_PREFIX
    strict = change.sensitive

    item = change.kind in ("item_added", "item_removed")
    old = _redact(change.old, change.keys, item=item)
    new = _redact(change.new, change.keys, item=item)

    def fmt(value: Any) -> str:
        return _value(value, change.path, strict=strict)

    if change.kind == "changed":
        if isinstance(old, str) and isinstance(new, str):
            body = _string_change(old, new, change.path, strict=strict)
        else:
            before, after = fmt(old), fmt(new)
            body = f"{before} → {after}"
            if before == after:  # only masked parts differ
                body += " (secret value changed)"
    elif change.kind in ("added", "item_added"):
        body = f"+ {fmt(new)}"
    elif change.kind == "item_removed":
        body = f"− {fmt(old)}"
    elif change.kind == "removed":
        is_container = isinstance(change.old, dict | list) and change.old
        body = "− removed" if is_container else f"− removed (was {fmt(old)})"
    elif change.kind == "new_entry":
        body = "+ new entry"
    else:
        body = "order changed"
    return f"{prefix}{change.path}: {body}"


def _string_change(old: str, new: str, path: str, *, strict: bool) -> str:
    """``"old" → "new"``, or for long values a diff excerpt that always shows the whole
    differing segment (a cut at a fixed length could make both sides look identical)."""
    escaped_old, escaped_new = escape(old), escape(new)
    if len(escaped_old) <= VALUE_MAX_CHARS and len(escaped_new) <= VALUE_MAX_CHARS:
        return f'"{escaped_old}" → "{escaped_new}"'
    prefix = _common_prefix_len(old, new)
    suffix = _common_prefix_len(old[prefix:][::-1], new[prefix:][::-1])
    old_mid, new_mid = old[prefix : len(old) - suffix], new[prefix : len(new) - suffix]
    head, tail = old[:prefix], old[len(old) - suffix :]
    parts: list[str] = []
    if head:
        ellipsis = "…" if len(head) > DIFF_CONTEXT_CHARS else ""
        parts.append(f'{ellipsis}"{escape(head[-DIFF_CONTEXT_CHARS:])}"')
    if old_mid:
        parts.append(f"[−{_segment(old_mid, path, strict=strict)}]")
    if new_mid:
        parts.append(f"[+{_segment(new_mid, path, strict=strict)}]")
    if tail:
        ellipsis = "…" if len(tail) > DIFF_CONTEXT_CHARS else ""
        parts.append(f'"{escape(tail[:DIFF_CONTEXT_CHARS])}"{ellipsis}')
    return f"edited ({len(old)} → {len(new)} chars): {''.join(parts)}"


def _common_prefix_len(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    index = 0
    while index < limit and a[index] == b[index]:
        index += 1
    return index


def _segment(text: str, path: str, *, strict: bool) -> str:
    return _checked(f'"{escape(text)}"', path, strict=strict)


def _checked(rendered: str, path: str, *, strict: bool) -> str:
    """``rendered`` in full; a ⚠ value that does not fit is refused, a plain one is cut."""
    if strict:
        if len(rendered) > SENSITIVE_VALUE_MAX_CHARS:
            raise SensitiveValueTooLongError(path, len(rendered))
        return rendered
    return clip(rendered, VALUE_MAX_CHARS)


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


def _value(value: Any, path: str, *, strict: bool) -> str:
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, int | float):
        return str(value)
    return _checked(_literal(value), path, strict=strict)


def _literal(value: Any) -> str:
    """One-line, unambiguous rendering of a JSON value (strings quoted and escaped, masked
    secrets as an unquoted ``<hidden>``)."""
    if isinstance(value, _Hidden):
        return MASKED if value.key is None else f'"{escape(value.key)}="{MASKED}'
    if isinstance(value, str):
        return f'"{escape(value)}"'
    if isinstance(value, dict):
        items = (f"{_literal(str(k))}: {_literal(v)}" for k, v in sorted(value.items()))
        return "{" + ", ".join(items) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    return escape(json.dumps(value))


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
        (f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}") if _unsafe(c) else c
        for c in text
    )


def _unsafe(char: str) -> bool:
    """Control / format / separator chars, spaces other than U+0020, and blank-rendering
    letters or marks (a lookalike blank could hide or fake part of a value)."""
    category = unicodedata.category(char)
    if category in _UNSAFE_CATEGORIES or (category == "Zs" and char != " "):
        return True
    code = ord(char)
    return any(low <= code <= high for low, high in _INVISIBLE_RANGES)


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
