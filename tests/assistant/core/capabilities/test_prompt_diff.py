"""Full prompt-change parts of a ``capability_edit`` (review S9 / S10 of #64).

The prompt diff is never truncated: every added and removed line is in the parts, the
parts respect Telegram's UTF-16 limit, and a change that needs more parts than allowed
is refused. The diff is near-linear and bounded (no ``difflib.ndiff``).
"""

import time

import pytest

from assistant.core.capabilities import prompt_diff as prompt_diff_module
from assistant.core.capabilities.edit_summary import (
    PROMPT_PART_MAX_UNITS,
    PromptChangeTooLargeError,
    summarize_change,
)
from assistant.core.capabilities.prompt_diff import (
    MAX_PROMPT_LINES,
    PromptDiffTooLargeError,
    prompt_diff,
    split_parts,
)
from assistant.core.capabilities.text_safety import utf16_len
from assistant.core.confirmation import ATTACHMENT_PART_MAX_UTF16, ATTACHMENT_TITLE_MAX_UTF16


def _change(old: str, new: str, **kw: object) -> tuple[str, tuple[str, ...]]:
    result = summarize_change(
        capability_id="cap",
        file_name="cap.yaml",
        reason="r",
        old={"capability_id": "cap", "prompt": old},
        new={"capability_id": "cap", "prompt": new},
        **kw,  # type: ignore[arg-type]
    )
    return result.text, result.prompt_parts


def _content(parts: tuple[str, ...] | list[str]) -> list[str]:
    """Diff lines of the parts with long-line continuations joined back."""
    lines: list[str] = []
    for line in "\n".join(parts).split("\n"):
        if len(line) >= 2 and line[1] == "…" and lines:
            lines[-1] += line[2:]
        else:
            lines.append(line)
    return lines


def test_every_added_and_removed_line_is_in_full_with_context() -> None:
    old = "\n".join(f"line {i}" for i in range(20))
    new = old.replace("line 10", "line ten, changed").replace("line 19", "")
    diff = prompt_diff(old, new)
    assert diff.lines == (
        "@@ -8,7 +8,7 @@",
        "  line 7",
        "  line 8",
        "  line 9",
        "- line 10",
        "+ line ten, changed",
        "  line 11",
        "  line 12",
        "  line 13",
        "@@ -17,4 +17,4 @@",
        "  line 16",
        "  line 17",
        "  line 18",
        "- line 19",
        "+ ",
    )
    assert (diff.added, diff.removed) == (2, 2)


def test_hunks_match_difflib_unified_diff() -> None:
    import difflib

    old = "\n".join(f"l{i % 7}" for i in range(300))
    new = "\n".join(f"l{i % 7}" if i % 13 else f"X{i}" for i in range(310))
    expected = [
        line.rstrip("\n")
        for line in difflib.unified_diff(
            [x + "\n" for x in old.split("\n")], [x + "\n" for x in new.split("\n")], n=3
        )
    ][2:]
    rendered = [
        line if line.startswith("@@") else line[0] + line[2:]
        for line in prompt_diff(old, new).lines
    ]
    assert rendered == expected


def test_distant_changes_are_separate_hunks() -> None:
    old = "\n".join(f"l{i}" for i in range(40))
    new = old.replace("l2\n", "L2\n").replace("l35", "L35")
    lines = prompt_diff(old, new).lines
    assert [line for line in lines if line.startswith("@@")] == [
        "@@ -1,6 +1,6 @@",
        "@@ -33,7 +33,7 @@",
    ]


@pytest.mark.parametrize(("old", "new", "marker"), [("", "a\nb\n\nc", "+"), ("a\nb\n\nc", "", "-")])
def test_added_or_removed_prompt_is_shown_whole(old: str, new: str, marker: str) -> None:
    text, parts = _change(old, new)
    span = "-0,0 +1,4" if marker == "+" else "-1,4 +0,0"
    assert parts == (f"@@ {span} @@\n{marker} a\n{marker} b\n{marker} \n{marker} c",)
    assert "see 1 attached part)" in text


def test_content_lines_cannot_forge_a_hunk_header() -> None:
    lines = prompt_diff("a", "@@ -1,1 +1,1 @@\n  fake context").lines
    assert lines[0] == "@@ -1,1 +1,2 @@"
    assert lines[1:] == ("- a", "+ @@ -1,1 +1,1 @@", "+   fake context")


def test_sanitiser_is_applied_but_ordinary_non_ascii_is_kept() -> None:
    new = "Не удаляй‮файлы​\tтабы\r ㅤ 🧠 «ok»"
    (line,) = [x for x in prompt_diff("", new).lines if x.startswith("+")]
    assert line == ("+ Не удаляй\\u202eфайлы\\u200b\\u0009табы\\u000d\\u2028\\u3164 🧠 «ok»")


def test_no_truncation_anywhere_for_a_large_prompt_change() -> None:
    old = "\n".join(f"Старое правило {i}: будь вежлив 🧠" for i in range(150))
    new = "\n".join(f"Новое правило {i}: будь краток ✅" for i in range(150))
    text, parts = _change(old, new, max_prompt_parts=50)
    assert "правило" not in text and "…" not in text and "truncated" not in text
    content = _content(parts)
    for i in range(150):
        assert f"- Старое правило {i}: будь вежлив 🧠" in content
        assert f"+ Новое правило {i}: будь краток ✅" in content
    assert all(utf16_len(p) <= PROMPT_PART_MAX_UNITS for p in parts)
    assert f"see {len(parts)} attached parts" in text


@pytest.mark.parametrize("char", ["я", "🧠"])
def test_parts_split_exactly_at_the_utf16_boundary(char: str) -> None:
    width = utf16_len(char)
    # Two lines that exactly fill one part ("+ " prefix, one "\n"), then one more line.
    body = (PROMPT_PART_MAX_UNITS - 1) // 2 - 2
    line = "+ " + char * (body // width)
    lines = [line, line, "+ x"]
    used = 2 * utf16_len(line) + 1
    parts = split_parts(lines, used)
    assert parts == ["\n".join(lines[:2]), "+ x"]
    assert utf16_len(parts[0]) == used
    assert split_parts(lines, used - 1) == [line, line + "\n+ x"]


@pytest.mark.parametrize("char", ["я", "🧠"])
def test_a_line_longer_than_a_part_is_split_without_breaking_characters(char: str) -> None:
    line = "+ " + char * 9000
    parts = split_parts(["@@ -0,0 +1,1 @@", line], 1000)
    assert all(utf16_len(p) <= 1000 for p in parts)
    assert all("\ud83e" not in p for p in parts)  # no lone surrogate halves
    assert parts[1].startswith("+…") and parts[-1].startswith("+…")
    assert parts[0].startswith("@@ -0,0 +1,1 @@\n+ " + char)  # the long line fills part 1
    assert _content(parts) == ["@@ -0,0 +1,1 @@", line]


def test_part_cap_rejects_instead_of_truncating() -> None:
    old = "\n".join(f"rule {i}" for i in range(400))
    new = "\n".join(f"RULE {i} changed" for i in range(400))
    _, parts = _change(old, new, max_prompt_parts=100)
    needed = len(parts)
    assert needed > 1
    _change(old, new, max_prompt_parts=needed)
    with pytest.raises(PromptChangeTooLargeError, match=f"needs {needed} messages"):
        _change(old, new, max_prompt_parts=needed - 1)


def test_part_and_title_bounds_fit_one_telegram_message() -> None:
    assert PROMPT_PART_MAX_UNITS <= ATTACHMENT_PART_MAX_UTF16
    header = "x" * ATTACHMENT_TITLE_MAX_UTF16 + " — part 99/99 (confirmation 0123abcd)\n\n"
    assert utf16_len(header) + ATTACHMENT_PART_MAX_UTF16 <= 4096


# --- S10: bounded, near-linear diff -----------------------------------------------


def test_3000_modified_lines_render_quickly() -> None:
    # ndiff took > 9 minutes for 500 such lines (review S10).
    old = "\n".join(
        f"Line {i}: be helpful and polite to the user at all times." for i in range(3000)
    )
    new = old.replace("times.", "timez.")
    start = time.perf_counter()
    text, parts = _change(old, new, max_prompt_parts=10_000)
    assert time.perf_counter() - start < 1.0
    assert "+3000/−3000 lines" in text
    assert sum(p.count("\n- Line") + p.startswith("- Line") for p in parts) == 3000


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("\n".join(["x"] * 3000), "\n".join(["x", "y"] * 1500)),
        ("\n".join([""] * 3000), "\n".join(str(i) for i in range(3000))),
        (
            "\n".join("ab"[i % 2] for i in range(5000)),
            "\n".join("ab"[i // 3 % 2] for i in range(5000)),
        ),
    ],
)
def test_repetitive_prompts_render_quickly(old: str, new: str) -> None:
    start = time.perf_counter()
    diff = prompt_diff(old, new)
    assert time.perf_counter() - start < 1.0
    # Every changed line is shown (possibly more than a minimal diff, never fewer).
    assert diff.added >= len(set(new.split("\n")) - set(old.split("\n")))


def test_diff_does_not_use_ndiff(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("ndiff is super-linear")

    monkeypatch.setattr(prompt_diff_module.difflib, "ndiff", boom)
    assert prompt_diff("a\nb", "a\nc").added == 1


def test_prompt_with_too_many_lines_is_refused() -> None:
    with pytest.raises(PromptDiffTooLargeError):
        prompt_diff("", "\n" * MAX_PROMPT_LINES)
    prompt_diff("", "\n" * (MAX_PROMPT_LINES - 1))
