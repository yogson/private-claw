"""Full prompt-change parts of a ``capability_edit`` (review S9 / S10 of #64).

The prompt diff is never truncated: every added and removed line is in the parts, the
parts respect Telegram's UTF-16 limit, and a change that needs more parts than allowed
is refused. The diff is near-linear and bounded (no ``difflib.ndiff``).
"""

import random
import re
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from assistant.core.capabilities import prompt_diff as prompt_diff_module
from assistant.core.capabilities.edit_summary import (
    PROMPT_PART_MAX_UNITS,
    PromptChangeTooLargeError,
    summarize_change,
)
from assistant.core.capabilities.prompt_diff import (
    CONTINUED_LABEL,
    MAX_PROMPT_LINES,
    PromptDiffTooLargeError,
    prompt_diff,
    split_parts,
)
from assistant.core.capabilities.text_safety import utf16_len
from assistant.core.confirmation import (
    ATTACHMENT_PART_MAX_UTF16,
    ATTACHMENT_TITLE_MAX_UTF16,
    ConfirmationAttachment,
)


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
    """Diff lines of the parts with the label lines dropped and long-line continuations
    joined back (a continuation's marker must be its line's marker)."""
    lines: list[str] = []
    for line in "\n".join(parts).split("\n"):
        if line == CONTINUED_LABEL:
            continue
        if len(line) >= 2 and line[1] == "…" and lines:
            assert line[0] == lines[-1][0], (line, lines[-1])
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


# --- B1 (fifth review of #64): no part is ever blank --------------------------------

_HUNK_RE = re.compile(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@")


def _check_parts(
    old: str, new: str, parts: tuple[str, ...] | list[str], units: int, text: str = ""
) -> None:
    """Every part is visible and fits; the hunks rebuild ``old`` and ``new`` exactly;
    the ``+A/−R`` counts of the summary ``text`` (if given) match the parts."""
    assert parts
    for part in parts:
        assert part.strip(), parts
        assert utf16_len(part) <= units <= ATTACHMENT_PART_MAX_UTF16
    ConfirmationAttachment(title="Prompt change for cap", parts=list(parts))
    a, b = old.split("\n") if old else [], new.split("\n") if new else []
    rebuilt: list[str] = []
    pos = 0
    hunks: list[tuple[tuple[int, ...], list[str]]] = []
    for line in _content(parts):
        match = _HUNK_RE.fullmatch(line)
        if match:
            hunks.append((tuple(int(x) for x in match.groups()), []))
        else:
            assert line[:1] in {" ", "-", "+"} and line[1:2] == " ", line
            hunks[-1][1].append(line)
    for (a1, alen, b1, blen), body in hunks:
        i1, j1 = (a1 - 1 if alen else a1), (b1 - 1 if blen else b1)
        assert [x[2:] for x in body if x[0] in " -"] == a[i1 : i1 + alen]
        assert [x[2:] for x in body if x[0] in " +"] == b[j1 : j1 + blen]
        rebuilt.extend(a[pos:i1])
        rebuilt.extend(x[2:] for x in body if x[0] in " +")
        pos = i1 + alen
    rebuilt.extend(a[pos:])
    assert rebuilt == b
    if text:
        content = _content(parts)
        added = sum(x.startswith("+") for x in content)
        removed = sum(x.startswith("-") for x in content)
        assert f"+{added}/−{removed} lines, see {len(parts)} attached part" in text
    else:
        assert _content(parts) == list(prompt_diff(old, new).lines)  # nothing dropped


@pytest.mark.parametrize("k", [3662, 3663, 3664, 3696, 3697, 3698])
def test_reviewer_lengths_end_with_a_visible_part(k: int) -> None:
    # `prompt: |` block scalars end with "\n": the diff ends with an empty context line
    # ("  "), which used to be a part of its own and fail ConfirmationAttachment.
    old, new = "intro\nOLDLINE\n", "intro\n" + "N" * k + "\n"
    text, parts = _change(old, new)
    assert parts[-1].endswith("\n  ")
    _check_parts(old, new, parts, PROMPT_PART_MAX_UNITS)
    _check_parts(old, new, parts, PROMPT_PART_MAX_UNITS, text)


@pytest.mark.parametrize("k", range(3640, 3710))
def test_lengths_around_the_part_boundary(k: int) -> None:
    old, new = "intro\nOLDLINE\n", "intro\n" + "N" * k + "\n"
    _check_parts(old, new, _change(old, new)[1], PROMPT_PART_MAX_UNITS)


def test_trailing_blank_lines_after_a_long_changed_line() -> None:
    old, new = "y\nA\n\n\n\n", "x" * 3666 + "\nA\n\n\n\n"
    _, parts = _change(old, new)
    _check_parts(old, new, parts, PROMPT_PART_MAX_UNITS)


@contextmanager
def _time_box(seconds: float) -> Iterator[None]:
    """Fail with TimeoutError instead of hanging (there is no pytest-timeout)."""

    def expire(*_args: object) -> None:
        raise TimeoutError(f"still running after {seconds}s")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="needs SIGALRM")
def test_a_part_that_would_start_blank_starts_with_the_continued_label() -> None:
    # The pinned-label guard keeps split_parts from looping forever; a regression must
    # fail fast, not hang the suite (N19 of #64).
    with _time_box(5):
        _check_continued_label()


def _check_continued_label() -> None:
    assert CONTINUED_LABEL == "(continued)"
    lines = ["@@ -1,2 +1,2 @@", "+ " + "x" * 98, "  ", "  "]
    assert split_parts(lines, 100) == ["@@ -1,2 +1,2 @@", "+ " + "x" * 98, "(continued)\n  \n  "]
    # A blank piece wider than what is left after the label is cut, never flushed
    # alone (and the loop terminates).
    lines = ["@@ -1,2 +1,2 @@", "+ " + "x" * 98, "  " + " " * 97]
    parts = split_parts(lines, 100)
    assert parts[2].startswith("(continued)\n   ") and parts[3].startswith(" …")
    assert all(p.strip() and utf16_len(p) <= 100 for p in parts)
    assert _content(parts) == lines
    # A long all-space context line after a full part.
    lines = ["@@ -1,1 +1,1 @@", "  " + " " * 300]
    parts = split_parts(lines, 64)
    assert all(p.strip() and utf16_len(p) <= 64 for p in parts)
    assert _content(parts) == lines


def test_a_hunk_header_is_never_continued() -> None:
    # N17 of #64: the part after a hunk header used to start with "@…".
    lines = ["@@ -1,1 +1,1 @@", "+ " + "x" * 28, "@@ -9,2 +9,2 @@", "  ", "-  "]
    parts = split_parts(lines, 64)
    assert parts == ["@@ -1,1 +1,1 @@\n+ " + "x" * 28 + "\n@@ -9,2 +9,2 @@", "(continued)\n  \n-  "]
    assert _content(parts) == lines


_ALPHABET = "abc XYZ Привет мир 🧠✅ 0123"


def _text_of_units(rng: random.Random, units: int) -> str:
    """Random text of ``units`` UTF-16 units (``units + 1`` if it ends in an emoji)."""
    text = "".join(rng.choices(_ALPHABET, k=units))
    while utf16_len(text) > units + 1:
        text = text[: len(text) - (utf16_len(text) - units) // 2 - 1]
    return text


def _random_line(rng: random.Random, units: int) -> str:
    roll = rng.random()
    if roll < 0.3:
        return ""
    if roll < 0.35:
        return " " * rng.randint(1, units + 5)
    if roll < 0.6:
        return _text_of_units(rng, rng.randint(1, 12))
    if roll < 0.9:  # around one part (minus the 2-char marker and a separator)
        return _text_of_units(rng, max(1, units + rng.randint(-45, 3)))
    return _text_of_units(rng, rng.randint(units, 3 * units))


def _random_prompts(rng: random.Random, units: int) -> tuple[str, str]:
    old = [_random_line(rng, units) for _ in range(rng.randint(0, 10))]
    new = list(old)
    for _ in range(rng.randint(1, 4)):
        op = rng.random()
        if op < 0.4 and new:
            new[rng.randrange(len(new))] = _random_line(rng, units)
        elif op < 0.7:
            new.insert(rng.randint(0, len(new)), _random_line(rng, units))
        elif new:
            del new[rng.randrange(len(new))]
    if rng.random() < 0.5:
        new.extend([""] * rng.randint(1, 4))
    old_text, new_text = "\n".join(old), "\n".join(new)
    if rng.random() < 0.5:  # block scalars end with "\n"
        old_text, new_text = old_text + "\n", new_text + "\n"
    return old_text, new_text


def test_randomized_multi_part_diffs_are_visible_bounded_and_exact() -> None:
    rng = random.Random(64_5)
    checked = 0
    for _ in range(2000):
        units = rng.choice([64, 64, 65, 80, 100, 160, 300, PROMPT_PART_MAX_UNITS])
        old, new = _random_prompts(rng, units)
        if old == new:
            continue
        text, parts = _change(old, new, max_prompt_parts=10_000, prompt_part_units=units)
        _check_parts(old, new, parts, units, text)
        checked += len(parts) > 1
    assert checked > 1000  # mostly multi-part diffs
