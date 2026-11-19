"""capability_change_summary: field-level summary of a manifest change (not a diff)."""

from typing import Any

from assistant.core.capabilities.edit_summary import (
    GRANTS_WARNING,
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


def test_prompt_only_change_shows_stats_and_excerpt() -> None:
    text = _summary(_cap(prompt="Line one.\nLine two."), _cap(prompt="Line one.\nLine 2, better."))

    assert text.startswith("Capability edit: cap (cap.yaml)\nReason: because\n")
    assert "• prompt: 19 → 25 chars (+1 / −1 lines)" in text
    assert 'excerpt: "Line 2, better."' in text
    assert GRANTS_WARNING not in text


def test_tools_added_and_removed_are_listed_and_grants_are_flagged() -> None:
    before = _cap(tools=[{"tool_id": "memory_search"}, {"tool_id": "shell_execute_readonly"}])
    after = _cap(tools=[{"tool_id": "memory_search"}, {"tool_id": "tavily_search"}])

    text = _summary(before, after)

    assert "• tools: + tavily_search, − shell_execute_readonly" in text
    assert GRANTS_WARNING in text
    # The warning comes before the field list so truncation can never drop it.
    assert text.index(GRANTS_WARNING) < text.index("Changed fields:")


def test_removing_only_does_not_flag_grants() -> None:
    before = _cap(tools=[{"tool_id": "a"}, {"tool_id": "b"}], mcp_servers=["finance"])
    text = _summary(before, _cap(tools=[{"tool_id": "a"}]))
    assert "− b" in text and "mcp_servers: − finance" in text
    assert GRANTS_WARNING not in text


def test_permissions_overrides_nested_and_mcp() -> None:
    before = _cap(
        tool_overrides={"shell_execute_allowlisted": {"command_allowlist": ["git status"]}},
        claude_code_settings={"permissions": {"allow": ["Read"], "deny": ["Bash(rm:*)"]}},
    )
    after = _cap(
        tool_overrides={
            "shell_execute_allowlisted": {"command_allowlist": ["git status", "git log*"]}
        },
        claude_code_settings={
            "permissions": {"allow": ["Read", "Bash(git push:*)"], "deny": []},
            "mcp_servers": {"gh": {"command": "gh-mcp"}},
        },
        nested_capabilities=["omnichem"],
        mcp_servers=["finance"],
    )

    text = _summary(before, after)

    override_line = "• tool_overrides.shell_execute_allowlisted: changed (command_allowlist"
    assert f'{override_line} +1 entry: "git log*")' in text
    assert '• claude_code_settings.permissions.allow: + "Bash(git push:*)"' in text
    assert '• claude_code_settings.permissions.deny: − "Bash(rm:*)"' in text
    assert "• claude_code_settings.mcp_servers: + gh" in text
    assert "• nested_capabilities: + omnichem" in text
    assert "• mcp_servers: + finance" in text
    assert GRANTS_WARNING in text


def test_tool_flag_and_params_changes() -> None:
    before = _cap(tools=[{"tool_id": "t", "enabled": False}])
    after = _cap(
        tools=[{"tool_id": "t", "enabled": True, "params_override": {"max_timeout_seconds": 5}}]
    )
    text = _summary(before, after)
    assert "~ t (enabled, params_override changed)" in text
    assert GRANTS_WARNING in text


def test_comment_only_change() -> None:
    text = _summary(_cap(prompt="p"), _cap(prompt="p"))
    assert "No manifest fields change" in text


def test_rollback_headline() -> None:
    text = _summary(_cap(prompt="a"), _cap(prompt="b"), headline="Capability rollback to X")
    assert text.startswith("Capability rollback to X: cap (cap.yaml)")


def test_summary_respects_the_utf16_cap_and_keeps_the_warning() -> None:
    before = _cap(prompt="x")
    after = _cap(
        prompt="🧠" * 5000,
        tools=[{"tool_id": f"tool_{i}"} for i in range(200)],
        claude_code_settings={
            "permissions": {"allow": [f"Bash({'y' * 70}{i}:*)" for i in range(200)]}
        },
    )

    text = _summary(before, after, max_units=1000)

    assert utf16_len(text) <= 1000
    assert GRANTS_WARNING in text
    assert "(+190 more)" in text  # long lists are shortened, not dumped


def test_long_reason_is_clipped() -> None:
    text = capability_change_summary(
        capability_id="cap",
        file_name="cap.yaml",
        reason="r" * 1000,
        before=_cap(prompt="a"),
        after=_cap(prompt="b"),
    )
    reason_line = text.splitlines()[1]
    assert len(reason_line) <= len("Reason: ") + 300
