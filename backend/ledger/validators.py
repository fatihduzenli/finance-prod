"""Card-number and bank-account guard for free text (docs/CLAUDE.md Rule 1).

Contract: docs/qa/PLAN-004-schema.md §4.3 as amended by §4.6. The database repeats the
Luhn rule, with the same normalisation, in ``finance_contains_card_number`` (migration
``ledger.0002_postgresql_guards``); the ABA routing pattern, read after the same
normalisation, is checked here only.

No function here returns, logs or raises with the text it inspected: the text may be a
real card number, and an error message or a log line is exactly where it must not end up.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator
from typing import Any

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder

PAYMENT_DATA_MESSAGE = "This text may not contain card or account numbers."
PAYMENT_DATA_CODE = "payment_data"

CARD_MIN_DIGITS = 13
CARD_MAX_DIGITS = 19
ROUTING_DIGITS = 9
ACCOUNT_MIN_DIGITS = 6
#: Largest number of characters between a routing number and an account-like run.
ROUTING_ACCOUNT_MAX_GAP = 20

#: Removed before matching (PLAN-004 §4.6 point 3): zero-width space, non-joiner and
#: joiner, word joiner and the byte order mark.
INVISIBLE_CHARACTERS = "\u200b\u200c\u200d\u2060\ufeff"
_REMOVE_INVISIBLE = str.maketrans("", "", INVISIBLE_CHARACTERS)

# A UUID-shaped token (8-4-4-4-12 hexadecimal groups, either case) bounded by characters
# that are not hexadecimal, or by the ends of the text. It is replaced by one letter
# before matching (§4.6 point 4): provider identifiers are not card numbers.
_UUID = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
)
UUID_REPLACEMENT = "u"

# A run of digits in which neighbouring digits may be separated by up to three
# separators (§4.6 point 2): space, tab, no-break space, hyphen-minus, the dashes U+2010
# to U+2015, and a line break (``\r\n`` is first turned into ``\n``, so it counts as
# one). Any other character (a dot, a slash, a colon, a letter) ends a run. Matching is
# greedy and each match is a whole run; a failed separator group backtracks at most three
# characters, so matching is linear in the length of the text. ``\d`` includes every
# Unicode decimal digit (full-width, Arabic-Indic, ...), which unicodedata.decimal values
# below.
_DIGIT_RUN = re.compile(r"\d(?:[ \t\u00a0\u2010-\u2015\n-]{0,3}\d)*")
_ROUTING_CANDIDATE = re.compile(r"(?<!\d)\d{9}(?!\d)")
_ACCOUNT_RUN = re.compile(r"\d{6,}")
_ABA_WEIGHTS = (3, 7, 1, 3, 7, 1, 3, 7, 1)


def _digit_values(run: str) -> list[int]:
    return [unicodedata.decimal(char) for char in run if char.isdecimal()]


def _passes_luhn(digits: list[int]) -> bool:
    total = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _passes_aba_checksum(digits: list[int]) -> bool:
    return sum(weight * digit for weight, digit in zip(_ABA_WEIGHTS, digits, strict=True)) % 10 == 0


def normalise_for_matching(text: str) -> str:
    """The text as the card-number rule reads it (PLAN-004 §4.6 points 2 to 4).

    Invisible characters are removed, every UUID-shaped token becomes one letter and a
    Windows line break becomes ``\\n``. ``finance_contains_card_number`` in the database
    applies the same steps in the same order.
    """
    without_invisible = text.translate(_REMOVE_INVISIBLE)
    return _UUID.sub(UUID_REPLACEMENT, without_invisible).replace("\r\n", "\n")


def visible_text(text: str) -> str:
    """The text without :data:`INVISIBLE_CHARACTERS` and without leading or trailing
    Unicode whitespace (no-break space included): what a required note is judged by,
    and what is stored."""
    return text.translate(_REMOVE_INVISIBLE).strip()


def _has_card_number(normalised: str) -> bool:
    for match in _DIGIT_RUN.finditer(normalised):
        digits = _digit_values(match.group())
        if CARD_MIN_DIGITS <= len(digits) <= CARD_MAX_DIGITS and _passes_luhn(digits):
            return True
    return False


def _has_bank_routing_pattern(normalised: str) -> bool:
    account_runs = [match.span() for match in _ACCOUNT_RUN.finditer(normalised)]
    for routing in _ROUTING_CANDIDATE.finditer(normalised):
        if not _passes_aba_checksum(_digit_values(routing.group())):
            continue
        start, end = routing.span()
        for other_start, other_end in account_runs:
            if (other_start, other_end) == (start, end):
                continue
            gap = other_start - end if other_start >= end else start - other_end
            if 0 <= gap <= ROUTING_ACCOUNT_MAX_GAP:
                return True
    return False


def contains_card_number(text: str) -> bool:
    """True when ``text`` holds a 13 to 19 digit run that passes the Luhn check.

    The text is first normalised by :func:`normalise_for_matching`. A run of 20 or more
    digits is not a card number, and no part of it is tested.
    """
    return _has_card_number(normalise_for_matching(text))


def contains_bank_routing_pattern(text: str) -> bool:
    """True when a 9-digit ABA-valid number lies within 20 characters of a 6+ digit run.

    The text is first normalised by :func:`normalise_for_matching`, so the digits of a
    UUID-shaped identifier are never read as a routing or an account number.
    """
    return _has_bank_routing_pattern(normalise_for_matching(text))


def validate_no_payment_data(value: str | None) -> None:
    """Reject free text that holds a card number or a routing and account number.

    The text is normalised once, and both rules read the normalised text.
    """
    if not value:
        return
    normalised = normalise_for_matching(value)
    if _has_card_number(normalised) or _has_bank_routing_pattern(normalised):
        raise ValidationError(PAYMENT_DATA_MESSAGE, code=PAYMENT_DATA_CODE)


def _json_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _json_strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _json_strings(item)


def validate_json_has_no_payment_data(value: Any) -> None:
    """Apply :func:`validate_no_payment_data` to a JSON value.

    The serialised text is checked as a whole, and so is every string in it on its own:
    serialisation escapes a line break inside a string, which would otherwise split a
    number written over two lines into two harmless-looking runs.
    """
    if value is None:
        return
    validate_no_payment_data(json.dumps(value, cls=DjangoJSONEncoder, ensure_ascii=False))
    for text in _json_strings(value):
        validate_no_payment_data(text)
