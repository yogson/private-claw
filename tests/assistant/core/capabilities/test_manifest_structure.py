"""check_manifest_structure: node-tree guard of the protected keys (one test per layer).

Each probe is caught first by exactly one layer and asserts that layer's message, so
removing any single layer makes its test fail.
"""

import pytest

from assistant.core.capabilities.manifest_structure import (
    ManifestStructureError,
    check_manifest_structure,
)

BASE = "capability_id: ed\nself_edit: true\nprompt: hello\n"


def _refused(text: str) -> str:
    with pytest.raises(ManifestStructureError) as info:
        check_manifest_structure(text)
    return str(info.value)


def test_valid_manifest_passes() -> None:
    structure = check_manifest_structure(BASE)
    assert structure.self_edit == "true"
    assert structure.capability_id == "ed"
    assert structure.top_level_keys == ("capability_id", "self_edit", "prompt")


@pytest.mark.parametrize("spelling", ["true", "True", "TRUE"])
def test_true_spellings_pass(spelling: str) -> None:
    assert check_manifest_structure(BASE.replace("true", spelling)).self_edit == spelling


# --- document shape ---------------------------------------------------------


def test_yaml_error_is_refused() -> None:
    assert "YAML parse error" in _refused(BASE + "tools: [unclosed\n")


def test_second_document_is_refused() -> None:
    assert "YAML parse error" in _refused(BASE + "---\nself_edit: true\n")


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string\n"])
def test_non_mapping_root_is_refused(text: str) -> None:
    assert "must be a YAML mapping" in _refused(text)


# --- merge keys (reviewer repro B1) -----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        BASE + '<<: {"self\\x5fedit": true}\n',  # reviewer repro
        BASE + "<<: {prompt: x}\n",
        BASE + "tools:\n  - <<: {tool_id: x}\n    enabled: true\n",  # nested
        BASE + '"<<": 1\n',  # quoted: not a merge, but still refused
    ],
)
def test_merge_keys_are_refused(text: str) -> None:
    assert "merge keys" in _refused(text)


# --- aliases ----------------------------------------------------------------


def test_aliases_are_refused() -> None:
    text = "capability_id: ed\nself_edit: &t true\nprompt: hello\nextra: *t\n"
    assert "aliases" in _refused(text)


def test_alias_as_key_is_refused() -> None:
    assert "aliases" in _refused(BASE + "x: &k self_edit\n*k : true\n")


# --- non-scalar keys --------------------------------------------------------


def test_complex_keys_are_refused() -> None:
    assert "plain scalars" in _refused(BASE + "? [self_edit]\n: true\n")


# --- duplicate keys (reviewer repro B1) -------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'capability_id: ed\n"self\\x5fedit": true\nself_edit: true\n',  # reviewer repro
        "capability_id: ed\nself_edit: true\nself_edit: true\n",
        "capability_id: ed\nself_edit: true\n'self_edit': true\n",  # quoting variant
        'capability_id: ed\nself_edit: true\n"self_edit": false\n',
        "capability_id: ed\nself_edit: true\nSelf_Edit: true\n",  # case variant
        "capability_id: ed\nself_edit: true\nｓｅｌｆ_ｅｄｉｔ: true\n",  # fullwidth (NFKC)
        "capability_id: ed\ncapability_id: other\nself_edit: true\n",
        BASE + "prompt: again\n",  # any duplicate top-level key
        BASE + "tools:\n  - tool_id: a\n    tool_id: b\n",  # nested duplicates too
    ],
)
def test_duplicate_keys_are_refused(text: str) -> None:
    assert "duplicate key" in _refused(text)


# --- protected keys imitated --------------------------------------------------


@pytest.mark.parametrize("key", ["Self_Edit", "SELF_EDIT", "ｓｅｌｆ_ｅｄｉｔ", "self_Edit"])
def test_lookalike_self_edit_keys_are_refused(key: str) -> None:
    message = _refused(f"capability_id: ed\nprompt: x\n{key}: true\n")
    assert "imitates 'self_edit'" in message


def test_lookalike_capability_id_key_is_refused() -> None:
    assert "imitates 'capability_id'" in _refused("self_edit: true\nCapability_ID: ed\n")


# --- exactly one protected key ----------------------------------------------


def test_missing_self_edit_is_refused() -> None:
    message = _refused("capability_id: ed\nprompt: hello\n")
    assert "exactly one top-level 'self_edit'" in message and "found 0" in message


def test_missing_capability_id_is_refused() -> None:
    message = _refused("self_edit: true\nprompt: hello\n")
    assert "exactly one top-level 'capability_id'" in message


def test_self_edit_nested_only_does_not_count() -> None:
    message = _refused("capability_id: ed\nprompt: hello\nextra:\n  self_edit: true\n")
    assert "found 0" in message


# --- strict self_edit value ---------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ['"true"', "'true'", "yes", "on", "y", "1", '"yes"', '!!bool "true"', "false", "[true]"],
)
def test_self_edit_must_be_the_plain_boolean_true(value: str) -> None:
    message = _refused(f"capability_id: ed\nself_edit: {value}\nprompt: hello\n")
    assert "plain YAML boolean true" in message


@pytest.mark.parametrize("value", ["[ed]", "{a: b}", "1", "null"])
def test_capability_id_must_be_a_string_scalar(value: str) -> None:
    message = _refused(f"capability_id: {value}\nself_edit: true\n")
    assert "capability_id must be a plain string" in message
