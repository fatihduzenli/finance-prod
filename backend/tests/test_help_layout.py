"""The help pages in their frame (PLAN-012f-b Batch E, slice E-3; DESIGN-SPEC §4 Help,
help-laptop.png).

The header's title alone; the 240 px topic card (a search field the script shows, then the
topics in the spec's order, the open one marked); one article card (the title, "Updated
{date} · {n} min read", the body); "Still stuck? E-mail …" from the company settings, or
nothing when they name no address; no "Was this helpful?"; the filter script reads text only
and sends nothing; the minutes; the phone's one column; no query beyond the index's.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from finance import help as help_pages
from ledger.models import CompanySettings
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

STATIC = Path(__file__).resolve().parents[1] / "finance/static/finance"
#: DESIGN-SPEC §4 Help's order, as the existing topics fill it (no "Getting around" and no
#: "Reports" topic exists).
SPEC_ORDER = [
    "store-costs",
    "collecting-an-invoice",
    "invoice-lines",
    "autopay-and-daily-charges",
    "promises-to-pay",
    "lease-ends-and-transfers",
    "needs-review",
]


@pytest.fixture
def viewer() -> Client:
    return client_for(Role.VIEWER, "help-viewer")[0]


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return str(response.content.decode())


def between(html: str, start: str, end: str) -> str:
    assert start in html, start
    return html.split(start, 1)[1].split(end, 1)[0]


def header_of(html: str) -> str:
    return between(html, 'id="page-header"', "</header>")


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def set_support_email(address: str) -> None:
    row = CompanySettings.current()
    CompanySettings.objects.filter(pk=row.pk).update(support_email=address)


# --- The frame ------------------------------------------------------------------------------------


def test_the_topics_follow_the_spec_order() -> None:
    assert [topic.slug for topic in help_pages.TOPICS] == SPEC_ORDER


def test_the_index_holds_the_title_and_the_topic_card(viewer: Client) -> None:
    html = page(viewer, "/help/")
    header = header_of(html)
    assert "<h1>Help</h1>" in header
    assert 'class="breadcrumb"' not in header
    card = between(html, 'id="help-topics-card"', "</nav>")
    # The search field is in the markup, hidden until the script shows it.
    assert re.search(r'<input type="search"[^>]*id="help-search"[^>]*hidden>', card)
    slugs = re.findall(r'href="/help/([a-z0-9-]+)/" id="help-nav-\1"', card)
    assert slugs == SPEC_ORDER
    for topic in help_pages.TOPICS:
        assert f">{topic.title}</a>" in card
    assert 'aria-current="page"' not in card  # no topic is open on the index
    assert 'id="help-topic"' not in html
    assert '<script src="/static/finance/help_filter.js" defer></script>' in html


def test_a_topic_renders_in_the_frame(viewer: Client) -> None:
    html = page(viewer, "/help/promises-to-pay/")
    header = header_of(html)
    assert "<h1>Help</h1>" in header and "Promises to pay" not in header
    card = between(html, 'id="help-topics-card"', "</nav>")
    current = re.findall(r'<a [^>]*aria-current="page"[^>]*>([^<]+)</a>', card)
    assert current == ["Promises to pay"]
    article = between(html, 'id="help-topic"', "</article>")
    assert '<h2 class="help-title" id="help-title">Promises to pay</h2>' in article
    meta = between(article, 'id="help-meta">', "</p>")
    assert text_of(meta) == "Updated Oct 22, 2026 · 1 min read"
    assert '<time datetime="2026-10-22">' in meta
    assert "Still open records this entry" in article
    assert '<script src="/static/finance/help_filter.js" defer></script>' in html
    # The topic card comes before the article in the page (the phone's order is the CSS's).
    assert html.index('id="help-topics-card"') < html.index('id="help-topic"')


@pytest.mark.parametrize("slug", SPEC_ORDER)
def test_every_topic_shows_its_own_date_and_no_helpful_vote(viewer: Client, slug: str) -> None:
    topic = next(t for t in help_pages.TOPICS if t.slug == slug)
    html = page(viewer, f"/help/{slug}/")
    meta = between(html, 'id="help-meta">', "</p>")
    assert f'<time datetime="{topic.updated.isoformat()}">' in meta
    assert "Was this helpful" not in html
    assert ">Yes<" not in html and ">No<" not in html


# --- The minutes ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "minutes"), [(0, 1), (1, 1), (200, 1), (201, 2), (400, 2), (401, 3)]
)
def test_the_minutes_are_the_words_over_two_hundred_rounded_up(words: int, minutes: int) -> None:
    assert help_pages.read_minutes(" ".join(["word"] * words)) == minutes


def test_the_minutes_count_words_not_markup() -> None:
    body = '<p id="a-long-identifier" class="x y z">one <strong>two</strong></p>' * 100
    assert help_pages.read_minutes(body) == 1  # 200 words; the attributes are not words
    assert help_pages.read_minutes(body + "<p>three</p>") == 2


def test_the_page_shows_the_minutes_of_its_own_body(viewer: Client) -> None:
    html = page(viewer, "/help/collecting-an-invoice/")
    body = between(html, '<div class="help-body">', "</article>")
    meta = text_of(between(html, 'id="help-meta">', "</p>"))
    assert meta.endswith(f"· {help_pages.read_minutes(body)} min read")


# --- The footer -----------------------------------------------------------------------------------


def test_no_support_address_no_footer(viewer: Client) -> None:
    set_support_email("")
    html = page(viewer, "/help/store-costs/")
    assert 'id="help-support"' not in html and "Still stuck?" not in html


def test_the_footer_names_the_company_support_address(viewer: Client) -> None:
    set_support_email("support@example.com")
    html = page(viewer, "/help/store-costs/")
    footer = between(html, 'id="help-support">', "</p>")
    assert footer == (
        'Still stuck? <a href="mailto:support@example.com">E-mail support@example.com</a>'
    )
    # The index has no article, so no footer.
    assert 'id="help-support"' not in page(viewer, "/help/")


def test_the_footer_escapes_the_address(viewer: Client) -> None:
    # A row written past the form's validation still renders as text.
    set_support_email('x"><script>alert(1)</script>@example.com')
    html = page(viewer, "/help/store-costs/")
    footer = between(html, 'id="help-support">', "</p>")
    assert "<script>" not in footer and '"><' not in footer
    assert "&lt;script&gt;" in footer


# --- The filter script ----------------------------------------------------------------------------


def test_the_filter_script_reads_text_only_and_sends_nothing() -> None:
    script = (STATIC / "help_filter.js").read_text()
    assert "textContent" in script
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert unsafe not in script, unsafe
    for outbound in ("fetch(", "XMLHttpRequest", "sendBeacon", "localStorage", "location"):
        assert outbound not in script, outbound
    # It shows the hidden field itself: without it the field stays hidden, the list whole.
    assert "field.hidden = false" in script


def test_the_filter_script_is_served(viewer: Client) -> None:
    from django.contrib.staticfiles import finders

    assert finders.find("finance/help_filter.js")


# --- The style sheet, the phone and the queries ---------------------------------------------------


def test_the_style_sheet_carries_the_frame_and_the_phone_rule() -> None:
    css = (STATIC / "app.css").read_text()
    section = "/*" + css.split("/* --- Help (PLAN-012f-b E-3)", 1)[1].split("/* --- ", 1)[0]
    for rule in (
        ".help-layout { display: grid; grid-template-columns: 240px minmax(0, 1fr);",
        '.help-topics a[aria-current="page"] { background: var(--month-current); '
        "color: var(--link); font-weight: 600; }",
        ".help-topic .help-title { margin: 0 0 var(--space-2); font-size: 22px; font-weight: 700;",
        ".help-body { font-size: var(--fs-lg); line-height: 1.65;",
        ".help-body h3 { margin: var(--space-6) 0 var(--space-2); font-size: 18px;",
        ".help-body li + li { margin-top: var(--space-2); }",
        ".help-tip { padding: 12px 16px; border: 1px solid var(--border-soft); "
        "border-left: 3px solid var(--teal);",
    ):
        assert rule in section, rule
    assert "#" not in re.sub(r"/\*.*?\*/", "", section, flags=re.S)  # tokens, no raw colour
    phone = section.split("@media (max-width: 768px) {", 1)[1]
    assert ".help-layout { grid-template-columns: minmax(0, 1fr);" in phone
    assert ".help-main { order: -1; }" in phone  # the article first, the topics after (§6)


def test_a_topic_costs_no_query_beyond_the_index(viewer: Client) -> None:
    viewer.get("/help/")  # warm the session's own first-request work
    with CaptureQueriesContext(connection) as index:
        viewer.get("/help/")
    with CaptureQueriesContext(connection) as topic:
        viewer.get("/help/store-costs/")
    assert len(topic.captured_queries) == len(index.captured_queries)
