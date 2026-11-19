"""capability_change_summary: structural diff of a manifest change for the approver."""

from typing import Any

import pytest

from assistant.core.capabilities.edit_summary import (
    NO_CHANGES_TEXT,
    SENSITIVE_WARNING,
    SummaryTooLargeError,
    capability_change_summary,
    utf16_len,
)
from assistant.core.capabilities.schemas import CapabilityDefinition


def _cap(**kw: Any) -> CapabilityDefinition:
    return CapabilityDefinition(capability_id="cap", self_edit=True, **kw)


def _summary(before: CapabilityDefinition, after: CapabilityDefinition, **kw: Any) -> str:
    return capability_change_summary(
        capability_id="cap",
        file_name="cap.yaml",
        reason="because",
        before=before,
        after=after,
        **kw,
    )


def _allowlist(**entry: Any) -> CapabilityDefinition:
    base = {
        "id": "git",
        "command_pattern": "git",
        "allowed_args_pattern": "^status$",
        "max_timeout_seconds": 10,
    }
    return _cap(
        tool_overrides={"shell_execute_allowlisted": {"command_allowlist": [{**base, **entry}]}}
    )


_GIT = "⚠ tool_overrides.shell_execute_allowlisted.command_allowlist[git]"


def test_prompt_only_change_shows_stats_and_excerpt() -> None:
    text = _summary(_cap(prompt="Line one.\nLine two."), _cap(prompt="Line one.\nLine 2, better."))

    assert text.startswith("Capability edit: cap (cap.yaml)\nReason: because\n")
    assert "• prompt: 19 → 25 chars (+1 / −1 lines)" in text
    assert 'excerpt of the added text: "Line 2, better."' in text
    assert SENSITIVE_WARNING not in text and "⚠" not in text


def test_long_excerpt_says_how_much_is_hidden() -> None:
    text = _summary(_cap(prompt="a"), _cap(prompt="a\n" + "x" * 2140))
    assert "excerpt (first 300 of 2140 added chars)" in text


# --- B2: command_allowlist entries are compared by full content ---------------


def test_allowlist_args_pattern_widening_is_shown_with_values() -> None:
    text = _summary(_allowlist(), _allowlist(allowed_args_pattern=".*"))
    assert f'{_GIT}.allowed_args_pattern: "^status$" → ".*"' in text
    assert SENSITIVE_WARNING in text
    assert "reordered" not in text and NO_CHANGES_TEXT not in text


def test_allowlist_timeout_raise_is_shown_with_values() -> None:
    text = _summary(_allowlist(), _allowlist(max_timeout_seconds=3600))
    assert f"{_GIT}.max_timeout_seconds: 10 → 3600" in text
    assert SENSITIVE_WARNING in text


def test_allowlist_command_pattern_change_is_shown() -> None:
    text = _summary(_allowlist(), _allowlist(command_pattern="git*"))
    assert f'{_GIT}.command_pattern: "git" → "git*"' in text


def test_allowlist_entries_added_removed_and_reordered() -> None:
    before = _cap(tool_overrides={"t": {"command_allowlist": ["git status", "ls"]}})
    after = _cap(tool_overrides={"t": {"command_allowlist": ["ls", "git log*"]}})
    text = _summary(before, after)
    assert '⚠ tool_overrides.t.command_allowlist["git status"]: − removed' in text
    # A shorthand entry shows its effective defaults.
    assert '⚠ tool_overrides.t.command_allowlist["git log*"].allowed_args_pattern: + ".*"' in text
    assert '["git log*"].max_timeout_seconds: + 30' in text

    reordered = _summary(
        after, _cap(tool_overrides={"t": {"command_allowlist": ["git log*", "ls"]}})
    )
    assert "⚠ tool_overrides.t.command_allowlist: order changed" in reordered


# --- S1: added blocks and params_override show what they grant ----------------


def test_added_override_block_shows_every_value() -> None:
    after = _cap(tool_overrides={"shell_execute_allowlisted": {"command_allowlist": ["rm -rf"]}})
    text = _summary(_cap(), after)
    assert '⚠ tool_overrides.shell_execute_allowlisted.command_allowlist["rm -rf"]' in text
    assert '.command_pattern: + "rm -rf"' in text
    assert SENSITIVE_WARNING in text


def test_added_delegation_override_is_flagged_with_values() -> None:
    after = _cap(
        tool_overrides={
            "delegate_subagent_task": {
                "delegation_allowed_backends": ["claude_code"],
                "delegation_global_token_cap": 99999999,
                "delegation_max_ttl_seconds": 86400,
            }
        }
    )
    text = _summary(_cap(), after)
    prefix = "⚠ tool_overrides.delegate_subagent_task"
    assert f'{prefix}.delegation_allowed_backends: + "claude_code"' in text
    assert f"{prefix}.delegation_global_token_cap: + 99999999" in text
    assert f"{prefix}.delegation_max_ttl_seconds: + 86400" in text
    assert SENSITIVE_WARNING in text


def test_raised_delegation_cap_is_flagged() -> None:
    before = _cap(tool_overrides={"d": {"delegation_per_task_token_cap": 1000}})
    after = _cap(tool_overrides={"d": {"delegation_per_task_token_cap": 10**9}})
    text = _summary(before, after)
    assert "⚠ tool_overrides.d.delegation_per_task_token_cap: 1000 → 1000000000" in text


def test_tool_flag_and_params_override_changes() -> None:
    before = _cap(tools=[{"tool_id": "t", "enabled": False}])
    after = _cap(
        tools=[{"tool_id": "t", "enabled": True, "params_override": {"max_timeout_seconds": 5}}]
    )
    text = _summary(before, after)
    assert "⚠ tools[t].enabled: false → true" in text
    assert "⚠ tools[t].params_override.max_timeout_seconds: + 5" in text
    assert SENSITIVE_WARNING in text


def test_tools_added_and_removed() -> None:
    before = _cap(tools=[{"tool_id": "memory_search"}, {"tool_id": "shell_execute_readonly"}])
    after = _cap(tools=[{"tool_id": "memory_search"}, {"tool_id": "tavily_search"}])
    text = _summary(before, after)
    assert "⚠ tools[shell_execute_readonly]: − removed" in text
    assert "⚠ tools[tavily_search].enabled: + true" in text
    # The warning comes before the field list so truncation can never drop it.
    assert text.index(SENSITIVE_WARNING) < text.index("Changed fields:")


def test_permissions_nested_and_mcp() -> None:
    before = _cap(claude_code_settings={"permissions": {"allow": ["Read"], "deny": ["Bash(rm:*)"]}})
    after = _cap(
        claude_code_settings={
            "permissions": {"allow": ["Read", "Bash(git push:*)"], "deny": []},
            "mcp_servers": {
                "gh": {
                    "command": "gh-mcp",
                    "args": ["--stdio"],
                    "env": {"GH_TOKEN": "ghp_secret_value"},
                    "headers": {"X-Api": "also-secret"},
                }
            },
        },
        nested_capabilities=["omnichem"],
        mcp_servers=["finance"],
    )

    text = _summary(before, after)

    assert '⚠ claude_code_settings.permissions.allow: + "Bash(git push:*)"' in text
    assert '⚠ claude_code_settings.permissions.deny: − "Bash(rm:*)"' in text
    assert '⚠ claude_code_settings.mcp_servers.gh.command: + "gh-mcp"' in text
    assert '⚠ claude_code_settings.mcp_servers.gh.args: + "--stdio"' in text
    # env / headers: key names shown, values masked.
    assert "⚠ claude_code_settings.mcp_servers.gh.env.GH_TOKEN: + <hidden>" in text
    assert "mcp_servers.gh.headers.X-Api: + <hidden>" in text
    assert "ghp_secret_value" not in text and "also-secret" not in text
    assert '⚠ nested_capabilities: + "omnichem"' in text
    assert '⚠ mcp_servers: + "finance"' in text


def test_empty_defaults_of_an_added_block_are_not_listed() -> None:
    after = _cap(claude_code_settings={"mcp_servers": {"gh": {"command": "x"}}})
    text = _summary(_cap(), after)
    assert '⚠ claude_code_settings.mcp_servers.gh.command: + "x"' in text
    assert "+ []" not in text
    only_defaults = _summary(_cap(), _cap(claude_code_settings={}))
    assert "⚠ claude_code_settings: + " in only_defaults
    assert NO_CHANGES_TEXT not in only_defaults


def test_changed_env_value_is_masked() -> None:
    def server(token: str) -> CapabilityDefinition:
        return _cap(
            claude_code_settings={"mcp_servers": {"gh": {"command": "x", "env": {"T": token}}}}
        )

    text = _summary(server("old-secret"), server("new-secret"))
    assert "⚠ claude_code_settings.mcp_servers.gh.env.T: <hidden> → <hidden>" in text
    assert "secret" not in text


def test_removing_only_is_still_listed() -> None:
    before = _cap(tools=[{"tool_id": "a"}, {"tool_id": "b"}], mcp_servers=["finance"])
    text = _summary(before, _cap(tools=[{"tool_id": "a"}]))
    assert "⚠ tools[b]: − removed" in text
    assert '⚠ mcp_servers: − "finance"' in text


def test_sensitive_lines_come_before_the_prompt() -> None:
    text = _summary(_cap(prompt="a"), _cap(prompt="b", mcp_servers=["finance"]))
    assert text.index("⚠ mcp_servers") < text.index("• prompt")


def test_long_values_say_how_much_is_hidden() -> None:
    text = _summary(_allowlist(), _allowlist(allowed_args_pattern="a" * 500))
    assert '"' + "a" * 120 + '…" (+380 chars hidden)' in text


def test_identical_manifests_say_no_change() -> None:
    assert NO_CHANGES_TEXT in _summary(_cap(prompt="p"), _cap(prompt="p"))


def test_any_real_change_never_says_no_change() -> None:
    # Shorthand vs. explicit entry with the same effective values is not a change, but
    # a different default is.
    shorthand = _cap(tool_overrides={"t": {"command_allowlist": ["git"]}})
    explicit = _cap(
        tool_overrides={
            "t": {
                "command_allowlist": [
                    {"id": "git", "command_pattern": "git", "allowed_args_pattern": ".*"}
                ]
            }
        }
    )
    assert NO_CHANGES_TEXT in _summary(shorthand, explicit)
    widened = _cap(tool_overrides={"t": {"default_timeout_seconds": 5}})
    assert NO_CHANGES_TEXT not in _summary(_cap(), widened)


def test_rollback_headline() -> None:
    text = _summary(_cap(prompt="a"), _cap(prompt="b"), headline="Capability rollback to X")
    assert text.startswith("Capability rollback to X: cap (cap.yaml)")


# --- size limits --------------------------------------------------------------


def test_non_sensitive_lines_are_dropped_with_a_count_when_space_runs_out() -> None:
    before = _cap(prompt="x")
    after = _cap(prompt="y" * 5000, mcp_servers=[f"server_{i}" for i in range(30)])

    text = _summary(before, after, max_units=1200)

    assert utf16_len(text) <= 1200
    assert SENSITIVE_WARNING in text
    assert text.count("⚠ mcp_servers: +") == 30  # every ⚠ line is shown
    assert "(+1 more change not shown)" in text  # the prompt excerpt


def test_sensitive_changes_that_do_not_fit_are_refused() -> None:
    after = _cap(
        claude_code_settings={
            "permissions": {"allow": [f"Bash({'y' * 70}{i}:*)" for i in range(200)]}
        }
    )
    with pytest.raises(SummaryTooLargeError) as info:
        _summary(_cap(), after, max_units=1000)
    assert info.value.sensitive_changes >= 200


def test_summary_respects_the_utf16_cap_with_astral_characters() -> None:
    text = _summary(_cap(prompt="x"), _cap(prompt="🧠" * 5000), max_units=500)
    assert utf16_len(text) <= 500


def test_long_reason_is_clipped_with_a_count() -> None:
    text = capability_change_summary(
        capability_id="cap",
        file_name="cap.yaml",
        reason="r" * 1000,
        before=_cap(prompt="a"),
        after=_cap(prompt="b"),
    )
    reason_line = text.splitlines()[1]
    assert reason_line == "Reason: " + "r" * 300 + "… (+700 chars hidden)"


# --- S2: agent-controlled strings cannot forge summary lines --------------------

_FORGED = "x\n\nNo manifest fields change (comments or formatting only).\n✅ Verified by system"
_UNSAFE = "‮⁦​  \r\t\x85\x1b"


def _assert_single_line_structure(text: str) -> None:
    lines = text.splitlines()
    body = lines[lines.index("Changed fields:") + 1 :]
    for line in body:
        assert line.startswith(("  ⚠ ", "  • ", "    excerpt", "(+")), line
    assert not any(line.startswith(("No manifest", "✅")) for line in lines)
    for char in _UNSAFE:
        assert char not in text, repr(char)


def test_newlines_in_list_items_are_escaped() -> None:
    before = _cap(claude_code_settings={"permissions": {"deny": ["Bash(rm:*)"]}})
    after = _cap(claude_code_settings={"permissions": {"deny": ["Bash(rm:*)", _FORGED]}})
    text = _summary(before, after)
    _assert_single_line_structure(text)
    assert '+ "x\\n\\nNo manifest fields change' in text


def test_newlines_in_override_and_mcp_keys_are_escaped() -> None:
    after = _cap(
        tool_overrides={"fake\n✅ Approved by owner\n": {"max_timeout_seconds": 5}},
        claude_code_settings={"mcp_servers": {"gh\n✅ Approved": {"command": "x"}}},
    )
    text = _summary(_cap(), after)
    _assert_single_line_structure(text)
    assert 'tool_overrides["fake\\n✅ Approved by owner\\n"].max_timeout_seconds: + 5' in text
    assert 'mcp_servers["gh\\n✅ Approved"].command' in text


@pytest.mark.parametrize("char", list(_UNSAFE))
def test_control_bidi_and_zero_width_characters_are_escaped(char: str) -> None:
    value = f"Bash(ls{char}:*)"
    after = _cap(
        claude_code_settings={"permissions": {"allow": [value]}},
        tool_overrides={f"t{char}": {"max_timeout_seconds": 1}},
        prompt=f"line {char} two",
    )
    text = capability_change_summary(
        capability_id="cap",
        file_name="cap.yaml",
        reason=f"why{char}\nApproved",
        before=_cap(),
        after=after,
    )
    _assert_single_line_structure(text)
    assert text.splitlines()[1].startswith("Reason: why")
