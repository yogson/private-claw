"""
Unit tests for the mobile-friendly GFM table renderer (legacy entity path).
"""

from assistant.channels.telegram.formatter import format_markdown_for_telegram
from assistant.channels.telegram.table_render import (
    TABLE_WIDTH_BUDGET,
    display_width,
    render_tables,
    strip_inline_markdown,
)

_LONG = "Starts every configured instance in the background and waits for health"


def _grid_lines(rendered: str) -> list[str]:
    """Lines inside the single code block of ``rendered``."""
    lines = rendered.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("```"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("```"))
    return lines[start + 1 : end]


# --- display width -----------------------------------------------------------------------


def test_display_width_counts_wide_and_zero_width_characters() -> None:
    assert display_width("abc") == 3
    assert display_width("日本語") == 6
    assert display_width("👍") == 2
    assert display_width("e" + chr(0x301)) == 1  # combining acute accent
    assert display_width("✅" + chr(0xFE0F)) == display_width("✅") == 2  # variation selector
    assert display_width("👨" + chr(0x200D) + "💻") == 2  # zero-width joiner sequence


def test_display_width_ignores_control_characters() -> None:
    assert display_width("a\x07b") == 2


# --- grid ----------------------------------------------------------------------------------


def test_grid_is_compact_without_trailing_whitespace() -> None:
    md = "| Item | Qty |\n|---|---|\n| Apple | 3 |\n| Kiwi | 12 |"
    assert render_tables(md) == "```\nItem  | Qty\n------+----\nApple | 3\nKiwi  | 12\n```"


def test_grid_separator_is_as_wide_as_content() -> None:
    md = "| A | Bbbb |\n|---|---|\n| xxxxx | y |"
    lines = _grid_lines(render_tables(md))
    assert len(lines[1]) == max(len(line) for line in lines)
    assert lines[1] == "------+-----"


def test_grid_honors_alignment_markers() -> None:
    md = "| L | C | R |\n|:--|:-:|--:|\n| a | b | c |\n| long | long | long |"
    lines = _grid_lines(render_tables(md))
    assert lines[0] == "L    |  C   |    R"
    assert lines[2] == "a    |  b   |    c"
    assert lines[3] == "long | long | long"


def test_grid_strips_inline_markdown_from_cells() -> None:
    md = "| Name | Link |\n|---|---|\n| **Bold** `code` | [site](https://example.com) |"
    lines = _grid_lines(render_tables(md))
    assert lines[2] == "Bold code | site"
    assert "https://" not in render_tables(md)


def test_strip_inline_markdown_variants() -> None:
    assert strip_inline_markdown("*it* _em_ ~~old~~ __b__") == "it em old b"
    assert strip_inline_markdown("![logo](x.png) <https://a.io>") == "logo https://a.io"
    assert strip_inline_markdown("snake_case_name") == "snake_case_name"
    assert strip_inline_markdown("`**raw**`") == "**raw**"
    assert strip_inline_markdown(r"a \* b") == "a * b"


def test_grid_aligns_cjk_and_emoji_by_display_width() -> None:
    md = "| 名前 | ok |\n|---|---|\n| 日本語 | 👍 |\n| ab | x |"
    lines = _grid_lines(render_tables(md))
    assert lines == ["名前   | ok", "-------+---", "日本語 | 👍", "ab     | x"]
    widths = {display_width(line.split(" | ")[0]) for line in lines if " | " in line}
    assert widths == {6}


def test_wide_characters_count_toward_the_budget() -> None:
    cjk = "表" * 18  # 36 cells but only 18 characters
    md = f"| A | B |\n|---|---|\n| {cjk} | x |"
    assert render_tables(md).startswith("**")  # lines layout, not a grid


def test_escaped_pipe_stays_inside_cell() -> None:
    md = "| Expr | Meaning |\n|---|---|\n| a \\| b | or |"
    lines = _grid_lines(render_tables(md))
    assert lines[2] == "a | b | or"


def test_table_without_outer_pipes() -> None:
    md = "Key | Value\n--- | ---\nk1 | v1"
    assert render_tables(md) == "```\nKey | Value\n----+------\nk1  | v1\n```"


def test_ragged_rows_are_padded_and_truncated() -> None:
    md = "| A | B | C |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |"
    lines = _grid_lines(render_tables(md))
    assert lines[2] == "1 |   |"
    assert lines[3] == "1 | 2 | 3"
    assert "4" not in render_tables(md)


def test_header_only_table() -> None:
    assert render_tables("| A | B |\n|---|---|") == "```\nA | B\n--+--\n```"
    wide = "| " + " | ".join(["Column header"] * 4) + " |\n|---|---|---|---|"
    assert render_tables(wide) == " · ".join(["**Column header**"] * 4)


def test_grid_fence_is_longer_than_backticks_in_cells() -> None:
    md = "| A |\n|---|\n| ``` x |"
    rendered = render_tables(md)
    assert rendered.startswith("````\n") and rendered.endswith("\n````")


# --- lines (2 columns) -----------------------------------------------------------------------


def test_two_wide_columns_render_as_lines_with_inline_markdown_kept() -> None:
    md = (
        "| Command | Effect |\n|---|---|\n"
        f"| `make up` | {_LONG} |\n"
        "| **make down** | Stops [all](https://example.com/x) instances |"
    )
    assert render_tables(md) == (
        f"**`make up`** — {_LONG}\n**make down** — Stops [all](https://example.com/x) instances"
    )


def test_lines_handle_empty_cells() -> None:
    md = f"| K | V |\n|---|---|\n| key | |\n| | {_LONG} |\n| | |"
    assert render_tables(md) == f"**key**\n{_LONG}"


# --- cards (3+ columns) ----------------------------------------------------------------------


def test_wide_table_renders_as_cards() -> None:
    md = (
        "| Name | Role | Notes |\n|---|---|---|\n"
        f"| Alice | Engineer | {_LONG} |\n"
        "| Bob | | Works on [infra](https://example.com/i) and `ci` |"
    )
    assert render_tables(md) == (
        f"**Alice**\nRole: Engineer\nNotes: {_LONG}\n\n"
        "**Bob**\nNotes: Works on [infra](https://example.com/i) and `ci`"
    )


def test_card_escapes_values_that_would_start_a_block() -> None:
    md = f"| T | | Notes |\n|---|---|---|\n| x | - item | 1. {_LONG} |"
    assert render_tables(md) == f"**x**\n\\- item\nNotes: 1. {_LONG}"


# --- surroundings ----------------------------------------------------------------------------


def test_table_inside_code_fence_is_untouched() -> None:
    md = "```markdown\n| A | B |\n|---|---|\n| 1 | 2 |\n```\n~~~\n| C | D |\n|---|---|\n~~~"
    assert render_tables(md) == md


def test_tables_next_to_text_lists_and_code() -> None:
    md = (
        "Intro text\n| A | B |\n|---|---|\n| 1 | 2 |\nOutro\n\n"
        "- item\n\n```\ncode | here\n--|--\n```\n"
        "| C | D |\n|---|---|\n| 3 | 4 |\n- next"
    )
    assert render_tables(md) == (
        "Intro text\n\n```\nA | B\n--+--\n1 | 2\n```\n\nOutro\n\n"
        "- item\n\n```\ncode | here\n--|--\n```\n\n"
        "```\nC | D\n--+--\n3 | 4\n```\n\n- next"
    )


def test_multiple_tables_pick_layouts_independently() -> None:
    md = (
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
        f"| K | V |\n|---|---|\n| k | {_LONG} |\n\n"
        f"| X | Y | Z |\n|---|---|---|\n| x | y | {_LONG} |"
    )
    assert render_tables(md) == (
        f"```\nA | B\n--+--\n1 | 2\n```\n\n**k** — {_LONG}\n\n**x**\nY: y\nZ: {_LONG}"
    )


def test_not_a_table_is_left_alone() -> None:
    md = "a | b\nnot a delimiter\n\n| A | B |\n|---|\n\n> | q | r |\n> |---|---|"
    assert render_tables(md) == md


# --- through the formatter entity path ---------------------------------------------------------


def test_formatter_grid_has_no_padding_and_fits_budget() -> None:
    md = (
        "Prices:\n\n| Item | Qty | Price |\n|:--|:-:|--:|\n"
        "| Apple | 3 | $1.20 |\n| [Kiwi](https://example.com/k) | **12** | `$0.5` |\n\nDone."
    )
    [(text, entities)] = format_markdown_for_telegram(md)
    pre = [e for e in entities if e.type == "pre"]
    assert len(pre) == 1
    block = text[pre[0].offset : pre[0].offset + pre[0].length]
    lines = block.split("\n")
    assert lines[0] == "Item  | Qty | Price"
    assert all(line == line.rstrip() for line in lines)
    assert max(display_width(line) for line in lines) <= TABLE_WIDTH_BUDGET
    assert text.endswith("Done.")


def test_formatter_cards_keep_entities() -> None:
    md = (
        "| Name | Role | Notes |\n|---|---|---|\n"
        f"| Alice | **lead** | [{_LONG}](https://example.com) |"
    )
    [(text, entities)] = format_markdown_for_telegram(md)
    assert text == f"Alice\nRole: lead\nNotes: {_LONG}"
    assert not any(e.type == "pre" for e in entities)
    by_type = {e.type: e for e in entities if e.type != "bold"}
    assert by_type["text_link"].url == "https://example.com"
    bolds = sorted((e.offset, e.length) for e in entities if e.type == "bold")
    assert bolds == [(0, 5), (12, 4)]


def _entities_by_type(md: str) -> tuple[str, dict[str, list[tuple[int, int]]]]:
    [(text, entities)] = format_markdown_for_telegram(md)
    grouped: dict[str, list[tuple[int, int]]] = {}
    for e in entities:
        grouped.setdefault(e.type, []).append((e.offset, e.length))
    return text, grouped


def test_lines_bold_keeps_markers_inside_code_spans() -> None:
    for code, shown in (("`__init__`", "__init__"), ("`**kwargs`", "**kwargs")):
        text, grouped = _entities_by_type(
            f"| Name | Description |\n|---|---|\n| {code} | {_LONG} |"
        )
        assert text.startswith(f"{shown} — ")
        assert grouped["code"] == [(0, len(shown))]
        assert grouped["bold"] == [(0, len(shown))]


def test_lines_bold_keeps_link_destination_intact() -> None:
    md = f"| Name | Description |\n|---|---|\n| [doc](https://e.com/__init__/x) | {_LONG} |"
    [(text, entities)] = format_markdown_for_telegram(md)
    assert text.startswith("doc — ")
    [link] = [e for e in entities if e.type == "text_link"]
    assert link.url == "https://e.com/__init__/x"


def test_cards_bold_keeps_markers_inside_code_and_links() -> None:
    for code, shown in (("`__init__`", "__init__"), ("`**kwargs`", "**kwargs")):
        md = f"| Name | Role | Notes |\n|---|---|---|\n| {code} | lead | {_LONG} |"
        text, grouped = _entities_by_type(md)
        assert text.startswith(f"{shown}\nRole: lead")
        assert grouped["code"] == [(0, len(shown))]
        assert (0, len(shown)) in grouped["bold"]
    link_md = "[doc](https://e.com/__init__/x)"
    md = f"| Name | Role | Notes |\n|---|---|---|\n| {link_md} | lead | {_LONG} |"
    [(text, entities)] = format_markdown_for_telegram(md)
    assert text.startswith("doc\nRole: lead")
    [link] = [e for e in entities if e.type == "text_link"]
    assert link.url == "https://e.com/__init__/x"


def test_bold_still_drops_markers_outside_code() -> None:
    md = f"| Name | Description |\n|---|---|\n| **__x__** `a` | {_LONG} |"
    text, _ = _entities_by_type(md)
    assert text.startswith("x a — ")


def test_br_tags_become_a_space_in_all_layouts() -> None:
    for tag in ("<br>", "<br/>", "<br />", "<BR>"):
        grid = render_tables(f"| A | B |\n|---|---|\n| a{tag}b | c |")
        assert "a b | c" in grid
        assert "<" not in grid
        lines = render_tables(f"| A | B |\n|---|---|\n| a{tag}b | {_LONG}x{tag}y |")
        assert lines == f"**a b** — {_LONG}x y"
        cards = render_tables(f"| A | B | C |\n|---|---|---|\n| a{tag}b | c{tag}d | {_LONG} |")
        assert cards.startswith("**a b**\nB: c d\n")
