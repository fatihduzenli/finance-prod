"""``{{ value|phone }}``: a stored phone number as a person reads it (PLAN-012f-c D9).

Ten digits, or eleven with a leading 1 (``+1``), render ``(123) 456-7822``. A number that
starts with ``+`` and another country code renders ``+CC`` and a space, then the rest as
stored (``+447911123456`` → ``+44 7911123456``; ``+44 20 7946 0958`` stays). Anything else
(letters, an extension, a tab, too few or too many digits) renders as stored, so a number the
filter does not understand is never hidden; ``None`` and the empty value render ``""``.
Only the screen changes: inputs and stored values keep what staff typed. Python code that
builds a line with a phone in it uses :func:`phone_text`.
"""

from __future__ import annotations

import re

from django import template

__all__ = ["phone", "phone_text"]

register = template.Library()

#: What a number the filter rewrites may hold: digits and the usual separators.
_PHONE_CHARACTERS = re.compile(r"\+?[0-9 ().\-]+")
_NOT_DIGIT = re.compile(r"[^0-9]")
#: The ITU country codes of one and two digits; every other code has three (the codes are
#: prefix-free, so the first match is the code).
_ONE_DIGIT_CODES = frozenset({"1", "7"})
# fmt: off
_TWO_DIGIT_CODES = frozenset(
    {
        "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45",
        "46", "47", "48", "49", "51", "52", "53", "54", "55", "56", "57", "58", "60", "61",
        "62", "63", "64", "65", "66", "81", "82", "84", "86", "90", "91", "92", "93", "94",
        "95", "98",
    }
)
# fmt: on
#: E.164: at most fifteen digits; fewer than eight is not an international number.
_INTERNATIONAL_DIGITS = range(8, 16)


def _country_code(digits: str) -> str:
    if digits[:1] in _ONE_DIGIT_CODES:
        return digits[:1]
    if digits[:2] in _TWO_DIGIT_CODES:
        return digits[:2]
    return digits[:3]


def _international(stored: str, digits: str) -> str:
    """``+CC rest``: the country code, a space, then what follows it as stored."""
    code = _country_code(digits)
    rest = stored[1:]
    taken = 0
    for index, character in enumerate(rest):
        if character in "0123456789":
            taken += 1
            if taken == len(code):
                rest = rest[index + 1 :]
                break
    return f"+{code} {rest.lstrip(' .-)')}".rstrip()


def phone_text(value: object) -> str:
    """A stored phone number as the screens show it (see the module's text)."""
    if value is None:
        return ""
    stored = str(value).strip()
    if not stored or not _PHONE_CHARACTERS.fullmatch(stored):
        return stored
    digits = _NOT_DIGIT.sub("", stored)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    elif stored.startswith("+"):
        if digits.startswith("1") or len(digits) not in _INTERNATIONAL_DIGITS:
            return stored
        return _international(stored, digits)
    if len(digits) != 10:
        return stored
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


@register.filter(name="phone")
def phone(value: object) -> str:
    """``(123) 456-7822``, ``+CC …``, the value as stored, or ``""``."""
    return phone_text(value)
