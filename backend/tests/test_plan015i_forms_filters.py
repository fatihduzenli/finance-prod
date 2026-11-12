"""PLAN-015i (docs/qa/15/PLAN-015i-forms-filters-residuals.md): the single-card form pages open
with the unnumbered step head (§3.1); the Activity log and the Messages from the processor take
the list pages' filter component (§3.2)."""

from __future__ import annotations

import html as html_lib
import re
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from finance.dates import day_text
from ledger.models import MessageTemplate
from ledger.services import audit
from ledger.views import audit as audit_views
from ledger.views.reports import ReportTooSlow
from tests.test_operator_views import client_for
from tests.test_plan012f_d_pages import APP_CSS, World, client_of, path_of
from tests.test_plan012f_d_pages import world as world  # noqa: F401 (fixture)
from tests.test_plan012fa_c_builder import element, text
from tests.test_plan015_batch6_admin import an_event, page

pytestmark = pytest.mark.django_db

STEP_HEAD = "ledger/leases/_step_head.html"

#: The twelve single-card form pages: the route, the card's title id, its title and the help
#: sentence moved into the head's note (``None``: the page's help stays where it was, or it
#: has none, and no note line is drawn).
FORMS: list[tuple[str, str, str, str | None]] = [
    ("ledger:stores:set_cost", "store-cost-card-title", "Landlord rent", None),
    (
        "ledger:stores:correct_cost",
        "cost-correct-card-title",
        "Landlord rent",
        "Saving writes a new row with these values and dates and keeps this one in the history, "
        "struck through, with your note. The dates may not overlap another current row of this "
        "store.",
    ),
    ("ledger:contacts:edit", "contact-edit-card-title", "Contact", None),
    (
        "ledger:contacts:correct",
        "contact-correct-card-title",
        "Correction",
        "The entry stays as it was recorded; the correction is listed under it with your name "
        "and the time. Never write card or account numbers.",
    ),
    ("ledger:contacts:outcome", "contact-outcome-card-title", "Outcome", None),
    ("ledger:leases:enrol", "enrol-card-title", "Autopay", None),
    ("ledger:charges:link_invoice", "link-invoice-card-title", "Invoice", None),
    ("ledger:one_off_charges:link", "link-invoice-card-title", "Invoice", None),
    ("ledger:plans:reschedule", "reschedule-card-title", "New dates", None),
    ("ledger:leases:resolve_deposit", "deposit-card-title", "Deposit", None),
    (
        "ledger:calendar:edit",
        "calendar-card-title",
        "Entry",
        "Calendar entries are shared with all staff and appear in every staff member's calendar "
        "feed. Never write card or account numbers.",
    ),
    ("ledger:one_off_charges:edit", "charge-card-title", "Charge", None),
]


def form_card(body: str, heading_id: str) -> str:
    opening = re.search(
        rf'<section class="card form-card step-card"(?: id="[a-z-]+")? '
        rf'aria-labelledby="{heading_id}">',
        body,
    )
    assert opening is not None, heading_id
    return element(body, opening.group(0), "section")


@pytest.mark.parametrize(("name", "heading_id", "title", "note"), FORMS)
def test_each_single_card_form_opens_with_the_unnumbered_step_head(
    world: World,  # noqa: F811
    name: str,
    heading_id: str,
    title: str,
    note: str | None,
) -> None:
    response = client_of(world, name, {}).get(path_of(world, name))
    assert response.status_code == 200, name
    body = response.content.decode()
    card = form_card(body, heading_id)
    first = card.split(">", 1)[1].lstrip()
    head = (
        f'<div class="step-head"><div class="step-title"><h2 id="{heading_id}">{title}</h2>'
        + (f'<p class="muted step-note">{note}</p>' if note else "")
        + "</div></div>"
    )
    assert first.startswith(head), (name, first[:300])
    # One heading per card, named by the section; no number, no empty note line.
    assert card.count("<h2") == 1 and body.count(f'id="{heading_id}"') == 1
    assert body.count(f'aria-labelledby="{heading_id}"') == 1
    assert "step-number" not in card and "step-action" not in card
    assert card.count("step-note") == (1 if note else 0)
    if note:
        # Moved, never duplicated.
        assert text(body).count(text(note)) == 1, name


def test_the_step_head_without_a_note_draws_no_muted_line() -> None:
    plain = render_to_string(STEP_HEAD, {"heading_id": "x-title", "title": "Title", "note": ""})
    assert plain == (
        '<div class="step-head"><div class="step-title"><h2 id="x-title">Title</h2></div></div>\n'
    )
    assert "step-note" not in render_to_string(STEP_HEAD, {"heading_id": "y", "title": "T"})


def test_the_step_head_with_a_note_is_unchanged() -> None:
    # Byte-identical to the head before PLAN-015i (New lease and Edit lease use it).
    numbered = render_to_string(
        STEP_HEAD,
        {"number": 2, "heading_id": "terms-title", "title": "Terms", "note": "Rent, start."},
    )
    assert numbered == (
        '<div class="step-head"><span class="step-number" aria-hidden="true">2</span>'
        '<div class="step-title"><h2 id="terms-title">Terms</h2>'
        '<p class="muted step-note">Rent, start.</p></div></div>\n'
    )
    escaped = render_to_string(
        STEP_HEAD, {"heading_id": "h", "title": "T", "note": "<b>x</b>", "action": ""}
    )
    assert '<p class="muted step-note">&lt;b&gt;x&lt;/b&gt;</p>' in escaped


# --- B: the Activity log and Messages filters ---------------------------------------------------


def admin() -> Client:
    return client_for(Role.ADMIN)[0]


def chips(body: str) -> list[tuple[str, str]]:
    """(href, words) of each chip, unescaped."""
    row = element(body, '<div class="filters-chips" id="filters-chips">', "div")
    return [
        (html_lib.unescape(href), html_lib.unescape(" ".join(words.split())))
        for href, words in re.findall(
            r'<a class="chip" href="([^"]*)" aria-label="[^"]*">(.*?) <span aria-hidden="true">'
            r"×</span></a>",
            row,
            re.S,
        )
    ]


def toggle(body: str) -> str:
    found = re.search(r'<button type="button" class="btn filters-toggle"[^>]*>(.*?)</button>', body)
    assert found is not None
    return found.group(1)


def params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def page_actions(body: str) -> str:
    start = body.index('<div class="page-header">')
    header = body[start : body.index("</header>", start)]
    return element(header, '<div class="page-actions">', "div")


def clear_url(body: str) -> str:
    found = re.search(r'<a class="btn" id="filters-clear" href="([^"]*)">Clear</a>', body)
    assert found is not None
    return html_lib.unescape(found.group(1))


def test_the_activity_log_without_a_filter_shows_no_chip_and_no_count() -> None:
    body = page(admin(), "/audit/")
    assert '<form method="get" action="/audit/" id="filters" class="filters" role="search"' in body
    assert chips(body) == [] and toggle(body) == "Filters"
    assert clear_url(body) == "/audit/"
    # No search field on this page: nothing is fetched as one types.
    assert 'class="filters-search"' not in body and "finance/filters.js" not in body
    assert 'id="audit-filters"' not in body and ">Show</button>" not in body


AUDIT_FIELDS = [
    ("action", "document.*", "Action"),
    ("user", "somebody", "User"),
    ("entity_type", "store", "Entity type"),
    ("entity_id", "abc-1", "Entity id"),
]


@pytest.mark.parametrize(("name", "value", "label"), AUDIT_FIELDS)
def test_each_activity_log_text_filter_has_its_chip(name: str, value: str, label: str) -> None:
    body = page(admin(), f"/audit/?{name}={value}&page=2")
    [(href, words)] = chips(body)
    assert words == f"{label}: {value}"
    assert toggle(body) == "Filters · 1"
    assert href.startswith("/audit/") and name not in params(href)
    assert "page" not in params(href)


def test_the_activity_log_date_chips_show_the_accepted_days() -> None:
    today = audit.today_in_new_york()
    start, end = today - timedelta(days=10), today - timedelta(days=2)
    body = page(admin(), f"/audit/?from={start.isoformat()}&to={end.isoformat()}")
    assert [words for _, words in chips(body)] == [
        f"From: {day_text(start)}",
        f"To: {day_text(end)}",
    ]
    removed_from, removed_to = (href for href, _ in chips(body))
    assert params(removed_from) == {"to": [end.isoformat()]}
    assert params(removed_to) == {"from": [start.isoformat()]}


def test_a_date_left_empty_or_at_the_default_has_no_chip() -> None:
    today = audit.today_in_new_york()
    end = today - timedelta(days=3)
    body = page(admin(), f"/audit/?to={end.isoformat()}")
    assert [words for _, words in chips(body)] == [f"To: {day_text(end)}"]
    assert chips(page(admin(), f"/audit/?to={today.isoformat()}")) == []


def test_all_six_activity_log_filters_count_six() -> None:
    today = audit.today_in_new_york()
    query = "&".join(f"{name}={value}" for name, value, _ in AUDIT_FIELDS)
    start, end = today - timedelta(days=5), today - timedelta(days=1)
    body = page(admin(), f"/audit/?{query}&from={start.isoformat()}&to={end.isoformat()}")
    assert [words.split(":")[0] for _, words in chips(body)] == [
        "Action",
        "User",
        "Entity type",
        "Entity id",
        "From",
        "To",
    ]
    assert toggle(body) == "Filters · 6"


def test_a_refused_value_has_no_chip_and_keeps_its_message() -> None:
    body = page(admin(), "/audit/?from=2026-02-31&to=nonsense")
    assert chips(body) == []
    assert html_lib.escape(audit.UNREADABLE_DATES.format(days=audit.DEFAULT_DAYS)) in body
    body = page(admin(), f"/audit/?action={'a' * 201}")
    assert chips(body) == []
    assert audit.TOO_LONG.format(limit=200, name="action") in body


def test_the_chips_show_when_the_query_was_too_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    def too_slow(_read: object) -> object:
        raise ReportTooSlow

    monkeypatch.setattr(audit_views, "bounded", too_slow)
    body = page(admin(), "/audit/?action=kept.*")
    assert 'id="audit-too-slow"' in body
    assert [words for _, words in chips(body)] == ["Action: kept.*"]


def test_clear_keeps_the_scoped_links_filters_and_drops_a_malformed_one() -> None:
    store, owner = uuid.uuid4(), uuid.uuid4()
    body = page(admin(), f"/audit/?documents_of=store:{owner}&store_of={store}&action=x")
    assert params(clear_url(body)) == {
        "documents_of": [f"store:{owner}"],
        "store_of": [str(store)],
    }
    # The scoped values are no chips; the hidden inputs stay inside the component's form.
    assert [words for _, words in chips(body)] == ["Action: x"]
    form = element(body, '<form method="get" action="/audit/" id="filters"', "form")
    assert f'<input type="hidden" name="documents_of" value="store:{owner}">' in form
    assert f'<input type="hidden" name="store_of" value="{store}">' in form
    body = page(admin(), "/audit/?documents_of=<x>&store_of=nope")
    assert clear_url(body) == "/audit/"
    assert audit.BAD_OWNER in body


def test_the_activity_log_escapes_a_value_in_its_chip_and_its_field() -> None:
    body = page(admin(), "/audit/?action=<script>alert(1)</script>&user=%22><img src=x onerror=1>")
    assert "<script>alert(1)</script>" not in body and "<img src=x" not in body
    assert "Action: &lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert 'value="&lt;script&gt;alert(1)&lt;/script&gt;"' in body
    assert "User: &quot;&gt;&lt;img src=x onerror=1&gt;" in body
    assert 'value="&quot;&gt;&lt;img src=x onerror=1&gt;"' in body


def test_a_chip_link_is_a_local_path() -> None:
    body = page(admin(), "/audit/?action=//evil.example.com/x&user=http://evil.example.com")
    for href, _ in chips(body):
        assert href.startswith("/audit/?") and not href.startswith("//")
        assert urlsplit(href).netloc == ""
    assert clear_url(body) == "/audit/"


def test_the_activity_log_export_is_in_the_header_with_the_same_query() -> None:
    body = page(admin(), "/audit/?action=kept.*&page=3")
    actions = page_actions(body)
    found = re.search(
        r'<a class="btn" id="audit-export" href="([^"]*)">Download spreadsheet</a>', actions
    )
    assert found is not None
    href = html_lib.unescape(found.group(1))
    assert href.startswith("/audit/export.csv?")
    query = audit.parse_query({"action": "kept.*"}).as_query(page=1)
    assert params(href) == {name: [value] for name, value in query.items()}
    assert body.count('id="audit-export"') == 1


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_the_activity_log_stays_admin_only(role: Role) -> None:
    assert client_for(role)[0].get("/audit/?action=x").status_code == 403


def test_messages_without_a_filter_show_no_chip() -> None:
    body = page(client_for(Role.VIEWER)[0], "/webhooks/")
    assert chips(body) == [] and toggle(body) == "Filters"
    assert '<form method="get" action="/webhooks/" id="filters"' in body
    assert ">Filter</button>" not in body and "finance/filters.js" not in body
    assert "No messages from" in body


@pytest.mark.parametrize(
    ("value", "words"),
    [("processed", "Processed"), ("dead_letter", "Gave up"), ("received", "Received")],
)
def test_each_messages_outcome_has_its_chip_in_words(value: str, words: str) -> None:
    an_event(value, "update_invoice")
    body = page(
        client_for(Role.VIEWER)[0], f"/webhooks/?outcome={value}&type=update_invoice&page=1"
    )
    found = chips(body)
    assert [w for _, w in found] == [f"Outcome: {words}", "Type: update_invoice"]
    assert toggle(body) == "Filters · 2"
    assert params(found[0][0]) == {"type": ["update_invoice"]}
    assert params(found[1][0]) == {"outcome": [value]}


def test_an_unknown_outcome_or_type_has_no_chip() -> None:
    body = page(client_for(Role.VIEWER)[0], "/webhooks/?outcome=nonsense&type=<b>bad</b>")
    assert chips(body) == [] and "<b>bad</b>" not in body
    assert clear_url(body) == "/webhooks/"


def test_the_messages_export_is_in_the_header_with_the_same_query() -> None:
    body = page(client_for(Role.VIEWER)[0], "/webhooks/?outcome=failed&type=update_invoice")
    actions = page_actions(body)
    assert (
        '<a class="btn" id="export-csv" href="/webhooks/export.csv?outcome=failed&amp;'
        'type=update_invoice">Download spreadsheet</a>'
    ) in actions
    assert body.count('id="export-csv"') == 1


# --- D: the template status chip -------------------------------------------------------------


def test_only_the_templates_table_chip_is_drawn_on_its_row_text_line() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    rule = "#templates-table .template-status .status-chip { position: relative; top: -2px; }"
    assert css.count(rule) == 1
    # No other rule moves a chip: the only chip rules that place it are the component's own.
    assert re.findall(r"[^\n{}]*\.status-chip[^\n{}]*\{[^}]*\b(?:top|position)\b[^}]*\}", css) == [
        rule
    ]
    # Outside the phone block, as no phone rule is needed.
    assert css.index(rule) < css.index("@media (max-width: 719.98px) {")
    MessageTemplate.objects.get_or_create(
        code="visual_check", defaults={"name": "Check", "subject": "S", "body": "B"}
    )
    body = page(admin(), reverse("ledger:settings:templates"))
    # Re-pinned (Fable, small gate run 1): the cell is named by its own class, so the stylesheet
    # names no data-role outside the phone block; the role is kept for the phone cards.
    cells = re.findall(r"<td ([^>]*)>(<span class=\"status-chip status-chip-[^\"]+\">)", body)
    assert cells and all(
        attrs == 'data-role="status" class="template-status" data-label="Status"'
        for attrs, _ in cells
    ), cells
    assert body.count('class="template-status"') == len(cells)
