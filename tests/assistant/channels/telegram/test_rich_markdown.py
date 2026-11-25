"""
Unit tests for rich message Markdown helpers (table detection and splitting).
"""

from assistant.channels.telegram.rich_markdown import (
    RICH_MAX_BYTES,
    contains_gfm_table,
    split_rich_markdown,
)

_TABLE = "| Name | Score |\n| :--- | ---: |\n| **Ann** | 1 |\n| [Bob](https://x.y) | 2 |"


def _table(rows: int, header: str = "| id | value |", cell: str = "v") -> str:
    lines = [header, "| --- | --- |"]
    lines += [f"| {i} | {cell} |" for i in range(rows)]
    return "\n".join(lines)


class TestContainsGfmTable:
    def test_detects_pipe_table(self) -> None:
        assert contains_gfm_table(f"Results:\n\n{_TABLE}\n\nDone.")

    def test_detects_table_without_outer_pipes(self) -> None:
        assert contains_gfm_table("a | b\n--- | ---\n1 | 2")

    def test_plain_text_has_no_table(self) -> None:
        assert not contains_gfm_table("Hello, *world*!\n\n- one\n- two")

    def test_table_in_backtick_fence_does_not_count(self) -> None:
        assert not contains_gfm_table(f"Example:\n\n```markdown\n{_TABLE}\n```\n\nEnd.")

    def test_table_in_tilde_fence_does_not_count(self) -> None:
        assert not contains_gfm_table(f"~~~\n{_TABLE}\n~~~")

    def test_table_in_unclosed_fence_does_not_count(self) -> None:
        assert not contains_gfm_table(f"```\n{_TABLE}")

    def test_table_after_fence_counts(self) -> None:
        assert contains_gfm_table(f"```\ncode | x\n```\n\n{_TABLE}")

    def test_table_after_block_with_nested_indented_fence_counts(self) -> None:
        block = "```markdown\n# README\n\n    ```\n    nested\n```"
        assert contains_gfm_table(f"{block}\n\n{_TABLE}")

    def test_indented_code_table_does_not_count(self) -> None:
        indented = "\n".join(f"    {line}" for line in _TABLE.splitlines())
        assert not contains_gfm_table(indented)

    def test_column_count_mismatch_is_not_a_table(self) -> None:
        assert not contains_gfm_table("| a | b |\n| --- |\n| 1 | 2 |")

    def test_horizontal_rule_is_not_a_table(self) -> None:
        assert not contains_gfm_table("a | b\n---\ntext")


class TestSplitRichMarkdown:
    def test_small_content_is_returned_verbatim(self) -> None:
        text = f"Intro\n\n\n{_TABLE}\n\nOutro"
        assert split_rich_markdown(text) == [text]

    def test_splits_at_block_boundaries_without_cutting_a_table(self) -> None:
        table = _table(3)
        paragraphs = ["A" * 40, "B" * 40, table, "C" * 40]
        text = "\n\n".join(paragraphs)
        chunks = split_rich_markdown(text, max_bytes=len(table.encode()) + 5)
        assert chunks is not None
        assert table in chunks
        assert "\n\n".join(chunks) == text

    def test_oversized_table_is_split_by_rows_with_header_repeated(self) -> None:
        table = _table(50)
        chunks = split_rich_markdown(f"Intro\n\n{table}", max_bytes=200)
        assert chunks is not None and len(chunks) > 2
        rows: list[str] = []
        for chunk in chunks:
            assert len(chunk.encode()) <= 200
            if chunk == "Intro":
                continue
            lines = chunk.split("\n")
            assert lines[0] == "| id | value |"
            assert lines[1] == "| --- | --- |"
            rows += lines[2:]
        assert rows == [f"| {i} | v |" for i in range(50)]

    def test_block_limit_splits_table_rows(self) -> None:
        table = _table(30)
        chunks = split_rich_markdown(table, max_blocks=10)
        assert chunks is not None
        for chunk in chunks:
            lines = chunk.split("\n")
            assert lines[:2] == ["| id | value |", "| --- | --- |"]
            assert len(lines) <= 10  # header + separator + body rows
        assert sum(len(chunk.split("\n")) - 2 for chunk in chunks) == 30

    def test_many_paragraphs_respect_block_limit(self) -> None:
        text = "\n\n".join(f"p{i}" for i in range(1200))
        chunks = split_rich_markdown(text)
        assert chunks is not None and len(chunks) == 3
        assert all(len(chunk.split("\n\n")) <= 500 for chunk in chunks)
        assert "\n\n".join(chunks) == text

    def test_utf8_bytes_limit(self) -> None:
        # Cyrillic letters are 2 bytes in UTF-8: 20k chars exceed 32768 bytes.
        paragraph = "Ж" * 10_000
        text = "\n\n".join([paragraph, paragraph])
        chunks = split_rich_markdown(text)
        assert chunks == [paragraph, paragraph]
        assert len(text) < RICH_MAX_BYTES < len(text.encode())

    def test_oversized_code_block_gives_up(self) -> None:
        code = "```\n" + "x = 1\n" * 50 + "```"
        assert split_rich_markdown(f"{_TABLE}\n\n{code}", max_bytes=100) is None

    def test_deeply_indented_fence_in_list_is_never_split(self) -> None:
        fence = "    ```\n    line0\n\n    line1\n    ```"
        text = f"1. Step\n\n{fence}\n\n2. Next"
        chunks = split_rich_markdown(text, max_bytes=len(fence) + 2)
        assert chunks is not None and len(chunks) > 1
        assert sum(fence in chunk for chunk in chunks) == 1
        for chunk in chunks:
            assert chunk.count("```") % 2 == 0

    def test_nested_indented_fence_does_not_close_top_level_block(self) -> None:
        block = "```markdown\n# README\n\n    ```\n    nested\n\n    more\n    ```\n```"
        text = f"{block}\n\n{_TABLE}"
        chunks = split_rich_markdown(text, max_bytes=len(block) + 2)
        assert chunks is not None and len(chunks) > 1
        assert sum(block in chunk for chunk in chunks) == 1

    def test_too_many_columns_gives_up(self) -> None:
        header = "|" + "|".join(f" c{i} " for i in range(21)) + "|"
        delimiter = "|" + "|".join(" --- " for _ in range(21)) + "|"
        assert split_rich_markdown(f"{header}\n{delimiter}\n" + "| x " * 21 + "|") is None
