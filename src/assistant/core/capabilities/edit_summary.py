"""
Component ID: CMP_CORE_CAPABILITIES

Human-readable summary of a capability manifest change (for the ``capability_edit``
confirmation prompt).

The summary is a structural diff of the parsed manifests (``model_dump`` of the before
and after definitions in Python mode, so string shorthands such as
``command_allowlist: [git]`` show up with their effective defaults, and free-form MCP
server settings keep their real Python types).

The rendering of security-sensitive (``⚠``) changes is deliberately simple; its
invariants are property-tested:

1. Change detection is type-strict. Two values are equal only when their
   :func:`canonical` renderings are equal: ``true`` vs ``1``, ``1`` vs ``1.0``, a string
   vs bytes, a date vs a string, a set vs a list, ``.nan`` are all changes. Values that
   are not JSON-native get a tagged rendering (``!!binary <hex>``, ``!!timestamp …``,
   ``!!set [...]``, ``!!float .nan``), never ``null`` or a plain string.
2. Every changed ⚠ leaf or list is shown as its FULL canonical value before → after, on
   one line, ASCII-only (``json.dumps(..., ensure_ascii=True)`` for strings, so a
   Cyrillic lookalike shows as ``\\uXXXX``). Lists keep their order and are shown whole:
   there is no added / removed item view. ``[]``, ``{}`` and ``null`` are shown
   explicitly; a key present on one side only is shown as ``(absent)`` on the other side,
   with the full value (every field of an added block, empty ones included). Lists of
   entries with a unique identity (``tool_id``, ``id``, ``command_pattern``, ``name``)
   are matched by it; each changed field of an entry is a leaf as above, and a change of
   the entries' order (other than appending) shows both full identity sequences. Where
   the value does not mean what it seems, a fixed note follows it: an empty delegation
   allowlist (no restriction), and a removed tool override (inherited, not cleared).
3. Nothing on a ⚠ line is ever cut. When the ⚠ lines do not all fit ``max_units``,
   :class:`SummaryTooLargeError` is raised and the proposal is refused.

Secrets: inside ``claude_code_settings.mcp_servers.<server>``, every value at or below an
``env`` / ``headers`` key or a secret-looking key (``token``, ``secret``, ``auth``, ...) is
masked as ``<hidden>``, whatever its type: dicts keep their key names, ``KEY=value``
list items keep ``KEY`` when it is an upper-case variable name (split at the first
``=``), everything else is hidden. A change that only touches hidden values says
``(secret value changed)``; the approver cannot verify such a value.

Every top-level field except ``prompt`` is security-sensitive (tools, overrides, MCP
servers, nested capabilities, Claude Code settings, and any field added later): its lines
are marked ``⚠`` and listed first. A ``prompt`` change is shown in FULL, but not in the
summary: :func:`summarize_change` renders it as a unified line diff
(:mod:`assistant.core.capabilities.prompt_diff`) split into message-sized parts that the
channel delivers before the buttons; the summary only has a short, never-dropped
``prompt: changed (+A/−R lines, see N attached parts)`` line. A prompt change that needs
more than ``max_prompt_parts`` parts is refused (:class:`PromptChangeTooLargeError`),
never cut. Other non-sensitive lines (none today) are dropped with an explicit
``(+N more changes not shown)`` line when space runs out. "No manifest fields change"
appears only when the canonical forms of the parsed manifests are identical.

The reason is agent-written free text on one line (:func:`reason_text`): besides the
:func:`inline` escaping, non-ASCII symbols (``⚠``, ``✅``, ``❌``, ``→``, ``•``, emoji)
are escaped and ``[[`` is broken up, so nothing on the Reason line can look like a
summary line, a status line or the ``[[CONFIRMATION_RESOLVED]]`` history marker.
"""

import datetime as dt
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from assistant.core.capabilities.prompt_diff import prompt_diff, split_parts
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.text_safety import escape_char, escape_unsafe, utf16_len

SENSITIVE_WARNING = (
    "⚠ Security-sensitive changes (tools / overrides / MCP / permissions): every ⚠ line "
    "shows the full value before → after (non-ASCII escaped as \\uXXXX); check each one"
)
NO_CHANGES_TEXT = "No manifest fields change (comments or formatting only)."
ABSENT = "(absent)"
MASKED = "<hidden>"
SECRET_CHANGED_NOTE = " (secret value changed)"
UNRESTRICTED_NOTE = " [note: an empty delegation allowlist means NO restriction]"
# A removed tool override field is inherited, not cleared (agent.tools.registry merges
# tools.yaml defaults with the overrides of every enabled capability).
INHERITED_NOTE = (
    " [note: a removed tool override is inherited, not cleared: another override of this "
    "tool or tools.yaml applies"
)
# What a removed field means when nothing else sets it, where that is wider than expected.
UNSET_MEANS = {
    "delegation_default_permission_mode": "the built-in auto, the second-widest mode",
    "delegation_allowed_backends": "NO restriction",
    "delegation_model_allowlist": "NO restriction",
    "delegation_per_task_token_cap": "no cap",
    "delegation_per_session_token_cap": "no cap",
    "delegation_global_token_cap": "no cap",
    "delegation_max_concurrent_tasks": "no task-count limit",
}
REASON_MAX_CHARS = 300
# Body of one prompt-change part (UTF-16 units). The channel adds a header of at most
# ~300 units (title + "part i/N (confirmation …)"), and a part must fit one Telegram
# message (4096); ConfirmationAttachment enforces the same bound.
PROMPT_PART_MAX_UNITS = 3700
DEFAULT_MAX_PROMPT_PARTS = 8
PROMPT_TITLE_MAX_CHARS = 40  # of the capability id: the title stays within 120 UTF-16 units
# Non-sensitive values (anything but ``prompt``, which has its own lines) are cut here.
PLAIN_VALUE_MAX_CHARS = 120
_NON_SENSITIVE_FIELDS = frozenset({"prompt"})
_IDENTITY_FIELDS = ("tool_id", "id", "command_pattern", "name")
_MCP_SERVERS_PATH = ("claude_code_settings", "mcp_servers")
_SECRET_CONTAINERS = frozenset({"env", "headers"})
_SECRET_KEY_RE = re.compile(
    r"token|secret|passw|api[_-]?key|auth|credential|cookie|private[_-]?key", re.I
)
# Delegation allowlists where ``[]`` disables the restriction (subagents/coordinator.py).
_EMPTY_IS_UNRESTRICTED = frozenset({"delegation_allowed_backends", "delegation_model_allowlist"})
# ``KEY=value`` items of a hidden list keep ``KEY`` only when it looks like an env
# variable name and something other than base64 padding follows the first ``=``.
_ENV_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,79}")
# Keys / identities shown without quotes; anything else is shown as its canonical form.
_PLAIN_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
SENSITIVE_PREFIX = "  ⚠ "
_PLAIN_PREFIX = "  • "


class SummaryTooLargeError(Exception):
    """The security-sensitive changes, shown in full, do not fit one confirmation."""

    def __init__(self, sensitive_changes: int, needed_units: int, max_units: int) -> None:
        super().__init__(
            f"{sensitive_changes} security-sensitive changes need {needed_units} UTF-16 "
            f"units to show in full (limit {max_units})"
        )
        self.sensitive_changes = sensitive_changes
        self.needed_units = needed_units
        self.max_units = max_units


class PromptChangeTooLargeError(Exception):
    """The full prompt change needs more parts than one confirmation may carry."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class ChangeSummary:
    """Approver-facing rendering of a change: the summary text and, when the prompt
    changes, the full prompt diff as message-sized parts (in delivery order)."""

    text: str
    prompt_parts: tuple[str, ...] = ()
    prompt_title: str = ""


class _AbsentType:
    """The value of a key or entry that exists on one side of the change only."""

    __slots__ = ()


_ABSENT = _AbsentType()


@dataclass(frozen=True)
class _Change:
    keys: tuple[str, ...]  # raw key names (list entries: their identity)
    path: str  # rendered path
    old: Any = _ABSENT
    new: Any = _ABSENT
    order: str | None = None  # identity field, when the entries' order changed

    @property
    def sensitive(self) -> bool:
        return self.keys[0] not in _NON_SENSITIVE_FIELDS


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


def _redact(value: Any, keys: tuple[str, ...]) -> Any:
    """``value`` (found at ``keys``) with every secret part replaced by :class:`_Hidden`."""
    if value is _ABSENT:
        return value
    if is_secret_path(keys):
        return _hide(value)
    depth = min(len(keys), len(_MCP_SERVERS_PATH))
    if keys[:depth] != _MCP_SERVERS_PATH[:depth]:
        return value  # not in (or above) the free-form MCP server settings
    if isinstance(value, dict):
        return {k: _redact(v, (*keys, str(k))) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, (*keys, str(i))) for i, v in enumerate(value)]
    return value


def _hide(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _hide(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_hide_item(v) for v in value]
    return _Hidden()


def _hide_item(value: Any) -> Any:
    if isinstance(value, str):
        return _Hidden(env_item_key(value))
    return _hide(value)


def env_item_key(item: str) -> str | None:
    """The visible ``KEY`` of a ``KEY=value`` list item, or None to hide the item whole.

    The item is split at the first ``=``. A padded base64 secret (``dGhpc2lz==``) has
    nothing but ``=`` after its first ``=``, and its "key" is not an upper-case variable
    name, so no part of it is shown.
    """
    key, sep, rest = item.partition("=")
    if not sep or not rest or rest.startswith("=") or not _ENV_KEY_RE.fullmatch(key):
        return None
    return key


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


def canonical(value: Any) -> str:
    """Type-strict, one-line, ASCII-only rendering of a parsed manifest value.

    Two values have the same canonical form only when they have the same types and
    values all the way down (``true`` ≠ ``1`` ≠ ``1.0``, ``"a"`` ≠ ``!!binary 61``,
    ``[a, b]`` ≠ ``[b, a]``; dict keys and set items are sorted). Strings are
    ``json.dumps(..., ensure_ascii=True)``, so every non-ASCII character is ``\\uXXXX``.
    Masked secrets render as an unquoted ``<hidden>``, which no other value produces.
    """
    if isinstance(value, _Hidden):
        return MASKED if value.key is None else f"{_json_str(value.key + '=')}{MASKED}"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "!!float .nan"
        if math.isinf(value):
            return "!!float .inf" if value > 0 else "!!float -.inf"
        return repr(value)
    if isinstance(value, str):
        return _json_str(value)
    if isinstance(value, bytes):
        return f"!!binary {value.hex() or '(empty)'}"
    if isinstance(value, dt.date):  # datetime is a date subclass
        return f"!!timestamp {_json_str(value.isoformat())}"
    if isinstance(value, dict):
        items = sorted(f"{canonical(k)}: {canonical(v)}" for k, v in value.items())
        return "{" + ", ".join(items) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(canonical(v) for v in value) + "]"
    if isinstance(value, set | frozenset):
        return "!!set [" + ", ".join(sorted(canonical(v) for v in value)) + "]"
    if isinstance(value, tuple):
        return "!!tuple [" + ", ".join(canonical(v) for v in value) + "]"
    return f"!!{type(value).__name__} {_json_str(repr(value))}"


def _json_str(text: str) -> str:
    return json.dumps(text, ensure_ascii=True)


def _same(old: Any, new: Any) -> bool:
    return canonical(old) == canonical(new)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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
    """The summary text of :func:`capability_change` (without the prompt parts)."""
    return capability_change(
        capability_id=capability_id,
        file_name=file_name,
        reason=reason,
        before=before,
        after=after,
        headline=headline,
        max_units=max_units,
    ).text


def capability_change(
    *,
    capability_id: str,
    file_name: str,
    reason: str,
    before: CapabilityDefinition,
    after: CapabilityDefinition,
    headline: str = "Capability edit",
    max_units: int = 3500,
    max_prompt_parts: int = DEFAULT_MAX_PROMPT_PARTS,
    prompt_part_units: int = PROMPT_PART_MAX_UNITS,
) -> ChangeSummary:
    """Render the approver-facing summary of ``before`` → ``after`` and the full prompt
    change parts.

    Raises :class:`SummaryTooLargeError` when the ⚠ lines alone exceed ``max_units``, and
    :class:`PromptChangeTooLargeError` when the prompt change does not fit
    ``max_prompt_parts`` parts.
    """
    return summarize_change(
        capability_id=capability_id,
        file_name=file_name,
        reason=reason,
        old=dump_manifest(before),
        new=dump_manifest(after),
        headline=headline,
        max_units=max_units,
        max_prompt_parts=max_prompt_parts,
        prompt_part_units=prompt_part_units,
    )


def dump_manifest(definition: CapabilityDefinition) -> dict[str, Any]:
    """Effective values of a manifest, with the real Python types of free-form values."""
    return definition.model_dump(exclude_none=True)


def summarize_dumps(
    *,
    capability_id: str,
    file_name: str,
    reason: str,
    old: dict[str, Any],
    new: dict[str, Any],
    headline: str = "Capability edit",
    max_units: int = 3500,
) -> str:
    """The summary text of :func:`summarize_change` on two manifest dumps."""
    return summarize_change(
        capability_id=capability_id,
        file_name=file_name,
        reason=reason,
        old=old,
        new=new,
        headline=headline,
        max_units=max_units,
    ).text


def summarize_change(
    *,
    capability_id: str,
    file_name: str,
    reason: str,
    old: dict[str, Any],
    new: dict[str, Any],
    headline: str = "Capability edit",
    max_units: int = 3500,
    max_prompt_parts: int = DEFAULT_MAX_PROMPT_PARTS,
    prompt_part_units: int = PROMPT_PART_MAX_UNITS,
) -> ChangeSummary:
    """:func:`capability_change` on two manifest dumps."""
    head = [f"{headline}: {inline(capability_id)} ({inline(file_name)})"]
    if reason.strip():
        head.append(f"Reason: {reason_text(reason)}")
    changes = diff_manifests(old, new)
    if not changes:
        return ChangeSummary(fit_utf16("\n".join([*head, NO_CHANGES_TEXT]), max_units))

    sensitive = [render_change(c) for c in changes if c.sensitive]
    required = list(sensitive)
    plain: list[str] = []
    prompt_parts: tuple[str, ...] = ()
    for change in changes:
        if change.sensitive:
            continue
        texts = _prompt_texts(change)
        if texts is None:
            plain.append(render_change(change))
            continue
        line, prompt_parts = _prompt_change(*texts, max_prompt_parts, prompt_part_units)
        required.append(line)  # never dropped: the parts are useless without it
    if sensitive:
        head.append(SENSITIVE_WARNING)
    head.append("Changed fields:")
    text = _fit_lines(head, required, plain, max_units, sensitive_changes=len(sensitive))
    title = prompt_title(capability_id) if prompt_parts else ""
    return ChangeSummary(text, prompt_parts, title)


def prompt_title(capability_id: str) -> str:
    """Title of the prompt-change parts (the channel adds ``part i/N`` and the id)."""
    return f"Prompt change for {clip(inline(capability_id), PROMPT_TITLE_MAX_CHARS)}"


def diff_manifests(old: dict[str, Any], new: dict[str, Any]) -> list[_Change]:
    """Changes between two manifest dumps, in document order (empty: no change)."""
    out: list[_Change] = []
    _diff((), "", old, new, out)
    return out


# ---------------------------------------------------------------------------
# Structural diff
# ---------------------------------------------------------------------------


def _diff(keys: tuple[str, ...], path: str, old: Any, new: Any, out: list[_Change]) -> None:
    if _same(old, new):
        return
    if isinstance(old, dict) and isinstance(new, dict):
        old_by, new_by = _by_canonical_key(old), _by_canonical_key(new)
        if old_by is not None and new_by is not None:
            for ck in [*old_by, *(k for k in new_by if k not in old_by)]:
                key = (old_by.get(ck) or new_by[ck])[0]
                sub_keys, sub_path = (*keys, str(key)), _join(path, key)
                old_value = old_by[ck][1] if ck in old_by else _ABSENT
                new_value = new_by[ck][1] if ck in new_by else _ABSENT
                if old_value is _ABSENT or new_value is _ABSENT:
                    out.append(_Change(sub_keys, sub_path, old_value, new_value))
                else:
                    _diff(sub_keys, sub_path, old_value, new_value, out)
            return
    if isinstance(old, list) and isinstance(new, list) and not is_secret_path(keys):
        identity = _identity_field(old, new)
        if identity is not None:
            _diff_entries(keys, path, identity, old, new, out)
            return
    out.append(_Change(keys, path, old, new))


def _by_canonical_key(value: dict[Any, Any]) -> dict[str, tuple[Any, Any]] | None:
    """``value`` keyed by the canonical form of each key (type-strict key matching:
    ``True`` and ``1`` are different keys); None if two keys collide."""
    result = {canonical(k): (k, v) for k, v in value.items()}
    return result if len(result) == len(value) else None


def _diff_entries(
    keys: tuple[str, ...],
    path: str,
    identity: str,
    old: list[dict[str, Any]],
    new: list[dict[str, Any]],
    out: list[_Change],
) -> None:
    old_by = {item[identity]: item for item in old}
    new_by = {item[identity]: item for item in new}
    for ident in [*old_by, *(i for i in new_by if i not in old_by)]:
        sub_keys, sub_path = (*keys, ident), f"{path}[{_ident(ident)}]"
        if ident not in new_by:
            out.append(_Change(sub_keys, sub_path, old=old_by[ident]))
        elif ident not in old_by:
            out.append(_Change(sub_keys, sub_path, new=new_by[ident]))
        else:
            _diff(sub_keys, sub_path, old_by[ident], new_by[ident], out)
    old_ids, new_ids = list(old_by), list(new_by)
    # Removing entries and appending new ones keep the order; anything else changes it.
    appended = [i for i in new_ids if i not in old_by]
    if new_ids != [i for i in old_ids if i in new_by] + appended:
        out.append(_Change(keys, path, old_ids, new_ids, order=identity))


def _identity_field(old: list[Any], new: list[Any]) -> str | None:
    """A field that identifies every entry of both lists (all dicts, unique str values)."""
    if not all(isinstance(x, dict) for x in (*old, *new)):
        return None
    for field in _IDENTITY_FIELDS:
        if all(_unique(items, field) for items in (old, new)):
            return field
    return None


def _unique(items: list[dict[str, Any]], field: str) -> bool:
    values = [item.get(field) for item in items]
    return all(type(v) is str for v in values) and len(set(values)) == len(values)


def _join(path: str, key: Any) -> str:
    if isinstance(key, str) and _PLAIN_KEY_RE.fullmatch(key):
        return f"{path}.{key}" if path else key
    return f"{path}[{canonical(key)}]"


def _ident(ident: str) -> str:
    return ident if _PLAIN_KEY_RE.fullmatch(ident) else canonical(ident)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_change(change: _Change) -> str:
    """One summary line: ``<path>: <full old> → <full new>``."""
    if not change.sensitive:
        return _plain_line(change)
    if change.order is not None:
        return (
            f"{SENSITIVE_PREFIX}{change.path} (order of {change.order}): "
            f"{canonical(change.old)} → {canonical(change.new)}"
        )
    before = _render_value(_redact(change.old, change.keys))
    after = _render_value(_redact(change.new, change.keys))
    line = f"{SENSITIVE_PREFIX}{change.path}: {before} → {after}"
    if before == after:  # only masked parts differ
        line += SECRET_CHANGED_NOTE
    if _sets_unrestricted(change.keys, change.new):
        line += UNRESTRICTED_NOTE
    if change.new is _ABSENT:
        line += inherited_note(change.keys, change.old)
    return line


def inherited_note(keys: tuple[str, ...], old: Any) -> str:
    """The note for removing tool override fields (at ``keys``, removed value ``old``):
    they fall back to other overrides or tools.yaml; ``""`` if ``keys`` is not one."""
    fields = sorted(_override_fields(keys, old))
    if not fields:
        return ""
    unset = "".join(
        f"; {name} set nowhere: {UNSET_MEANS[name]}" for name in fields if name in UNSET_MEANS
    )
    return f"{INHERITED_NOTE}{unset}]"


def _override_fields(keys: tuple[str, ...], old: Any) -> set[str]:
    """Names of the CapabilityToolOverride fields removed at ``keys``."""
    if keys[:1] == ("tools",) and len(keys) == 2:
        # A whole tools[<tool>] binding removed: its params_override goes with it.
        params = old.get("params_override") if isinstance(old, dict) else None
        return {str(k) for k in params} if isinstance(params, dict) else set()
    if keys[:1] == ("tool_overrides",):
        depth = 2  # tool_overrides.<tool>.<field>
    elif keys[:1] == ("tools",) and keys[2:3] == ("params_override",):
        depth = 3  # tools[<tool>].params_override.<field>
    else:
        return set()
    if len(keys) > depth + 1:
        return set()  # inside a field (e.g. one allowlist entry): not a fallback
    if len(keys) == depth + 1:
        return {keys[depth]}
    # A whole override removed (or every override, at ``tool_overrides``): the fields
    # each removed override set.
    overrides = [old]
    for _ in range(depth - len(keys)):
        overrides = [v for d in overrides if isinstance(d, dict) for v in d.values()]
    return {str(k) for d in overrides if isinstance(d, dict) for k in d}


def _render_value(value: Any) -> str:
    return ABSENT if value is _ABSENT else canonical(value)


def _sets_unrestricted(keys: tuple[str, ...], value: Any) -> bool:
    """``value`` sets a delegation allowlist to ``[]`` (= allow everything)."""
    if isinstance(value, list):
        if not value and keys and keys[-1] in _EMPTY_IS_UNRESTRICTED:
            return True
        return any(_sets_unrestricted((*keys, str(i)), v) for i, v in enumerate(value))
    if isinstance(value, dict):
        return any(_sets_unrestricted((*keys, str(k)), v) for k, v in value.items())
    return False


def _plain_line(change: _Change) -> str:
    def fmt(value: Any) -> str:
        return clip(_render_value(value), PLAIN_VALUE_MAX_CHARS)

    return f"{_PLAIN_PREFIX}{change.path}: {fmt(change.old)} → {fmt(change.new)}"


def _prompt_texts(change: _Change) -> tuple[str, str] | None:
    """``(old, new)`` prompt texts of a ``prompt`` change (absent = ``""``), or None."""
    if change.keys != ("prompt",):
        return None
    old = "" if change.old is _ABSENT else change.old
    new = "" if change.new is _ABSENT else change.new
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    return old, new


def _prompt_change(
    old: str, new: str, max_parts: int, part_units: int
) -> tuple[str, tuple[str, ...]]:
    """The summary line of a prompt change and the full diff as parts."""
    diff = prompt_diff(old, new)
    parts = split_parts(diff.lines, part_units)
    if len(parts) > max_parts:
        raise PromptChangeTooLargeError(
            f"the prompt change needs {len(parts)} messages to show every added and "
            f"removed line in full, more than the {max_parts} allowed per confirmation"
        )
    n = len(parts)
    line = (
        f"{_PLAIN_PREFIX}prompt: changed ({len(old)} → {len(new)} chars, "
        f"+{diff.added}/−{diff.removed} lines, see {n} attached part{'' if n == 1 else 's'})"
    )
    return line, tuple(parts)


def clip(text: str, limit: int) -> str:
    """Cut ``text`` at ``limit`` chars with an explicit count of what was cut."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} chars hidden)"


def inline(text: str) -> str:
    """One line of free text: whitespace runs (newlines included) collapse to one space,
    remaining control / format (bidi, zero-width) and blank-rendering characters are
    escaped as ``\\uXXXX``. Used for the reason and status lines, not for manifest values
    (those are always ASCII, see :func:`canonical`)."""
    return escape_unsafe(" ".join(text.split()))


# Non-ASCII symbols the summary and status lines use as markers (⚠ ✅ ❌ → ⌛ 🛠 🧠 ...)
# are So / Sm; the bullet of plain lines is Po.
_MARKER_CATEGORIES = frozenset({"So", "Sm", "Sk"})
_MARKER_CHARS = frozenset({"•", "‣", "◦", "·"})


def reason_text(reason: str) -> str:
    """The agent-written reason as one line that cannot imitate a summary line.

    :func:`inline`, then every non-ASCII symbol (``⚠``, ``✅``, ``❌``, ``→``, emoji,
    bullets) is escaped as ``\\uXXXX`` and ``[[`` becomes ``[ [`` (so the
    ``[[CONFIRMATION_RESOLVED]]`` history marker cannot appear), then the line is clipped.
    """
    text = "".join(
        escape_char(c)
        if not c.isascii() and (unicodedata.category(c) in _MARKER_CATEGORIES or c in _MARKER_CHARS)
        else c
        for c in inline(reason)
    )
    while "[[" in text:
        text = text.replace("[[", "[ [")
    return clip(text, REASON_MAX_CHARS)


def _fit_lines(
    head: list[str],
    required: list[str],
    plain: list[str],
    max_units: int,
    *,
    sensitive_changes: int,
) -> str:
    """``head`` + ``required`` (⚠ lines and the prompt line: never cut or dropped) +
    as many ``plain`` lines as fit, with a count of the dropped ones."""
    text = "\n".join([*head, *required, *plain])
    if utf16_len(text) <= max_units:
        return text
    needed = utf16_len("\n".join([*head, *required]))
    if needed > max_units:
        # Required lines are never cut or dropped: the proposal must be split instead.
        raise SummaryTooLargeError(sensitive_changes, needed, max_units)
    lines = [*head, *required]
    for index, line in enumerate(plain):
        note = _more_note(len(plain) - index - 1)
        if utf16_len("\n".join([*lines, line, note])) > max_units:
            break
        lines.append(line)
    hidden = len(plain) - (len(lines) - len(head) - len(required))
    lines.append(_more_note(hidden))
    text = "\n".join(lines)
    if utf16_len(text) > max_units:
        raise SummaryTooLargeError(sensitive_changes, utf16_len(text), max_units)
    return text


def _more_note(hidden: int) -> str:
    return f"(+{hidden} more change{'' if hidden == 1 else 's'} not shown)"


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
