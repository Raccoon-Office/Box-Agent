"""Estimate text tokens using the character-class costs of newer tokenizers."""

from __future__ import annotations


# Costs are hundredths of a token: start of run, class transition, long run,
# and repeated character in a long run. Later ranges override earlier ones.
_CHARACTER_CLASSES = (
    (((128, 65535),), (92, 0, 92, 60)),  # Other Unicode
    (((0, 127),), (40, 88, 95, 10)),  # Other ASCII
    (((97, 122),), (20, 46, 71, 71)),  # Lowercase ASCII
    (((65, 90),), (74, 64, 92, 92)),  # Uppercase ASCII
    (((48, 57),), (15, 147, 33, 33)),  # Digits
    (((9, 9), (32, 32)), (5, 0, 5, 5)),  # Horizontal whitespace
    (((10, 10), (13, 13)), (0, 125, 0, 0)),  # Newlines
    (((192, 879), (7680, 7935)), (160, 0, 160, 160)),  # Extended Latin
    (((880, 1023),), (74, 0, 74, 74)),  # Greek
    (((1024, 1327),), (42, 0, 42, 42)),  # Cyrillic
    (((1328, 2303),), (72, 0, 72, 72)),  # Armenian, Hebrew, Arabic, etc.
    (((2304, 7679),), (90, 0, 90, 90)),  # Indic and other scripts
    (((12352, 12543),), (77, 0, 77, 77)),  # Japanese kana
    (((13312, 40959),), (107, 0, 107, 107)),  # CJK ideographs
    (((44032, 55215),), (131, 0, 131, 131)),  # Hangul syllables
)


def _class_table() -> bytes:
    table = bytearray(65536)
    for kind, (ranges, _) in enumerate(_CHARACTER_CLASSES):
        for start, end in ranges:
            table[start : end + 1] = bytes([kind]) * (end - start + 1)
    return bytes(table)


_CLASS_TABLE = _class_table()
_COSTS = tuple(costs for _, costs in _CHARACTER_CLASSES)
_SHORT_RUN_LENGTH = 16


def estimate_text_tokens(text: str) -> int:
    """Count by character class and round to the nearest whole token."""
    # Match the reference estimator's treatment of supplemental characters as
    # UTF-16 pairs. Ordinary text does not need to be copied or encoded.
    if not text.isascii() and max(text) > "\uffff":
        text = _as_utf16_characters(text)
    total = 0
    previous_kind = -1
    previous_character = ""
    run_length = 0
    for character in text:
        kind = _CLASS_TABLE[ord(character)]
        start, transition, long_run, repeated = _COSTS[kind]
        if kind != previous_kind:
            total += transition
            previous_kind = kind
            run_length = 0
        if run_length < _SHORT_RUN_LENGTH:
            total += start
        elif character == previous_character:
            total += repeated
        else:
            total += long_run
        run_length += 1
        previous_character = character
    return (total + 50) // 100


def _as_utf16_characters(text: str) -> str:
    units = []
    for character in text:
        code_point = ord(character)
        if code_point <= 65535:
            units.append(character)
        else:
            offset = code_point - 65536
            units.extend((chr(0xD800 + (offset >> 10)), chr(0xDC00 + (offset & 0x3FF))))
    return "".join(units)
