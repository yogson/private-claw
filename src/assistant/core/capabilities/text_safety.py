"""
Component ID: CMP_CORE_CAPABILITIES

Character-level sanitising and UTF-16 accounting for approver-facing text (the
``capability_edit`` summary and the full prompt-change parts).
"""

import unicodedata

# Control, format (bidi controls, zero-width), surrogate, private-use, unassigned, and
# line / paragraph separators.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# Characters that render blank but are letters, marks or symbols, so the categories above
# miss them: Hangul fillers, Braille blank, and the Default_Ignorable_Code_Point marks
# (combining grapheme joiner, Khmer / Mongolian invisibles, variation selectors, tags).
_INVISIBLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xE0000, 0xE0FFF),
)


def is_unsafe_char(char: str) -> bool:
    """Control / format / separator chars, spaces other than U+0020, and blank-rendering
    letters or marks (a lookalike blank could hide or fake part of a line)."""
    category = unicodedata.category(char)
    if category in _UNSAFE_CATEGORIES or (category == "Zs" and char != " "):
        return True
    code = ord(char)
    return any(low <= code <= high for low, high in _INVISIBLE_RANGES)


def escape_char(char: str) -> str:
    """``\\uXXXX`` (or ``\\UXXXXXXXX`` above the BMP) for one character."""
    code = ord(char)
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def escape_unsafe(text: str) -> str:
    """``text`` with every :func:`is_unsafe_char` character escaped; nothing else changes
    (ordinary non-ASCII text such as Cyrillic or emoji is kept)."""
    return "".join(escape_char(c) if is_unsafe_char(c) else c for c in text)


def utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units (how Telegram measures messages)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2
