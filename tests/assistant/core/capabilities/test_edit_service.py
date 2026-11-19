"""CapabilityEditService: read, propose (validation, guards), apply, history, rollback."""

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from assistant.core.capabilities.catalog import CapabilityCatalog
from assistant.core.capabilities.edit_service import (
    HISTORY_DIR_NAME,
    STATUS_APPLIED,
    STATUS_BACKUP_FAILED,
    STATUS_NOT_EDITABLE,
    STATUS_REJECTED_INVALID,
    STATUS_RELOAD_REJECTED,
    STATUS_STALE,
    STATUS_WRITE_FAILED,
    CapabilityEditError,
    CapabilityEditService,
    EditProposal,
    check_protected_fields,
    find_git_work_tree,
)
from assistant.core.capabilities.loader import ManifestParseError, parse_capability_manifest
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilityLoadIssue

KNOWN_TOOLS = {"memory_search", "tavily_search", "shell_execute_readonly"}

EDITABLE = """\
# Editable test manifest.
# This comment must survive every edit byte for byte.
capability_id: editable
self_edit: true
prompt: |
  Be concise.
  Answer in English.

tools:
  - tool_id: memory_search
    enabled: true   # inline comment
"""
LOCKED = "capability_id: locked\nprompt: hi\n"
FLAG_FALSE = "capability_id: flagfalse\nself_edit: false\nprompt: hi\n"


def _tool_validator(
    definitions: Mapping[str, CapabilityDefinition],
) -> list[CapabilityLoadIssue]:
    return [
        CapabilityLoadIssue("validation", f"'{cid}' references unknown tool '{b.tool_id}'")
        for cid, d in sorted(definitions.items())
        for b in d.tools
        if b.tool_id not in KNOWN_TOOLS
    ]


def _sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    root = tmp_path / "config.local"
    caps = root / "capabilities"
    caps.mkdir(parents=True)
    (caps / "editable.yaml").write_text(EDITABLE)
    (caps / "locked.yaml").write_text(LOCKED)
    (caps / "flagfalse.yaml").write_text(FLAG_FALSE)
    return root


@pytest.fixture()
def catalog(config_dir: Path) -> CapabilityCatalog:
    return CapabilityCatalog(config_dir, validator=_tool_validator, min_refresh_interval=0)


@pytest.fixture()
def service(catalog: CapabilityCatalog) -> CapabilityEditService:
    return CapabilityEditService(catalog)


def _manifest(config_dir: Path, name: str = "editable.yaml") -> Path:
    return config_dir / "capabilities" / name


def _propose(
    service: CapabilityEditService,
    old: str = "Be concise.",
    new: str = "Be concise and friendly.",
    *,
    cap: str = "editable",
    base: str | None = None,
) -> EditProposal:
    if base is None:
        base = service.read(cap).base_sha256
    return service.propose_edit(cap, old, new, "user asked for a friendlier tone", base)


def _error(fn: Any, *args: Any, **kwargs: Any) -> CapabilityEditError:
    with pytest.raises(CapabilityEditError) as info:
        fn(*args, **kwargs)
    return info.value


async def _apply(service: CapabilityEditService, proposal: EditProposal, cid: str = "c1") -> Any:
    return await service.apply(
        proposal.payload, confirmation_id=cid, session_id="tg:1", user_id="u1"
    )


def _history_files(config_dir: Path, cap: str = "editable") -> list[str]:
    directory = config_dir / HISTORY_DIR_NAME / cap
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


# ---------------------------------------------------------------------------
# read / editability
# ---------------------------------------------------------------------------


def test_read_returns_raw_text_sha_and_revision(
    service: CapabilityEditService, config_dir: Path
) -> None:
    view = service.read("editable")
    assert view.raw_yaml == EDITABLE
    assert view.base_sha256 == _sha(EDITABLE)
    assert view.self_edit is True
    assert view.file == "editable.yaml"
    assert view.revision == 1


@pytest.mark.parametrize("cap", ["locked", "flagfalse", "unknown", "../editable", ".hidden"])
def test_not_editable_manifests_are_refused(service: CapabilityEditService, cap: str) -> None:
    assert _error(service.read, cap).status == STATUS_NOT_EDITABLE
    err = _error(service.propose_edit, cap, "hi", "hello", "r", "0" * 64)
    assert err.status == STATUS_NOT_EDITABLE


def test_editable_ids_lists_only_opted_in_manifests(service: CapabilityEditService) -> None:
    assert service.editable_ids() == ["editable"]


def test_editable_ids_skips_manifests_that_read_would_refuse(config_dir: Path) -> None:
    caps = config_dir / "capabilities"
    (caps / "loose.yaml").write_text("capability_id: loose\nself_edit: yes\nprompt: hi\n")
    (caps / "hidden.yaml").write_text(
        'capability_id: hidden\nself_edit: true\n"self\\x5fedit": true\nprompt: hi\n'
    )
    (caps / "plain.yaml").write_text("capability_id: plain\nself_edit: true\nprompt: hi\n")
    catalog = CapabilityCatalog(config_dir, validator=_tool_validator, min_refresh_interval=0)
    service = CapabilityEditService(catalog)
    assert {"loose", "hidden"} <= set(catalog.current().definitions)
    assert service.editable_ids() == ["editable", "plain"]


def test_symlink_escaping_the_config_dir_is_not_editable(tmp_path: Path, config_dir: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text("capability_id: escaped\nself_edit: true\nprompt: x\n")
    (config_dir / "capabilities" / "escaped.yaml").symlink_to(outside)
    service = CapabilityEditService(CapabilityCatalog(config_dir, min_refresh_interval=0))

    err = _error(service.read, "escaped")
    assert err.status == STATUS_NOT_EDITABLE
    assert "outside the active config dir" in err.reason


async def test_symlinked_manifest_is_edited_at_its_target(config_dir: Path) -> None:
    shared = config_dir / "shared"
    shared.mkdir()
    target = shared / "linked.yaml"
    target.write_text("capability_id: linked\nself_edit: true\nprompt: old text\n")
    link = config_dir / "capabilities" / "linked.yaml"
    link.symlink_to(Path("..") / "shared" / "linked.yaml")
    catalog = CapabilityCatalog(config_dir, min_refresh_interval=0)
    service = CapabilityEditService(catalog)

    proposal = _propose(service, "old text", "new text", cap="linked")
    outcome = await _apply(service, proposal)

    assert outcome.ok, outcome.detail
    assert link.is_symlink()
    assert target.read_text() == "capability_id: linked\nself_edit: true\nprompt: new text\n"
    assert catalog.current().get("linked").prompt == "new text"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# proposals: exact match, validation, guards
# ---------------------------------------------------------------------------


def test_valid_proposal_writes_nothing_and_carries_a_summary(
    service: CapabilityEditService, config_dir: Path
) -> None:
    proposal = _propose(service)

    assert _manifest(config_dir).read_text() == EDITABLE
    assert not (config_dir / HISTORY_DIR_NAME).exists()
    payload = proposal.payload
    assert payload["op"] == "edit"
    assert payload["base_sha256"] == _sha(EDITABLE)
    assert payload["new_sha256"] == _sha(
        EDITABLE.replace("Be concise.", "Be concise and friendly.")
    )
    assert payload["base_revision"] == 1
    assert "editable (editable.yaml)" in proposal.summary
    assert "• prompt: changed (" in proposal.summary and "friendly" not in proposal.summary
    assert proposal.prompt_title == "Prompt change for editable"
    assert proposal.prompt_parts == (
        "@@ -1,3 +1,3 @@\n- Be concise.\n+ Be concise and friendly.\n  Answer in English.\n  ",
    )


def test_dedupe_key_is_stable_for_the_same_proposal(service: CapabilityEditService) -> None:
    assert _propose(service).dedupe_key == _propose(service).dedupe_key
    assert _propose(service).dedupe_key != _propose(service, new="Be brief.").dedupe_key


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("", "x", "must not be empty"),
        ("Be concise.", "Be concise.", "identical"),
        ("Not in the file", "x", "not found"),
        ("enabled: true", "enabled: false", "not unique"),  # a second binding is added below
    ],
)
def test_exact_match_rules(
    service: CapabilityEditService, old: str, new: str, match: str, config_dir: Path
) -> None:
    if match == "not unique":
        _manifest(config_dir).write_text(
            EDITABLE + "  - tool_id: tavily_search\n    enabled: true\n"
        )
    err = _error(_propose, service, old, new)
    assert err.status == STATUS_REJECTED_INVALID
    assert match in err.reason


def test_empty_reason_is_rejected(service: CapabilityEditService) -> None:
    base = service.read("editable").base_sha256
    err = _error(service.propose_edit, "editable", "Be concise.", "Be brief.", "  ", base)
    assert err.status == STATUS_REJECTED_INVALID


def test_stale_base_sha_at_propose(service: CapabilityEditService) -> None:
    err = _error(_propose, service, "Be concise.", "Be brief.", base="0" * 64)
    assert err.status == STATUS_STALE
    assert "capability_read" in err.reason


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("prompt: |\n  Be concise.", "prompt: [unclosed", "YAML parse error"),
        ("tools:\n", "unknown_field: 1\ntools:\n", "extra fields not permitted"),  # schema
        ("tool_id: memory_search", "tool_id: not_a_tool", "cross-file"),  # unknown tool
        ("capability_id: editable", "capability_id: renamed", "capability_id must stay"),
    ],
)
def test_invalid_results_are_rejected_at_propose(
    service: CapabilityEditService, old: str, new: str, match: str
) -> None:
    err = _error(_propose, service, old, new)
    assert err.status == STATUS_REJECTED_INVALID
    assert match in err.reason


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("self_edit: true", "self_edit: false", "plain YAML boolean true"),  # flip
        ("self_edit: true", 'self_edit: "true"', "plain YAML boolean true"),  # quoted
        ("self_edit: true", "self_edit: yes", "plain YAML boolean true"),  # YAML 1.1 bool
        ("self_edit: true\n", "", "exactly one top-level 'self_edit'"),  # delete the line
        ("true\nprompt", "false\nprompt", "plain YAML boolean true"),  # value only
        ("tools:\n", "self_edit: true\ntools:\n", "duplicate key"),  # plain duplicate
        # Reviewer repro B1: an escaped duplicate *before* the real line, value true.
        (
            "capability_id: editable\n",
            'capability_id: editable\n"self\\x5fedit": true\n',
            "duplicate key",
        ),
        ("tools:\n", '"self\\x5fedit": false\ntools:\n', "duplicate key"),
        ("tools:\n", "Self_Edit: true\ntools:\n", "duplicate key"),  # case variant
        ("tools:\n", "'self_edit': true\ntools:\n", "duplicate key"),  # quoting variant
        # Reviewer repro B1: a merge key that carries a hidden second opt-in.
        (
            "capability_id: editable\n",
            'capability_id: editable\n<<: {"self\\x5fedit": true}\n',
            "merge keys",
        ),
        ("tools:\n", "<<: {prompt: x}\ntools:\n", "merge keys"),
        ("self_edit: true", "self_edit: &t true\nx: *t", "aliases"),
        ("capability_id: editable\n", "", "exactly one top-level 'capability_id'"),
        ("editable\n", "editable\ncapability_id: editable\n", "duplicate key"),
    ],
)
def test_protected_keys_cannot_be_changed_duplicated_or_hidden(
    service: CapabilityEditService, old: str, new: str, match: str
) -> None:
    err = _error(_propose, service, old, new)
    assert err.status == STATUS_REJECTED_INVALID
    assert match in err.reason, err.reason


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("tools:\n", "tools:\n  # self_edit mention\n"),  # comments are free text
        ("true\n", "true  # set by a human\n"),  # a comment on the self_edit line
    ],
)
def test_comments_mentioning_self_edit_are_allowed(
    service: CapabilityEditService, old: str, new: str
) -> None:
    assert _propose(service, old, new).payload["op"] == "edit"


def test_reviewer_repro_hidden_opt_in_never_reaches_the_file(
    service: CapabilityEditService, config_dir: Path
) -> None:
    # The exact repro from the PR review: manifest ``ed`` with a planted escaped
    # duplicate or merge key, both of which used to be accepted and survive a revoke.
    _manifest(config_dir, "ed.yaml").write_text(
        "capability_id: ed\nself_edit: true\nprompt: hello\n"
    )
    service._catalog.reload(reason="test")  # noqa: SLF001
    base = service.read("ed").base_sha256
    for new in (
        'capability_id: ed\n"self\\x5fedit": true\n',
        'capability_id: ed\n<<: {"self\\x5fedit": true}\n',
    ):
        err = _error(service.propose_edit, "ed", "capability_id: ed\n", new, "tidy", base)
        assert err.status == STATUS_REJECTED_INVALID
    assert _manifest(config_dir, "ed.yaml").read_text().count("self") == 1


def test_parsed_protected_field_check_is_its_own_layer() -> None:
    # Behind the structural check: the parsed values must also be unchanged.
    before = CapabilityDefinition(capability_id="editable", self_edit=True)
    check_protected_fields("editable", before, before)
    for after, match in (
        (CapabilityDefinition(capability_id="editable", self_edit=False), "self_edit must stay"),
        (CapabilityDefinition(capability_id="other", self_edit=True), "capability_id must stay"),
    ):
        with pytest.raises(CapabilityEditError, match=match):
            check_protected_fields("editable", before, after)
    with pytest.raises(CapabilityEditError, match="self_edit must stay"):
        check_protected_fields("editable", CapabilityDefinition(capability_id="editable"), before)


@pytest.mark.parametrize("flag", ['"true"', "'yes'", '"on"', "1", "0", "null"])
def test_self_edit_is_a_strict_boolean_in_the_schema(flag: str) -> None:
    with pytest.raises(ManifestParseError, match="self_edit"):
        parse_capability_manifest("x.yaml", f"capability_id: x\nself_edit: {flag}\n".encode())


@pytest.mark.parametrize("flag", ["yes", "on", "On", '!!bool "true"'])
def test_yaml_11_booleans_do_not_make_a_manifest_editable(config_dir: Path, flag: str) -> None:
    # YAML 1.1 resolves these to a boolean (so the manifest loads), but only the plain
    # ``true`` opts in to agent edits.
    (config_dir / "capabilities" / "loose.yaml").write_text(
        f"capability_id: loose\nself_edit: {flag}\nprompt: hi\n"
    )
    catalog = CapabilityCatalog(config_dir, validator=_tool_validator, min_refresh_interval=0)
    err = _error(CapabilityEditService(catalog).read, "loose")
    assert err.status == STATUS_NOT_EDITABLE and "plain YAML boolean true" in err.reason


def test_a_file_that_already_hides_a_second_opt_in_is_not_editable(
    service: CapabilityEditService, config_dir: Path
) -> None:
    _manifest(config_dir).write_text(
        EDITABLE.replace("tools:\n", '"self\\x5fedit": true\ntools:\n')
    )
    service._catalog.reload(reason="test")  # noqa: SLF001
    err = _error(service.read, "editable")
    assert err.status == STATUS_NOT_EDITABLE and "duplicate key" in err.reason


async def test_apply_rechecks_the_structure_of_a_tampered_payload(
    service: CapabilityEditService, config_dir: Path
) -> None:
    proposal = _propose(service)
    hidden = 'capability_id: editable\n<<: {"self\\x5fedit": true}\n'
    new_text = EDITABLE.replace("capability_id: editable\n", hidden)
    payload = {
        **proposal.payload,
        "old_string": "capability_id: editable\n",
        "new_string": hidden,
        "new_sha256": _sha(new_text),
    }
    outcome = await service.apply(payload, confirmation_id="c1", session_id="s", user_id=None)
    assert not outcome.ok and outcome.status == STATUS_REJECTED_INVALID
    assert "merge keys" in outcome.detail
    assert _manifest(config_dir).read_text() == EDITABLE


def test_allowlist_widening_is_spelled_out_in_the_proposal_summary(
    service: CapabilityEditService, config_dir: Path
) -> None:
    # Reviewer repro B2: used to render as "command_allowlist reordered" with no warning.
    _manifest(config_dir).write_text(
        EDITABLE
        + "tool_overrides:\n  shell_execute_allowlisted:\n    command_allowlist:\n"
        + '      - {id: git, command_pattern: git, allowed_args_pattern: "^status$", '
        + "max_timeout_seconds: 10}\n"
    )
    proposal = _propose(
        service, '"^status$", max_timeout_seconds: 10', '".*", max_timeout_seconds: 3600'
    )
    assert "⚠ Security-sensitive changes" in proposal.summary
    assert '[git].allowed_args_pattern: "^status$" → ".*"' in proposal.summary
    assert "[git].max_timeout_seconds: 10 → 3600" in proposal.summary


def test_proposal_whose_sensitive_changes_do_not_fit_is_refused(
    service: CapabilityEditService,
) -> None:
    allow = "".join(f"      - Bash({'y' * 70}{i}:*)\n" for i in range(200))
    new = f"claude_code_settings:\n  permissions:\n    allow:\n{allow}tools:\n"
    err = _error(_propose, service, "tools:\n", new)
    assert err.status == STATUS_REJECTED_INVALID
    assert "security-sensitive field changes" in err.reason and "smaller edits" in err.reason


def _with_args_pattern(config_dir: Path, pattern: str) -> None:
    _manifest(config_dir).write_text(
        EDITABLE
        + "tool_overrides:\n  shell_execute_allowlisted:\n    command_allowlist:\n"
        + f"      - {{id: git, command_pattern: git, allowed_args_pattern: '{pattern}'}}\n"
    )


def test_long_pattern_widened_at_the_tail_shows_both_full_values(
    service: CapabilityEditService, config_dir: Path
) -> None:
    # Reviewer repro B3: both sides used to render as the same 120-char prefix.
    long = "^(" + "a" * 130 + ")$"
    _with_args_pattern(config_dir, long)
    proposal = _propose(service, f"'{long}'", f"'{long}|.*'")
    assert f'[git].allowed_args_pattern: "{long}" → "{long}|.*"' in proposal.summary


def test_sensitive_change_too_long_to_show_in_full_is_refused(
    service: CapabilityEditService, config_dir: Path
) -> None:
    _with_args_pattern(config_dir, "^status$")
    err = _error(_propose, service, "'^status$'", f"'^({'b' * 4000})$'")
    assert err.status == STATUS_REJECTED_INVALID
    assert "1 security-sensitive field changes" in err.reason
    assert "Nothing is ever cut" in err.reason and "smaller edits" in err.reason


_SERVER = (
    EDITABLE
    + 'claude_code_settings:\n  permissions:\n    deny: ["Bash(rm:*)"]\n'
    + "  mcp_servers:\n    fs:\n      url: https://api.example.com/mcp\n"
    + "      args: [--allow, /home/me/proj, --deny, /]\n      flag: true\n      n: 1\n"
)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # Reviewer repros B4 (homoglyphs), B5 (list order), S7 (type-strict values).
        (
            "url: https://api.example.com/mcp",
            "url: https://api.exаmple.com/mcp",
            'fs.url: "https://api.example.com/mcp" → "https://api.ex\\u0430mple.com/mcp"',
        ),
        (
            'deny: ["Bash(rm:*)"]',
            'deny: ["Bash(rм:*)"]',
            'permissions.deny: ["Bash(rm:*)"] → ["Bash(r\\u043c:*)"]',
        ),
        (
            "args: [--allow, /home/me/proj, --deny, /]",
            "args: [--allow, /, --deny, /home/me/proj, -v]",
            'fs.args: ["--allow", "/home/me/proj", "--deny", "/"] → '
            '["--allow", "/", "--deny", "/home/me/proj", "-v"]',
        ),
        (
            "args: [--allow, /home/me/proj, --deny, /]",
            "args: [--allow, /, --deny, /home/me/proj]",
            'fs.args: ["--allow", "/home/me/proj", "--deny", "/"] → '
            '["--allow", "/", "--deny", "/home/me/proj"]',
        ),
        ("flag: true", "flag: 1", "fs.flag: true → 1"),
        ("n: 1\n", "n: 1.0\n", "fs.n: 1 → 1.0"),
    ],
)
def test_reviewer_repros_are_visible_in_the_proposal(
    service: CapabilityEditService, config_dir: Path, old: str, new: str, expected: str
) -> None:
    _manifest(config_dir).write_text(_SERVER)
    proposal = _propose(service, old, new)
    assert "No manifest fields change" not in proposal.summary
    assert f"⚠ claude_code_settings.{'mcp_servers.' if 'fs.' in expected else ''}{expected}" in (
        proposal.summary
    )


@pytest.mark.parametrize(
    "value", [".nan", ".inf", "2024-01-01", "!!binary YQ==", "!!set {a: null}", "{1: x}"]
)
def test_non_json_mcp_values_are_rejected_at_propose(
    service: CapabilityEditService, config_dir: Path, value: str
) -> None:
    # Reviewer repro S7: these used to be accepted and crash json.dumps at sub-agent launch.
    _manifest(config_dir).write_text(_SERVER)
    err = _error(_propose, service, "n: 1\n", f"n: {value}\n")
    assert err.status == STATUS_REJECTED_INVALID
    assert "MCP server settings must be" in err.reason or "not valid JSON" in err.reason


def test_added_delegation_block_with_empty_allowlists_is_visible(
    service: CapabilityEditService,
) -> None:
    # Reviewer repro B6: only the TTL line used to be shown.
    block = (
        "tool_overrides:\n  delegate_subagent_task:\n    delegation_default_ttl_seconds: 600\n"
        "    delegation_allowed_backends: []\n    delegation_model_allowlist: []\n"
    )
    proposal = _propose(service, "tools:\n", block + "tools:\n")
    assert (
        "⚠ tool_overrides.delegate_subagent_task: (absent) → "
        '{"delegation_allowed_backends": [], "delegation_default_ttl_seconds": 600, '
        '"delegation_model_allowlist": []} [note: an empty delegation allowlist means NO '
        "restriction]"
    ) in proposal.summary


_SECRET = "ghp_SECRETVALUE123"
_WITH_ENV = (
    EDITABLE
    + "claude_code_settings:\n  mcp_servers:\n    gh:\n      command: gh-mcp\n"
    + f"      env:\n        GITHUB_TOKEN: {_SECRET}\n        LOG_LEVEL: info\n"
)


def test_env_replaced_by_a_string_does_not_leak_the_secret(
    service: CapabilityEditService, config_dir: Path
) -> None:
    # Reviewer repro S4.
    _manifest(config_dir).write_text(_WITH_ENV)
    env_block = f"env:\n        GITHUB_TOKEN: {_SECRET}\n        LOG_LEVEL: info\n"
    with capture_logs() as logs:
        proposal = _propose(service, env_block, "env: x\n")
    assert (
        '⚠ claude_code_settings.mcp_servers.gh.env: {"GITHUB_TOKEN": <hidden>, '
        '"LOG_LEVEL": <hidden>} → <hidden>' in proposal.summary
    )
    assert _SECRET not in proposal.summary
    assert _SECRET not in repr(logs)


def test_yaml_errors_do_not_quote_the_manifest_line(
    service: CapabilityEditService, config_dir: Path
) -> None:
    # PyYAML embeds the offending source line in str(exc); it may hold a secret.
    _manifest(config_dir).write_text(_WITH_ENV)
    err = _error(_propose, service, f"GITHUB_TOKEN: {_SECRET}\n", f'GITHUB_TOKEN: "{_SECRET}\n')
    assert err.status == STATUS_REJECTED_INVALID
    assert "YAML parse error" in err.reason and "line " in err.reason
    assert _SECRET not in err.reason


def test_unparsable_file_on_disk_does_not_quote_the_manifest_line(
    service: CapabilityEditService, config_dir: Path
) -> None:
    _manifest(config_dir).write_text(
        _WITH_ENV.replace(f"GITHUB_TOKEN: {_SECRET}", f'GITHUB_TOKEN: "{_SECRET}')
    )
    err = _error(service.read, "editable")
    assert err.status == STATUS_NOT_EDITABLE and "does not parse on disk" in err.reason
    assert _SECRET not in err.reason


# --- S5: unexpected parser / schema exceptions never escape -------------------------


@pytest.mark.parametrize("key", ["1", "~", "!!binary c2VsZl9lZGl0"])
def test_non_string_top_level_key_is_rejected_at_propose(
    service: CapabilityEditService, key: str
) -> None:
    # Reviewer repro S5: used to raise TypeError out of propose_edit.
    err = _error(_propose, service, "prompt: |", f"{key}: x\nprompt: |")
    assert err.status == STATUS_REJECTED_INVALID
    assert "top-level keys must be strings" in err.reason


def _boom_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    import assistant.core.capabilities.edit_service as module

    real = module.parse_capability_manifest

    def parse(file_name: str, raw: bytes) -> CapabilityDefinition:
        if b"# boom" in raw:
            raise TypeError("keywords must be strings")
        return real(file_name, raw)

    monkeypatch.setattr(module, "parse_capability_manifest", parse)


def test_unexpected_validation_error_is_rejected_at_propose(
    service: CapabilityEditService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boom_parser(monkeypatch)
    err = _error(_propose, service, "Be concise.", "Be concise. # boom")
    assert err.status == STATUS_REJECTED_INVALID
    assert "unexpected validation error (TypeError)" in err.reason


async def test_unexpected_validation_error_is_rejected_at_rollback_and_apply(
    service: CapabilityEditService, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _apply(service, _propose(service, "Be concise.", "Be brief."), "c1")
    history_id = service.history("editable")[0].history_id
    entry = config_dir / HISTORY_DIR_NAME / "editable"
    content = EDITABLE + "# boom\n"
    (entry / f"{history_id}.yaml").write_text(content)
    sidecar = json.loads((entry / f"{history_id}.json").read_text())
    sidecar["sha256"] = _sha(content)
    (entry / f"{history_id}.json").write_text(json.dumps(sidecar))
    proposal = _propose(service, "Be brief.", "Be brief. # boom")
    before = _manifest(config_dir).read_text()

    _boom_parser(monkeypatch)
    base = service.read("editable").base_sha256
    err = _error(service.propose_rollback, "editable", history_id, "undo", base)
    assert err.status == STATUS_REJECTED_INVALID and "TypeError" in err.reason
    outcome = await _apply(service, proposal, "c2")
    assert not outcome.ok and outcome.status == STATUS_REJECTED_INVALID
    assert "TypeError" in outcome.detail
    assert _manifest(config_dir).read_text() == before


async def test_non_string_key_in_a_tampered_payload_is_rejected_at_apply(
    service: CapabilityEditService, config_dir: Path
) -> None:
    proposal = _propose(service)
    new_text = EDITABLE.replace("prompt: |", "1: x\nprompt: |")
    payload = {
        **proposal.payload,
        "old_string": "prompt: |",
        "new_string": "1: x\nprompt: |",
        "new_sha256": _sha(new_text),
    }
    outcome = await service.apply(payload, confirmation_id="c1", session_id="s", user_id=None)
    assert not outcome.ok and outcome.status == STATUS_REJECTED_INVALID
    assert "top-level keys must be strings" in outcome.detail
    assert _manifest(config_dir).read_text() == EDITABLE


def test_unexpected_summary_error_is_rejected(
    service: CapabilityEditService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import assistant.core.capabilities.edit_service as module

    def broken(**_: Any) -> str:
        raise TypeError("bug")

    monkeypatch.setattr(module, "capability_change", broken)
    err = _error(_propose, service)
    assert err.status == STATUS_REJECTED_INVALID and "TypeError" in err.reason


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


async def test_apply_backs_up_writes_atomically_and_bumps_the_revision(
    service: CapabilityEditService, catalog: CapabilityCatalog, config_dir: Path
) -> None:
    path = _manifest(config_dir)
    os.chmod(path, 0o640)
    proposal = _propose(service)

    outcome = await _apply(service, proposal)

    assert outcome.ok and outcome.status == STATUS_APPLIED
    assert "revision 1 → 2" in outcome.detail
    assert (outcome.revision_before, outcome.revision_after) == (1, 2)
    expected = EDITABLE.replace("Be concise.", "Be concise and friendly.")
    assert path.read_text() == expected  # comments and formatting byte-identical
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert catalog.current().revision == 2
    assert catalog.current().get("editable").prompt.startswith("Be concise and friendly.")  # type: ignore[union-attr]

    files = _history_files(config_dir)
    assert len(files) == 2 and files[0].endswith(".json") and files[1].endswith(".yaml")
    backup = config_dir / HISTORY_DIR_NAME / "editable" / files[1]
    assert backup.read_text() == EDITABLE
    sidecar = json.loads((backup.with_suffix(".json")).read_text())
    assert sidecar["history_id"] == backup.stem
    assert sidecar["sha256"] == _sha(EDITABLE)
    assert sidecar["new_sha256"] == _sha(expected)
    assert sidecar["confirmation_id"] == "c1" and sidecar["session_id"] == "tg:1"
    assert sidecar["user_id"] == "u1" and sidecar["op"] == "edit"
    assert sidecar["base_revision"] == 1
    # No temp files left next to the manifest.
    assert sorted(p.name for p in path.parent.iterdir()) == [
        "editable.yaml",
        "flagfalse.yaml",
        "locked.yaml",
    ]


async def test_apply_logs_audit_events_with_correlation_ids(
    service: CapabilityEditService, config_dir: Path
) -> None:
    proposal = _propose(service)
    with capture_logs() as logs:
        await _apply(service, proposal, "c1")
        await _apply(service, proposal, "c2")  # same base again: stale

    applied = next(e for e in logs if e["event"] == "capability_edit.applied")
    assert applied["confirmation_id"] == "c1" and applied["session_id"] == "tg:1"
    assert applied["user_id"] == "u1" and applied["capability_id"] == "editable"
    assert (applied["revision_before"], applied["revision_after"]) == (1, 2)
    assert applied["base_sha256"] == _sha(EDITABLE) and applied["history_id"]
    failed = next(e for e in logs if e["event"] == "capability_edit.failed")
    assert failed["confirmation_id"] == "c2" and failed["status"] == STATUS_STALE
    reloaded = next(e for e in logs if e["event"] == "capabilities.reloaded")
    assert reloaded["reason"] == "capability_edit:c1"


async def test_history_is_never_loaded_as_manifests(
    service: CapabilityEditService, catalog: CapabilityCatalog
) -> None:
    await _apply(service, _propose(service))
    snapshot = catalog.reload("test")
    assert sorted(snapshot.definitions) == ["editable", "flagfalse", "locked"]
    assert not snapshot.errors


async def test_file_changed_between_proposal_and_approval_is_stale(
    service: CapabilityEditService, catalog: CapabilityCatalog, config_dir: Path
) -> None:
    proposal = _propose(service)
    changed = EDITABLE.replace("Answer in English.", "Answer in Greek.")
    _manifest(config_dir).write_text(changed)

    outcome = await _apply(service, proposal)

    assert not outcome.ok and outcome.status == STATUS_STALE
    assert "nothing was written" in outcome.detail
    assert _manifest(config_dir).read_text() == changed
    assert _history_files(config_dir) == []


async def test_self_edit_revoked_between_proposal_and_approval(
    service: CapabilityEditService, config_dir: Path
) -> None:
    proposal = _propose(service)
    revoked = EDITABLE.replace("self_edit: true", "self_edit: false")
    _manifest(config_dir).write_text(revoked)

    outcome = await _apply(service, proposal)

    assert not outcome.ok and outcome.status == STATUS_NOT_EDITABLE
    assert _manifest(config_dir).read_text() == revoked
    assert _history_files(config_dir) == []


async def test_backup_failure_writes_nothing(
    service: CapabilityEditService, config_dir: Path
) -> None:
    (config_dir / HISTORY_DIR_NAME).write_text("not a directory")
    outcome = await _apply(service, _propose(service))

    assert not outcome.ok and outcome.status == STATUS_BACKUP_FAILED
    assert _manifest(config_dir).read_text() == EDITABLE


async def test_crash_before_replace_leaves_the_original_intact(
    service: CapabilityEditService,
    catalog: CapabilityCatalog,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _propose(service)
    real_replace = os.replace

    def failing_replace(src: str, dst: str | Path) -> None:
        if Path(dst).name == "editable.yaml":
            raise OSError(5, "simulated crash")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    outcome = await _apply(service, proposal)

    assert not outcome.ok and outcome.status == STATUS_WRITE_FAILED
    assert _manifest(config_dir).read_text() == EDITABLE
    assert sorted(p.name for p in _manifest(config_dir).parent.iterdir()) == [
        "editable.yaml",
        "flagfalse.yaml",
        "locked.yaml",
    ]
    assert _history_files(config_dir) == []  # the backup of a failed write is removed
    assert catalog.current().revision == 1


async def test_reload_rejection_restores_the_previous_content(
    service: CapabilityEditService, catalog: CapabilityCatalog, config_dir: Path
) -> None:
    proposal = _propose(service)
    # Another manifest breaks after the proposal: the reload is rejected as a whole.
    (config_dir / "capabilities" / "broken.yaml").write_text("capability_id: [oops\n")

    outcome = await _apply(service, proposal)

    assert not outcome.ok and outcome.status == STATUS_RELOAD_REJECTED
    assert "restored" in outcome.detail
    assert _manifest(config_dir).read_text() == EDITABLE
    assert catalog.current().revision == 1
    assert _history_files(config_dir) == []


async def test_reload_rejection_does_not_quote_another_manifests_secret(
    service: CapabilityEditService, catalog: CapabilityCatalog, config_dir: Path
) -> None:
    # Reviewer repro S8: the detail quoted PyYAML's excerpt of a non-opt-in manifest.
    from assistant.core.confirmation import CapabilityEditHandler
    from assistant.core.confirmation.models import ConfirmationRequest

    secret = "ghpLEAKEDSECRET"
    proposal = _propose(service)
    other = (
        "capability_id: other\nclaude_code_settings:\n  mcp_servers:\n    gh:\n"
        f"      command: x\n      env:\n        TOKEN: {secret}: [\n"
    )
    (config_dir / "capabilities" / "other.yaml").write_text(other)
    request = ConfirmationRequest.model_construct(
        confirmation_id="c1", session_id="s", payload=proposal.payload
    )

    with capture_logs() as logs:
        outcome = await CapabilityEditHandler(service).apply(request, user_id=None)

    assert not outcome.ok and outcome.status == STATUS_RELOAD_REJECTED
    assert "[other.yaml] YAML parse error: ScannerError (line 7, column" in outcome.detail
    assert secret not in outcome.detail and "TOKEN" not in outcome.detail
    assert secret not in repr(logs)
    assert all(secret not in e.message for e in catalog.current().errors)
    assert _manifest(config_dir).read_text() == EDITABLE


async def test_concurrent_approvals_on_the_same_base_are_serialized(
    service: CapabilityEditService, config_dir: Path
) -> None:
    first = _propose(service, "Be concise.", "Be brief.")
    second = _propose(service, "Answer in English.", "Answer in Greek.")

    outcomes = await asyncio.gather(_apply(service, first, "c1"), _apply(service, second, "c2"))

    assert [o.status for o in outcomes] == [STATUS_APPLIED, STATUS_STALE]
    assert _manifest(config_dir).read_text() == EDITABLE.replace("Be concise.", "Be brief.")


async def test_tampered_payload_is_not_applied(
    service: CapabilityEditService, config_dir: Path
) -> None:
    proposal = _propose(service)
    payload = {**proposal.payload, "new_string": "Something else."}
    outcome = await service.apply(payload, confirmation_id="c1", session_id="s", user_id=None)

    assert not outcome.ok and outcome.status == STATUS_REJECTED_INVALID
    assert _manifest(config_dir).read_text() == EDITABLE


# ---------------------------------------------------------------------------
# history and rollback
# ---------------------------------------------------------------------------


async def test_history_lists_newest_first(service: CapabilityEditService) -> None:
    await _apply(service, _propose(service, "Be concise.", "Be brief."), "c1")
    await _apply(service, _propose(service, "Be brief.", "Be short."), "c2")

    entries = service.history("editable")

    assert [e.confirmation_id for e in entries] == ["c2", "c1"]
    assert entries[1].sha256 == _sha(EDITABLE)
    assert all(e.op == "edit" and e.reason for e in entries)
    assert len(service.history("editable", limit=1)) == 1


async def test_rollback_restores_a_version_and_backs_up_the_current_one(
    service: CapabilityEditService, catalog: CapabilityCatalog, config_dir: Path
) -> None:
    await _apply(service, _propose(service, "Be concise.", "Be brief."), "c1")
    edited = _manifest(config_dir).read_text()
    original_id = service.history("editable")[0].history_id

    view = service.read("editable")
    proposal = service.propose_rollback("editable", original_id, "undo", view.base_sha256)
    assert f"rollback to {original_id}" in proposal.summary
    assert proposal.payload["op"] == "rollback"
    assert _manifest(config_dir).read_text() == edited  # proposing writes nothing

    outcome = await _apply(service, proposal, "c2")

    assert outcome.ok, outcome.detail
    assert _manifest(config_dir).read_text() == EDITABLE
    assert catalog.current().revision == 3
    newest = service.history("editable")[0]
    assert newest.op == "rollback" and newest.sha256 == _sha(edited)

    # Rollback of the rollback brings the edit back.
    again = service.propose_rollback(
        "editable", newest.history_id, "redo", service.read("editable").base_sha256
    )
    assert (await _apply(service, again, "c3")).ok
    assert _manifest(config_dir).read_text() == edited


async def test_rollback_validation(service: CapabilityEditService, config_dir: Path) -> None:
    await _apply(service, _propose(service, "Be concise.", "Be brief."), "c1")
    base = service.read("editable").base_sha256
    history_id = service.history("editable")[0].history_id

    for bad_id in ("../../editable", "20260101T000000000000Z__deadbeef"):
        err = _error(service.propose_rollback, "editable", bad_id, "undo", base)
        assert err.status == STATUS_REJECTED_INVALID

    err = _error(service.propose_rollback, "editable", history_id, "undo", "0" * 64)
    assert err.status == STATUS_STALE

    backup = config_dir / HISTORY_DIR_NAME / "editable" / f"{history_id}.yaml"
    backup.write_text(EDITABLE + "# tampered\n")
    err = _error(service.propose_rollback, "editable", history_id, "undo", base)
    assert err.status == STATUS_REJECTED_INVALID and "corrupted" in err.reason


async def test_rollback_to_a_version_without_self_edit_is_rejected(
    service: CapabilityEditService, config_dir: Path
) -> None:
    await _apply(service, _propose(service, "Be concise.", "Be brief."), "c1")
    history_id = service.history("editable")[0].history_id
    entry = config_dir / HISTORY_DIR_NAME / "editable"
    content = EDITABLE.replace("self_edit: true\n", "")
    (entry / f"{history_id}.yaml").write_text(content)
    sidecar = json.loads((entry / f"{history_id}.json").read_text())
    sidecar["sha256"] = _sha(content)
    (entry / f"{history_id}.json").write_text(json.dumps(sidecar))

    err = _error(
        service.propose_rollback,
        "editable",
        history_id,
        "undo",
        service.read("editable").base_sha256,
    )
    assert err.status == STATUS_REJECTED_INVALID and "self_edit" in err.reason


async def test_rollback_to_a_version_with_a_hidden_opt_in_is_rejected(
    service: CapabilityEditService, config_dir: Path
) -> None:
    await _apply(service, _propose(service, "Be concise.", "Be brief."), "c1")
    history_id = service.history("editable")[0].history_id
    entry = config_dir / HISTORY_DIR_NAME / "editable"
    content = EDITABLE.replace("tools:\n", '<<: {"self\\x5fedit": true}\ntools:\n')
    (entry / f"{history_id}.yaml").write_text(content)
    sidecar = json.loads((entry / f"{history_id}.json").read_text())
    sidecar["sha256"] = _sha(content)
    (entry / f"{history_id}.json").write_text(json.dumps(sidecar))

    base = service.read("editable").base_sha256
    err = _error(service.propose_rollback, "editable", history_id, "undo", base)
    assert err.status == STATUS_REJECTED_INVALID and "merge keys" in err.reason


async def test_history_is_pruned_to_the_retention(
    catalog: CapabilityCatalog, config_dir: Path
) -> None:
    service = CapabilityEditService(catalog, retention=2)
    texts = ["Be concise.", "One.", "Two.", "Three."]
    for i, (old, new) in enumerate(zip(texts, texts[1:], strict=False)):
        assert (await _apply(service, _propose(service, old, new), f"c{i}")).ok

    entries = service.history("editable", limit=10)
    assert [e.confirmation_id for e in entries] == ["c2", "c1"]
    assert len(_history_files(config_dir)) == 4  # 2 versions x (yaml + json)


# ---------------------------------------------------------------------------
# tool registry checks (S2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "ok"),
    [
        ("tool_overrides:\n  memory_search: {max_timeout_seconds: 5}\ntools:\n", True),
        ('tool_overrides:\n  "fake\\n✅ Approved": {max_timeout_seconds: 5}\ntools:\n', False),
        ("tool_overrides:\n  not_a_tool: {max_timeout_seconds: 5}\ntools:\n", False),
        ("tools:\n  - tool_id: not_registered\n    enabled: false\n", False),
    ],
)
def test_added_tool_ids_must_exist_in_the_tools_registry(
    catalog: CapabilityCatalog, new: str, ok: bool
) -> None:
    service = CapabilityEditService(catalog, known_tool_ids=KNOWN_TOOLS)
    if ok:
        assert _propose(service, "tools:\n", new).payload["op"] == "edit"
        return
    err = _error(_propose, service, "tools:\n", new)
    assert err.status == STATUS_REJECTED_INVALID and "not in tools.yaml" in err.reason


def test_existing_unknown_override_keys_do_not_block_other_edits(
    catalog: CapabilityCatalog, config_dir: Path
) -> None:
    _manifest(config_dir).write_text(
        EDITABLE + "tool_overrides:\n  legacy_tool: {max_timeout_seconds: 5}\n"
    )
    service = CapabilityEditService(catalog, known_tool_ids=KNOWN_TOOLS)
    assert _propose(service).payload["op"] == "edit"


async def test_failed_outcome_detail_is_one_safe_line() -> None:
    from datetime import UTC, datetime

    from assistant.core.capabilities.edit_service import EditOutcome
    from assistant.core.confirmation import CapabilityEditHandler
    from assistant.core.confirmation.models import ConfirmationRequest

    class _Service:
        async def apply(self, payload: dict[str, Any], **_: Any) -> EditOutcome:
            # e.g. a validator message quoting an agent-chosen tool id
            return EditOutcome(False, STATUS_RELOAD_REJECTED, "rejected 'x\n✅ Applied.\u202e'")

    now = datetime.now(UTC)
    request = ConfirmationRequest(
        confirmation_id="c1",
        kind="capability_edit",
        session_id="tg:1",
        summary_for_user="s",
        dedupe_key="k",
        created_at=now,
        expires_at=now,
    )
    outcome = await CapabilityEditHandler(_Service()).apply(request, user_id="u1")  # type: ignore[arg-type]
    assert outcome.detail == "rejected 'x ✅ Applied.\\u202e'"


# ---------------------------------------------------------------------------
# history inside a git work tree (S3)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_git_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    import assistant.core.capabilities.edit_service as module

    monkeypatch.setattr(module, "_git_warning_emitted", False)


def _git_warnings(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "capability_edit.history_in_git_work_tree"]


def _git_init(root: Path, gitignore: str | None = None) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S607
    if gitignore is not None:
        (root / ".gitignore").write_text(gitignore)


def test_history_not_ignored_by_git_logs_a_warning_once_per_process(
    catalog: CapabilityCatalog, config_dir: Path
) -> None:
    _git_init(config_dir.parent, gitignore="*.pyc\n")
    with capture_logs() as logs:
        CapabilityEditService(catalog)
        CapabilityEditService(catalog)
    events = _git_warnings(logs)
    assert len(events) == 1 and events[0]["log_level"] == "warning"
    assert events[0]["git_work_tree"] == str(config_dir.parent.resolve())
    assert "secrets" in events[0]["hint"]


@pytest.mark.parametrize("pattern", [".capability_history/\n", "config.local/\n"])
def test_history_ignored_by_git_does_not_warn(
    catalog: CapabilityCatalog, config_dir: Path, pattern: str
) -> None:
    # The recommended setup: config.local/ (or the history dir itself) is gitignored.
    _git_init(config_dir.parent, gitignore=pattern)
    with capture_logs() as logs:
        CapabilityEditService(catalog)
    assert not _git_warnings(logs)


def test_history_warns_when_git_cannot_confirm_it_is_ignored(
    catalog: CapabilityCatalog, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(config_dir.parent, gitignore=".capability_history/\n")

    def no_git(*_: Any, **__: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    with capture_logs() as logs:
        CapabilityEditService(catalog)
    assert len(_git_warnings(logs)) == 1


def test_bare_git_marker_without_a_repository_still_warns(
    catalog: CapabilityCatalog, config_dir: Path
) -> None:
    (config_dir.parent / ".git").mkdir()  # git check-ignore fails: not a repository
    with capture_logs() as logs:
        CapabilityEditService(catalog)
    assert len(_git_warnings(logs)) == 1


def test_history_outside_a_git_work_tree_does_not_warn(
    catalog: CapabilityCatalog, config_dir: Path
) -> None:
    if find_git_work_tree(config_dir) is not None:
        pytest.skip("the test tmp dir is inside a git work tree")
    with capture_logs() as logs:
        CapabilityEditService(catalog)
    assert not _git_warnings(logs)


def test_repo_gitignore_covers_the_history_dir() -> None:
    gitignore = Path(__file__).resolve().parents[4] / ".gitignore"
    assert f"{HISTORY_DIR_NAME}/" in gitignore.read_text().splitlines()


# ---------------------------------------------------------------------------
# S6: deep nesting, huge inputs and unexpected exceptions never escape
# ---------------------------------------------------------------------------


def _deep(style: str, depth: int) -> str:
    if style == "flow_list":
        return "x: " + "[" * depth + "]" * depth + "\n"
    if style == "flow_map":
        return "x: " + "{a: " * depth + "1" + "}" * depth + "\n"
    lines = ["x:"] + [f"{'  ' * (i + 1)}a:" for i in range(depth)]
    return "\n".join(lines) + " 1\n"


_STYLES = ["flow_list", "flow_map", "block_map"]
_DEPTHS = [100, 500, 1000, 5000]


@pytest.mark.parametrize("depth", _DEPTHS)
@pytest.mark.parametrize("style", _STYLES)
def test_deeply_nested_edit_is_rejected_invalid(
    service: CapabilityEditService, style: str, depth: int
) -> None:
    err = _error(_propose, service, "tools:\n", _deep(style, depth) + "tools:\n")
    assert err.status == STATUS_REJECTED_INVALID
    # Deep block nesting also exceeds the size bound (indentation grows quadratically).
    assert "deeper than 32 levels" in err.reason or "bytes allowed" in err.reason


@pytest.mark.parametrize("depth", _DEPTHS)
async def test_deeply_nested_rollback_and_apply_are_rejected_invalid(
    service: CapabilityEditService, config_dir: Path, depth: int
) -> None:
    await _apply(service, _propose(service, "Be concise.", "Be brief."), "c1")
    history_id = service.history("editable")[0].history_id
    entry = config_dir / HISTORY_DIR_NAME / "editable"
    content = EDITABLE + _deep("flow_list", depth)
    (entry / f"{history_id}.yaml").write_text(content)
    sidecar = json.loads((entry / f"{history_id}.json").read_text())
    sidecar["sha256"] = _sha(content)
    (entry / f"{history_id}.json").write_text(json.dumps(sidecar))
    base = service.read("editable").base_sha256
    err = _error(service.propose_rollback, "editable", history_id, "undo", base)
    assert err.status == STATUS_REJECTED_INVALID and "deeper than" in err.reason

    before = _manifest(config_dir).read_text()
    proposal = _propose(service, "Be brief.", "Be short.")
    new_string = "Be short.\n" + _deep("block_map", depth)
    payload = {
        **proposal.payload,
        "new_string": new_string,
        "new_sha256": _sha(before.replace("Be brief.", new_string)),
    }
    outcome = await service.apply(payload, confirmation_id="c2", session_id="s", user_id=None)
    assert not outcome.ok and outcome.status == STATUS_REJECTED_INVALID
    assert "deeper than" in outcome.detail or "bytes allowed" in outcome.detail
    assert _manifest(config_dir).read_text() == before


@pytest.mark.parametrize("depth", _DEPTHS)
def test_deeply_nested_manifest_on_disk_is_not_editable(
    service: CapabilityEditService, config_dir: Path, depth: int
) -> None:
    _manifest(config_dir).write_text(EDITABLE + _deep("flow_map", depth))
    err = _error(service.read, "editable")
    assert err.status in (STATUS_NOT_EDITABLE, STATUS_STALE)
    assert "editable" not in service.editable_ids()


def test_huge_edit_is_rejected_invalid(service: CapabilityEditService) -> None:
    from assistant.core.capabilities.manifest_structure import MAX_MANIFEST_BYTES

    huge = "Be concise. " + "x" * (MAX_MANIFEST_BYTES + 1)
    err = _error(_propose, service, "Be concise.", huge)
    assert err.status == STATUS_REJECTED_INVALID
    assert f"more than the {MAX_MANIFEST_BYTES} bytes" in err.reason


def test_huge_manifest_on_disk_is_not_editable(
    service: CapabilityEditService, config_dir: Path
) -> None:
    _manifest(config_dir).write_text(EDITABLE + "# " + "x" * (300 * 1024) + "\n")
    err = _error(service.read, "editable")
    assert err.status == STATUS_NOT_EDITABLE
    # Refused on the raw size, before the file is parsed at all.
    assert err.reason.startswith("editable.yaml is ") and "bytes allowed" in err.reason


async def test_unexpected_exceptions_become_rejected_invalid_without_their_message(
    service: CapabilityEditService, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _apply(service, _propose(service, "Be concise.", "Be brief."), "c1")
    history_id = service.history("editable")[0].history_id
    base = service.read("editable").base_sha256
    proposal = _propose(service, "Be brief.", "Be brief and kind.")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RecursionError(f"maximum recursion depth exceeded near {_SECRET}")

    monkeypatch.setattr(CapabilityEditService, "_validate_result", boom)
    with capture_logs() as logs:
        edit = _error(_propose, service, "Be brief.", "Be short.")
        rollback = _error(service.propose_rollback, "editable", history_id, "undo", base)
        outcome = await _apply(service, proposal, "c2")
    for reason in (edit.reason, rollback.reason, outcome.detail):
        assert "RecursionError" in reason and _SECRET not in reason
    assert edit.status == rollback.status == outcome.status == STATUS_REJECTED_INVALID
    assert _SECRET not in repr(logs)


async def test_unexpected_exception_after_validation_is_a_failed_outcome(
    service: CapabilityEditService, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = _propose(service)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"bug near {_SECRET}")

    monkeypatch.setattr(CapabilityEditService, "_write_backup", boom)
    outcome = await _apply(service, proposal)
    assert not outcome.ok and outcome.status == STATUS_REJECTED_INVALID
    assert "RuntimeError" in outcome.detail and _SECRET not in outcome.detail
    assert _manifest(config_dir).read_text() == EDITABLE


# ---------------------------------------------------------------------------
# S9: full prompt parts; part cap
# ---------------------------------------------------------------------------


def test_prompt_change_needing_more_parts_than_allowed_is_rejected_invalid(
    catalog: CapabilityCatalog, config_dir: Path
) -> None:
    long_old = "\n".join(f"  Rule {i}: answer briefly." for i in range(300))
    _manifest(config_dir).write_text(EDITABLE.replace("  Be concise.", long_old))
    catalog.reload("test")
    service = CapabilityEditService(catalog, max_prompt_parts=2)
    base = service.read("editable").base_sha256
    long_new = long_old.replace("briefly", "in detail")
    err = _error(service.propose_edit, "editable", long_old, long_new, "r", base)
    assert err.status == STATUS_REJECTED_INVALID
    assert "more than the 2 allowed" in err.reason and "smaller prompt edit" in err.reason
    proposal = CapabilityEditService(catalog, max_prompt_parts=8).propose_edit(
        "editable", long_old, long_new, "r", base
    )
    assert 2 < len(proposal.prompt_parts) <= 8
    assert f"see {len(proposal.prompt_parts)} attached parts" in proposal.summary


def test_rollback_proposal_carries_the_prompt_parts(
    service: CapabilityEditService,
) -> None:
    asyncio.run(_apply(service, _propose(service), "c1"))
    history_id = service.history("editable")[0].history_id
    base = service.read("editable").base_sha256
    proposal = service.propose_rollback("editable", history_id, "undo", base)
    assert proposal.prompt_parts == (
        "@@ -1,3 +1,3 @@\n- Be concise and friendly.\n+ Be concise.\n  Answer in English.\n  ",
    )


def test_max_prompt_parts_must_be_positive(catalog: CapabilityCatalog) -> None:
    with pytest.raises(ValueError, match="max_prompt_parts"):
        CapabilityEditService(catalog, max_prompt_parts=0)


# ---------------------------------------------------------------------------
# S11: YAML alias graphs never expand (catalog load and all read paths)
# ---------------------------------------------------------------------------


def _alias_chain(levels: int, refs: int = 10) -> str:
    """Reviewer repro S11: < 700 bytes, ``refs ** levels`` values once expanded."""
    lines = [
        "capability_id: editable",
        "self_edit: true",
        "prompt: hi",
        "claude_code_settings:",
        "  mcp_servers:",
        "    s:",
        "      x0: &a0 [lol]",
    ]
    lines += [
        f"      x{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * refs) + "]"
        for i in range(1, levels + 1)
    ]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("levels", [7, 10])
def test_alias_chain_is_rejected_at_parse_without_expanding(levels: int) -> None:
    text = _alias_chain(levels)
    assert len(text.encode()) < 800
    start = time.perf_counter()
    with pytest.raises(ManifestParseError) as info:
        parse_capability_manifest("editable.yaml", text.encode())
    assert time.perf_counter() - start < 1.0
    assert "more than 100000 values once YAML aliases are expanded" in info.value.messages[0]


def test_wide_alias_fan_out_is_checked_once_per_distinct_node() -> None:
    # Without identity memoisation the bound alone would still visit fan-out x 100 000
    # nodes (here 400 x 99 000) before refusing; with it, each container is walked once.
    lines = [
        "capability_id: editable",
        "self_edit: true",
        "prompt: hi",
        "claude_code_settings:",
        "  mcp_servers:",
        "    s:",
        "      x1: &a1 [" + ", ".join(["1"] * 990) + "]",
        "      x2: &a2 [" + ", ".join(["*a1"] * 99) + "]",
        "      x3: [" + ", ".join(["*a2"] * 400) + "]",
    ]
    start = time.perf_counter()
    with pytest.raises(ManifestParseError) as info:
        parse_capability_manifest("editable.yaml", ("\n".join(lines) + "\n").encode())
    assert time.perf_counter() - start < 1.0
    assert "s.x3: more than 100000 values" in info.value.messages[0]


def test_small_alias_use_still_loads() -> None:
    definition = parse_capability_manifest("editable.yaml", _alias_chain(3).encode())
    settings = definition.claude_code_settings
    assert settings is not None and len(settings.mcp_servers["s"]["x3"]) == 10


def test_recursive_alias_is_rejected() -> None:
    text = (
        "capability_id: editable\nself_edit: true\nprompt: hi\nclaude_code_settings:\n"
        "  mcp_servers:\n    s:\n      x: &a [1, {y: *a}]\n"
    )
    start = time.perf_counter()
    with pytest.raises(ManifestParseError) as info:
        parse_capability_manifest("editable.yaml", text.encode())
    assert time.perf_counter() - start < 1.0
    assert "refers to itself" in info.value.messages[0]


@pytest.mark.parametrize("levels", [3, 10])
async def test_alias_chain_on_disk_keeps_reload_and_all_four_tools_prompt(
    service: CapabilityEditService, catalog: CapabilityCatalog, config_dir: Path, levels: int
) -> None:
    from unittest.mock import MagicMock

    from assistant.agent.tools.capability_edit import (
        capability_history,
        capability_propose_edit,
        capability_propose_rollback,
        capability_read,
    )
    from assistant.agent.tools.deps import TurnDeps

    await _apply(service, _propose(service), "c1")  # a history entry for the rollback
    history_id = service.history("editable")[0].history_id
    _manifest(config_dir).write_text(_alias_chain(levels))
    ctx = MagicMock()
    ctx.tool_call_id, ctx.tool_name = "tc", "capability_propose_edit"

    async def requester(_draft: Any) -> Any:
        raise AssertionError("nothing may be proposed")

    ctx.deps = TurnDeps(confirmation_requester=requester, capability_edit_service=service)
    start = time.perf_counter()
    catalog.reload("test")
    results = [
        capability_read(ctx, "editable"),
        capability_history(ctx, "editable"),
        await capability_propose_edit(
            ctx,
            capability_id="editable",
            old_string="hi",
            new_string="ho",
            reason="r",
            base_sha256="0" * 64,
        ),
        await capability_propose_rollback(
            ctx, capability_id="editable", history_id=history_id, reason="r", base_sha256="0" * 64
        ),
    ]
    assert time.perf_counter() - start < 2.0
    read, history, edit, rollback = results
    assert history["status"] == "ok"
    for result in (read, edit, rollback):
        assert result["status"] == STATUS_NOT_EDITABLE
        # 3 levels parse (and are refused by the alias rule); 10 levels fail the bound.
        assert ("YAML aliases" in result["reason"]) or ("once YAML aliases" in result["reason"])
