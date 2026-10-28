"""Whole-token matching for page assertions (PLAN-012g Batch 0).

A short needle asserted absent from (or present in) a whole rendered page can match inside a
random hex id or a CSRF token: "4111", "9009" and "tx" each failed one run that way. These
helpers match the needle as a token of its own — no letter or digit on either side — so an id
or a token cannot supply it, while the words and numbers a page really shows still match.
"""

from __future__ import annotations

import re

HEX = "0-9a-f"
ALNUM = "A-Za-z0-9"


def as_a_token(needle: str, *, alphabet: str = ALNUM) -> re.Pattern[str]:
    """The needle with no character of ``alphabet`` on either side (``re.escape``d)."""
    return re.compile(rf"(?<![{alphabet}]){re.escape(needle)}(?![{alphabet}])")


def has_token(haystack: str, needle: str) -> bool:
    return as_a_token(needle).search(haystack) is not None


def assert_present(haystack: str, needle: str, message: object = None) -> None:
    assert has_token(haystack, needle), (
        message if message is not None else f"{needle!r} not on the page as a token"
    )


def assert_absent(haystack: str, needle: str, message: object = None) -> None:
    found = as_a_token(needle).search(haystack)
    if message is None and found is not None:
        around = haystack[max(0, found.start() - 30) : found.end() + 30]
        message = f"{needle!r} on the page: …{around!r}…"
    assert found is None, message


def words(haystack: str) -> set[str]:
    """The visible words of an HTML fragment: scripts and styles dropped, tags removed, split on
    whitespace — for membership checks that must ignore ids and attribute values."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", haystack, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return set(text.split())
