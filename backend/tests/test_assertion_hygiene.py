"""PLAN-012g Batch 0: no new page-wide bare-substring assertion with a short needle.

Four runs failed on the same class: ``assert "4111" not in body`` (an ICS feed whose UIDs are
random hex), ``"9009" not in html`` (a page of UUID hrefs), ``value not in page`` with "tx" (a
CSRF token). A short run of letters and digits asserted present in, or absent from, a whole page
can be supplied by an id or a token. Such checks go through ``tests/text_helpers.py`` (a
whole-token match) or against one element's text. This test reads every test file and fails on
a new site; the ones reviewed as safe are listed with their reason.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent

# A short needle: one to eight letters and digits, nothing else.
NEEDLE = r'"[A-Za-z0-9]{1,8}"'
# A whole page or response as the haystack (not a JSON body, a message or one element's text).
HAYSTACK = (
    r"(?:body|page|html|text|content|lowered|rendered|out|ics|pages|strip|header|options|form|"
    r"stores|payments|shown|location|response\.content\.decode\(\)(?:\.lower\(\))?|"
    r"page\.content\(\)|[a-z_]+\.lower\(\))"
)
# A dict or a method call after the name is a JSON body or a lookup, not a page.
BARE = re.compile(rf"\bassert\b[^\n]*?{NEEDLE} (?:not )?in {HAYSTACK}\b(?!\[|\.get\()")
# The same class in a query: a short needle in a `__contains` / `__icontains` lookup over rows
# whose text holds UUIDs (the audit rows' dicts; the fifth instance, 2026-10-28). A slice such as
# CARD[-4:] counts as short.
BARE_QUERY = re.compile(r'__i?contains=(?:"[A-Za-z0-9]{1,4}"|[A-Z_]+\[-\d\])')

# Sites reviewed on 2026-10-28 and left as they are, with the reason: a line number, or the
# stripped line's text where the file is edited often. A new entry needs the same review: can an
# id, a token or an attribute value supply the needle?
ALLOWED: dict[str, set[int | str]] = {
    # Usernames and content types are fixed strings, not ids: these lookups cannot hit a UUID.
    "test_seed_demo.py": {
        'u.email.endswith("@example.com") for u in User.objects.filter(username__contains="seed")'
    },
    # Fee type names are typed by staff; "4111" against them is the card-number rule itself.
    "test_plan012b_adversarial.py": {
        'assert not FeeType.objects.filter(name__contains="4111").exists()'
    },
    # The body's computed font-family string, not a page.
    "test_shell_12f.py": {147},
    # A template's source text, not a rendered page: no token, no id.
    "test_add_payment_page.py": {553},
    # The /healthz JSON body.
    "test_banner_adversarial.py": {85},
    # A fixed validation message.
    "test_card_number_guard.py": {143},
    # A CSV export: no ids in its columns.
    "test_contact_log.py": {1570},
    "test_store_form_fields.py": {'assert ",TX," in body and "Texas" not in body'},
    # Text extracted from an uploaded document, not a page; a content-type lookup (a fixed string).
    "test_documents.py": {
        337,
        1796,
        1811,
        'docx = Document.objects.get(content_type__contains="word")',
    },
    # A Bicep file, lowercased.
    "test_infra_storage.py": {163},
    # The message shown to staff: a fixed string, no ids.
    "test_manual_payment_marking.py": {586, 587, 589},
    # An outbound request body as a dict: key membership, not a substring.
    # (listed by text: the file moves with the PLAN-014 re-pins).
    "test_lease_sync.py": {
        'assert "lastname" not in body',
        'assert ("email" in body) is (expected_email is not None)',
    },
    "test_plan012a_adversarial.py": {58},
    "test_stax_adapter.py": {289, 290, 368},
    # The money filter's output for one value (listed by text: the file moves with route pins).
    "test_plan012c_design_d_adversarial.py": {
        'assert "nan" not in lowered and "inf" not in lowered, rendered'
    },
    # A redirect URL built by the app, no token in it.
    "test_plan012e2_a_adversarial.py": {223},
    # One section's slice; "Margin" cannot come from a UUID.
    "test_plan012e2_c_adversarial.py": {121},
    # The page header's visible text, tags stripped; ids live in attributes.
    "test_plan012f_c_adversarial.py": {143, 144},
    # A JSON preview body.
    "test_plan012fb_d_adversarial.py": {135},
    # A command's stdout.
    "test_provider_environment_check.py": {101},
    # The service worker's JavaScript.
    "test_pwa.py": {123},
    # A PostgreSQL trigger's message.
    # (listed by text: the file moved with the PLAN-014 D3 re-pins).
    "test_reserved_tables.py": {'assert "outbound" not in text, text'},
    # form.errors, a dict.
    "test_settlement.py": {672},
    # An error message's text.
    "test_stax_retries.py": {341},
}


def sites() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(TESTS.rglob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            allowed = ALLOWED.get(path.name, set())
            hit = BARE.search(line) or BARE_QUERY.search(line)
            if hit and number not in allowed and line.strip() not in allowed:
                found.append((str(path.relative_to(TESTS)), number, line.strip()))
    return found


def test_no_page_wide_bare_substring_assertion_with_a_short_needle() -> None:
    offenders = sites()
    assert not offenders, "\n".join(f"{f}:{n}: {line}" for f, n, line in offenders)


def test_the_helper_matches_tokens_not_fragments() -> None:
    from tests.text_helpers import as_a_token, has_token, words

    page = '<a href="/stores/3fa94111c2d8/">Store 4111</a><input value="zkqpmLi2YMtxNyc">'
    assert has_token(page, "4111") and not has_token(page, "tx")
    assert has_token(page.replace("Store 4111", "Store 9"), "4111") is False
    assert as_a_token("a.b").search("x a.b y") and not as_a_token("a.b").search("xa.by")
    assert words(page) == {"Store", "4111"}
