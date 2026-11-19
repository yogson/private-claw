"""
Component ID: CMP_CORE_CAPABILITIES

Human-readable summary of a capability manifest change (for the ``capability_edit``
confirmation prompt).

The summary compares the parsed manifests before and after field by field. It is a
summary, not a diff: permission-relevant list fields (tools, overrides, MCP servers,
nested capabilities, Claude Code permissions) list exactly what was added or removed,
because those items are short and safety-relevant; the prompt shows size statistics and
a short excerpt of the added text. Nothing is filtered: the summary only informs the
approver. The result always fits ``max_units`` UTF-16 code units (Telegram counts message
length in UTF-16), and the "grants" warning is placed before the field list so a cut can
never drop it.
"""

import difflib
from collections.abc import Iterable
from typing import Any

from assistant.core.capabilities.schemas import (
    CapabilityDefinition,
    CapabilityToolBinding,
    CapabilityToolOverride,
)

GRANTS_WARNING = "⚠ Grants new tools / MCP servers / permissions"
REASON_MAX_CHARS = 300
EXCERPT_MAX_CHARS = 300
_ITEM_MAX_CHARS = 80
_LIST_MAX_ITEMS = 10
_VALUE_MAX_CHARS = 60
_TRUNCATED_NOTE = "\n… [summary truncated]"


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
    """Render the approver-facing summary of ``before`` → ``after``."""
    changes, grants = _field_changes(before, after)
    lines = [f"{headline}: {capability_id} ({file_name})"]
    if reason.strip():
        lines.append(f"Reason: {_clip(' '.join(reason.split()), REASON_MAX_CHARS)}")
    if grants:
        lines.append(GRANTS_WARNING)
    if changes:
        lines.append("Changed fields:")
        lines.extend(changes)
    else:
        lines.append("No manifest fields change (comments or formatting only).")
    return fit_utf16("\n".join(lines), max_units)


def _field_changes(
    before: CapabilityDefinition, after: CapabilityDefinition
) -> tuple[list[str], bool]:
    lines: list[str] = []
    grants = False

    if before.prompt != after.prompt:
        lines.extend(_prompt_change(before.prompt, after.prompt))

    tool_line, tool_grants = _tools_change(before.tools, after.tools)
    if tool_line:
        lines.append(tool_line)
        grants |= tool_grants

    for line, line_grants in _overrides_change(before.tool_overrides, after.tool_overrides):
        lines.append(line)
        grants |= line_grants

    for label, old, new in (
        ("nested_capabilities", before.nested_capabilities, after.nested_capabilities),
        ("mcp_servers", before.mcp_servers, after.mcp_servers),
    ):
        added, removed = _added_removed(old, new)
        if added or removed:
            lines.append(f"  • {label}: {_plus_minus(added, removed)}")
            grants |= bool(added)

    cc_lines, cc_grants = _claude_code_change(before, after)
    lines.extend(cc_lines)
    grants |= cc_grants
    return lines, grants


def _prompt_change(old: str, new: str) -> list[str]:
    added_lines = removed_lines = 0
    added_text: list[str] = []
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added_lines += 1
            added_text.append(line[2:].strip())
        elif line.startswith("- "):
            removed_lines += 1
    result = [
        f"  • prompt: {len(old)} → {len(new)} chars (+{added_lines} / −{removed_lines} lines)"
    ]
    excerpt = " ".join(part for part in added_text if part)
    if excerpt:
        result.append(f'    excerpt: "{_clip(excerpt, EXCERPT_MAX_CHARS)}"')
    return result


def _tools_change(
    old: list[CapabilityToolBinding], new: list[CapabilityToolBinding]
) -> tuple[str, bool]:
    before = {b.tool_id: b for b in old}
    after = {b.tool_id: b for b in new}
    added = [t for t in after if t not in before and after[t].enabled]
    added_disabled = [t for t in after if t not in before and not after[t].enabled]
    removed = [t for t in before if t not in after]
    parts: list[str] = []
    grants = bool(added)
    if added or removed:
        parts.append(_plus_minus(added, removed))
    for tool_id in added_disabled:
        parts.append(f"+ {tool_id} (disabled)")
    for tool_id in (t for t in after if t in before and after[t] != before[t]):
        old_b, new_b = before[tool_id], after[tool_id]
        details: list[str] = []
        if old_b.enabled != new_b.enabled:
            details.append("enabled" if new_b.enabled else "disabled")
            grants |= new_b.enabled
        if old_b.params_override != new_b.params_override:
            details.append("params_override changed")
            grants |= _override_grants(old_b.params_override, new_b.params_override)
        parts.append(f"~ {_clip(tool_id, _ITEM_MAX_CHARS)} ({', '.join(details)})")
    if not parts:
        return "", False
    return f"  • tools: {_join_limited(parts)}", grants


def _overrides_change(
    old: dict[str, CapabilityToolOverride], new: dict[str, CapabilityToolOverride]
) -> list[tuple[str, bool]]:
    result: list[tuple[str, bool]] = []
    for tool_id in sorted(old.keys() | new.keys()):
        before, after = old.get(tool_id), new.get(tool_id)
        if before == after:
            continue
        label = f"  • tool_overrides.{_clip(tool_id, _ITEM_MAX_CHARS)}"
        if before is None:
            result.append((f"{label}: added", _override_grants(None, after)))
            continue
        if after is None:
            result.append((f"{label}: removed", False))
            continue
        result.append(
            (
                f"{label}: changed ({_override_details(before, after)})",
                _override_grants(before, after),
            )
        )
    return result


def _override_details(before: CapabilityToolOverride, after: CapabilityToolOverride) -> str:
    old = before.model_dump(exclude_none=True)
    new = after.model_dump(exclude_none=True)
    details: list[str] = []
    for key in sorted(old.keys() | new.keys()):
        if old.get(key) == new.get(key):
            continue
        if key == "command_allowlist":
            added, removed = _added_removed(_allowlist_patterns(before), _allowlist_patterns(after))
            details.append(f"command_allowlist {_count_items(added, removed)}")
        elif isinstance(old.get(key), list) or isinstance(new.get(key), list):
            added, removed = _added_removed(
                [str(x) for x in old.get(key) or []], [str(x) for x in new.get(key) or []]
            )
            details.append(f"{key} {_count_items(added, removed)}")
        else:
            details.append(f"{key}: {_value(old.get(key))} → {_value(new.get(key))}")
    return "; ".join(details) if details else "changed"


def _override_grants(
    before: CapabilityToolOverride | None, after: CapabilityToolOverride | None
) -> bool:
    if after is None:
        return False
    old_allow = _allowlist_patterns(before) if before is not None else []
    old_ro = (before.shell_readonly_commands or []) if before is not None else []
    added_allow, _ = _added_removed(old_allow, _allowlist_patterns(after))
    added_ro, _ = _added_removed(old_ro, after.shell_readonly_commands or [])
    return bool(added_allow or added_ro)


def _allowlist_patterns(override: CapabilityToolOverride | None) -> list[str]:
    if override is None or not override.command_allowlist:
        return []
    return [entry.command_pattern for entry in override.command_allowlist]


def _claude_code_change(
    before: CapabilityDefinition, after: CapabilityDefinition
) -> tuple[list[str], bool]:
    old, new = before.claude_code_settings, after.claude_code_settings
    if old == new:
        return [], False
    lines: list[str] = []
    grants = False
    prefix = "  • claude_code_settings"
    old_allow = old.permissions.allow if old else []
    new_allow = new.permissions.allow if new else []
    old_deny = old.permissions.deny if old else []
    new_deny = new.permissions.deny if new else []
    added, removed = _added_removed(old_allow, new_allow)
    if added or removed:
        lines.append(f"{prefix}.permissions.allow: {_plus_minus(added, removed, quote=True)}")
        grants |= bool(added)
    added, removed = _added_removed(old_deny, new_deny)
    if added or removed:
        lines.append(f"{prefix}.permissions.deny: {_plus_minus(added, removed, quote=True)}")
        grants |= bool(removed)
    old_servers = old.mcp_servers if old else {}
    new_servers = new.mcp_servers if new else {}
    added, removed = _added_removed(list(old_servers), list(new_servers))
    changed = [k for k in new_servers if k in old_servers and new_servers[k] != old_servers[k]]
    if added or removed or changed:
        parts = [_plus_minus(added, removed)] if added or removed else []
        parts.extend(f"~ {_clip(k, _ITEM_MAX_CHARS)}" for k in changed)
        lines.append(f"{prefix}.mcp_servers: {_join_limited(parts)}")
        grants |= bool(added or changed)
    if not lines:
        lines.append(
            f"{prefix}: {'added' if old is None else 'removed' if new is None else 'changed'}"
        )
    return lines, grants


def _added_removed(old: Iterable[str], new: Iterable[str]) -> tuple[list[str], list[str]]:
    old_list, new_list = list(old), list(new)
    old_set, new_set = set(old_list), set(new_list)
    added = list(dict.fromkeys(x for x in new_list if x not in old_set))
    removed = list(dict.fromkeys(x for x in old_list if x not in new_set))
    return added, removed


def _plus_minus(added: list[str], removed: list[str], *, quote: bool = False) -> str:
    def fmt(item: str) -> str:
        item = _clip(item, _ITEM_MAX_CHARS)
        return f'"{item}"' if quote else item

    parts = [f"+ {fmt(x)}" for x in added] + [f"− {fmt(x)}" for x in removed]
    return _join_limited(parts)


def _count_items(added: list[str], removed: list[str]) -> str:
    parts: list[str] = []
    if added:
        shown = ", ".join(f'"{_clip(x, _ITEM_MAX_CHARS)}"' for x in added[:3])
        more = f", … +{len(added) - 3} more" if len(added) > 3 else ""
        parts.append(f"+{len(added)} entr{'y' if len(added) == 1 else 'ies'}: {shown}{more}")
    if removed:
        shown = ", ".join(f'"{_clip(x, _ITEM_MAX_CHARS)}"' for x in removed[:3])
        more = f", … +{len(removed) - 3} more" if len(removed) > 3 else ""
        parts.append(f"−{len(removed)} entr{'y' if len(removed) == 1 else 'ies'}: {shown}{more}")
    return "; ".join(parts) if parts else "reordered"


def _join_limited(parts: list[str]) -> str:
    if len(parts) <= _LIST_MAX_ITEMS:
        return ", ".join(parts)
    hidden = len(parts) - _LIST_MAX_ITEMS
    return ", ".join(parts[:_LIST_MAX_ITEMS]) + f", … (+{hidden} more)"


def _value(value: Any) -> str:
    return _clip(repr(value), _VALUE_MAX_CHARS)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units (how Telegram measures messages)."""
    return len(text.encode("utf-16-le")) // 2


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
