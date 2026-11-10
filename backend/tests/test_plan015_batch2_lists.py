"""PLAN-015 Batch 2 (docs/qa/15/PLAN-015-batch-2-lists.md): the list pages in the dashboard's
language — the shared header (the title block with the counts as its muted sub-line, then the
actions: a segmented control where the page has one, "Download spreadsheet" with the dashboard's
icon, the page's other buttons, the primary last), the Payments totals as KPI cards v2 and its
period links, the Operators avatar tile, and the status chips of the Outbox, Bank deposits and
the Invoices tab, with the Bank deposits state as a segmented control."""

from __future__ import annotations

import dataclasses
import html as html_module
import re
from collections.abc import Sequence
from datetime import date

import pytest
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from banking import services as bank_services
from banking.models import BankTransaction, Confidence
from ledger.models import (
    HeldReason,
    InvoiceStatus,
    MessageStatus,
    Operator,
    OutboundMessage,
    PaymentStatus,
)
from ledger.services import messaging
from ledger.services.payments_ledger import PaymentFilter, parse_filter
from ledger.views.outbox import STATUS_TONES, status_cell
from ledger.views.payments import period_links
from tests.receivables_helpers import (
    a_lease,
    a_payment,
    a_rent_charge,
    a_store,
    an_invoice,
    an_operator,
)
from tests.test_banking import OCTOBER, deposit, lease_at, owner
from tests.test_operator_views import client_for
from tests.test_plan014_d_messaging import a_world, at, queued

pytestmark = pytest.mark.django_db

TODAY = date(2026, 11, 10)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def page(client: Client, path: str, expect: int = 200) -> str:
    response = client.get(path)
    assert response.status_code == expect, (path, response.status_code)
    return response.content.decode()


def header_of(html: str) -> str:
    start = html.index('<div class="page-header">')
    return html[start : html.index("</header>", start)]


# --- §3.1 The header ------------------------------------------------------------------------------

#: The six pages, the title, the counts sentence with one lease in the data and whether the
#: page has "Download spreadsheet". The seventh list of the plan, the invoices, is the store and
#: operator pages' Invoices tab: its header is the workspace's (Batch 3).
HEADERS = [
    ("/stores/", "Stores", "1 store · 1 with a lease · 0 vacant", True),
    ("/operators/", "Operators", "1 operator", True),
    ("/leases/", "Leases", "1 lease", True),
    ("/payments/", "Payments", "0 payments", True),
    ("/admin-settings/bank-deposits/", "Bank deposits", "0 deposits", False),
    ("/admin-settings/outbox/", "Outbox", "0 messages", False),
]


@pytest.mark.parametrize("role", [Role.BILLING, Role.ADMIN])
@pytest.mark.parametrize(("path", "title", "counts", "export"), HEADERS)
def test_the_list_pages_share_the_dashboards_header(
    role: Role, path: str, title: str, counts: str, export: bool
) -> None:
    a_lease(a_store("2601 HEAD", 2601), an_operator("Head Co"))
    client = client_for(role)[0]
    if path == "/admin-settings/outbox/" and role != Role.ADMIN:
        page(client, path, expect=403)
        return
    header = header_of(page(client, path))
    assert header.startswith(
        f'<div class="page-header"><div class="page-title-block"><h1>{title}</h1>'
        f'<p class="page-sub" id="list-counts">{counts}</p></div><div class="page-actions">'
    ), header[:300]
    assert 'class="list-counts"' not in header and 'class="page-title"' not in header
    actions = header[header.index('<div class="page-actions">') :]
    if export:
        button = re.search(r'<a class="btn" id="export-csv" href="[^"]+">(.*?)</a>', actions)
        assert button is not None
        assert button[1].startswith('<svg class="icon icon-file" width="16" height="16"')
        assert button[1].endswith("</svg>Download spreadsheet")
    else:
        assert 'id="export-csv"' not in actions
    if "btn-primary" in actions:  # the primary "+ New …" comes last
        assert actions.index('id="export-csv"') < actions.index('class="btn btn-primary"')
        assert actions.count('class="btn btn-primary"') == 1
        assert actions.index('class="btn btn-primary"') > actions.rindex('class="btn"')


# --- §3.2 The Payments totals as KPI cards v2 -----------------------------------------------------


def totals_of(html: str) -> str:
    start = html.index('id="payments-totals"')
    return html[start : html.index("</section>", start)]


def figure(totals: str, value_id: str) -> tuple[str, str]:
    """(the figure's tone classes, its text) of the kpi2 value named ``value_id``."""
    found = re.search(rf'<span class="kpi2-value([^"]*)" id="{value_id}">([^<]*)</span>', totals)
    assert found is not None, value_id
    return found[1].strip(), found[2]


CARDS = [
    ("banknote", "Settled", "total-settled"),
    ("circle-half", "Pending", "total-pending"),
    ("alert", "Returned", "total-returned"),
    ("dash", "Refunded", "total-refunded"),
    ("lock", "Deposits held", "total-deposits-held"),
]


def test_the_totals_are_five_kpi2_cards_with_their_icons_titles_ids_and_tones() -> None:
    lease = a_lease(a_store("2602 KPI", 2602), an_operator("Kpi Co"))
    charge = a_rent_charge(lease, date(2026, 10, 1), due="900.00")
    a_payment(charge, "300.00", date(2026, 10, 2))
    a_payment(charge, "120.00", None, status=PaymentStatus.PENDING, initiated_on=date(2026, 10, 3))
    a_payment(charge, "75.00", None, status=PaymentStatus.RETURNED, initiated_on=date(2026, 10, 4))
    html = page(client_for(Role.BILLING)[0], "/payments/")
    head = html[html.index('id="payment-totals-card"') : html.index('id="payments-totals"')]
    assert (
        '<p class="section-label" id="payment-totals-label">Totals of the 3 payments shown</p>'
        in head
    )
    assert '<div class="card-head" id="payment-totals-card">' in html
    assert 'role="tooltip" id="totals-help"' in head and 'aria-describedby="totals-help"' in head
    totals = totals_of(html)
    assert totals.startswith('id="payments-totals" aria-labelledby="payment-totals-label">')
    assert html[: html.index('id="payments-totals"')].endswith('<section class="kpi-row kpi2-row" ')
    cards = totals.split('<div class="kpi2">')[1:]
    assert len(cards) == 5
    for card, (icon, title, value_id) in zip(cards, CARDS, strict=True):
        assert f'class="icon icon-{icon}"' in card, title
        assert f'<p class="kpi2-title">{title}</p>' in card
        assert "kpi2-sub" not in card and "kpi2-link" not in card  # no note, no link
        assert f'id="{value_id}"' in card
    assert figure(totals, "total-settled") == ("tone-paid", "$300.00")
    assert figure(totals, "total-pending") == ("", "$120.00")
    assert figure(totals, "total-returned") == ("tone-unpaid", "$75.00")
    assert figure(totals, "total-refunded") == ("tone-muted", "$0.00")
    assert figure(totals, "total-deposits-held") == ("tone-muted", "$0.00")
    # filters.js swaps #results: the cards are inside it, so a filter refreshes them.
    results = html.index('<div id="results">')
    assert results < html.index('id="payment-totals-card"') < html.index('<table id="payments"')
    assert 'class="tile' not in totals and 'id="payment-totals-card" class="card"' not in html


def test_with_no_payment_every_figure_is_a_muted_zero_and_the_table_says_so() -> None:
    html = page(client_for(Role.VIEWER)[0], "/payments/")
    totals = totals_of(html)
    for _, _, value_id in CARDS:
        assert figure(totals, value_id) == ("tone-muted", "$0.00"), value_id
    assert "Totals of the 0 payments shown" in html and "No payments match." in html


# --- §3.3 The Payments period links ---------------------------------------------------------------

MONTH = ("2026-11-01", "2026-11-30")
QUARTER = ("2026-10-01", "2026-12-31")  # the quarter at the year's edge
YEAR = ("2026-01-01", "2026-12-31")


def links_of(html: str) -> list[tuple[str, str, bool]]:
    start = html.index('<div class="segmented period-control" id="payments-period"')
    control = html[start : html.index("</div>", start)]
    assert control.startswith(
        '<div class="segmented period-control" id="payments-period" role="group" '
        'aria-label="Period">'
    )
    return [
        (label, html_module.unescape(href), bool(current))
        for href, current, label in re.findall(
            r'<a href="([^"]*)"( aria-current="page")?>([^<]+)</a>', control
        )
    ]


def test_the_four_links_sit_before_download_and_all_time_is_active_without_dates() -> None:
    html = page(client_for(Role.VIEWER)[0], "/payments/")
    actions = header_of(html)[header_of(html).index('<div class="page-actions">') :]
    assert actions.index('id="payments-period"') < actions.index('id="export-csv"')
    assert links_of(html) == [
        ("All time", "/payments/", True),
        ("This month", f"/payments/?date_from={MONTH[0]}&date_to={MONTH[1]}", False),
        ("Quarter", f"/payments/?date_from={QUARTER[0]}&date_to={QUARTER[1]}", False),
        ("Year", f"/payments/?date_from={YEAR[0]}&date_to={YEAR[1]}", False),
    ]


@pytest.mark.parametrize(
    ("dates", "active"),
    [(MONTH, "This month"), (QUARTER, "Quarter"), (YEAR, "Year")],
)
def test_each_period_is_active_when_the_dates_are_exactly_its_window(
    dates: tuple[str, str], active: str
) -> None:
    html = page(client_for(Role.VIEWER)[0], f"/payments/?date_from={dates[0]}&date_to={dates[1]}")
    assert [label for label, _, current in links_of(html) if current] == [active]


@pytest.mark.parametrize(
    "query",
    [
        "date_from=2026-11-01&date_to=2026-11-29",  # one day short of the month
        "date_from=2026-11-01",  # an open end
        "date_to=2026-12-31",
        "date_from=2026-10-01&date_to=2026-11-30",
        "date_from=2025-11-01&date_to=2025-11-30",  # last year's November
        "date_from=garbage&date_to=2026-11-30",
    ],
)
def test_any_other_range_leaves_every_link_inactive(query: str) -> None:
    html = page(client_for(Role.VIEWER)[0], f"/payments/?{query}")
    assert [label for label, _, current in links_of(html) if current] == []


def test_a_garbage_date_is_dropped_by_the_parse_so_the_list_is_all_time() -> None:
    # The existing parse drops an unusable date (the page stays green); the list shown is then
    # every payment, and All time says so. Nothing of the garbage is echoed in a link.
    html = page(client_for(Role.VIEWER)[0], "/payments/?date_from=garbage&date_to=%3Cscript%3E")
    links = links_of(html)
    assert [label for label, _, current in links if current] == ["All time"]
    assert all("garbage" not in href and "script" not in href for _, href, _ in links)


def test_every_link_keeps_the_other_filters_and_date_by_and_drops_the_page() -> None:
    operator = an_operator("Keep Co")
    query = (
        f"date_by=initiated&date_from=2026-11-01&date_to=2026-11-30&method=zelle"
        f"&status=settled&operator={operator.pk}&q=keep&page=2"
    )
    links = links_of(page(client_for(Role.VIEWER)[0], f"/payments/?{query}"))
    kept = f"method=zelle&operator={operator.pk}&status=settled&q=keep"
    assert links == [
        ("All time", f"/payments/?date_by=initiated&{kept}", False),
        (
            "This month",
            f"/payments/?date_by=initiated&date_from={MONTH[0]}&date_to={MONTH[1]}&{kept}",
            True,
        ),
        (
            "Quarter",
            f"/payments/?date_by=initiated&date_from={QUARTER[0]}&date_to={QUARTER[1]}&{kept}",
            False,
        ),
        (
            "Year",
            f"/payments/?date_by=initiated&date_from={YEAR[0]}&date_to={YEAR[1]}&{kept}",
            False,
        ),
    ]


def test_the_windows_follow_today_and_run_no_query() -> None:
    selected = parse_filter({"date_from": "2026-01-01", "date_to": "2026-03-31"})
    with CaptureQueriesContext(connection) as captured:
        links = period_links(selected, date(2026, 2, 28))
    assert captured.captured_queries == []
    assert links == [
        ("All time", "/payments/", False),
        ("This month", "/payments/?date_from=2026-02-01&date_to=2026-02-28", False),
        ("Quarter", "/payments/?date_from=2026-01-01&date_to=2026-03-31", True),
        ("Year", "/payments/?date_from=2026-01-01&date_to=2026-12-31", False),
    ]
    assert period_links(PaymentFilter(), date(2028, 2, 29))[1][1].endswith("date_to=2028-02-29")


def test_the_period_links_add_no_query_to_the_list() -> None:
    client = client_for(Role.VIEWER)[0]
    client.get("/payments/")  # warm the session

    def count(path: str) -> int:
        with CaptureQueriesContext(connection) as captured:
            assert client.get(path).status_code == 200
        return len(captured.captured_queries)

    plain = count("/payments/")
    assert count(f"/payments/?date_from={MONTH[0]}&date_to={MONTH[1]}") == plain


# --- §3.4 The Operators list's avatar tile --------------------------------------------------------


def operator_cell(html: str, operator: Operator) -> str:
    link = f'href="/operators/{operator.pk}/">'
    at = html.index(link, html.index('<table id="operators"'))
    start = html.rindex("<td", 0, at)
    return html[start : html.index("</td>", at)]


@pytest.mark.parametrize(
    ("name", "initials"),
    [
        ("Acme", "AC"),  # one word: its first two letters
        ("Kaya Holdings", "KH"),
        ("Atlas Retail Group", "AR"),  # the first letters of the first two words
        ("!!! ...", "—"),  # no letter: the component's dash
    ],
)
def test_the_operator_cell_leads_with_the_avatar_tile_beside_the_name(
    name: str, initials: str
) -> None:
    operator = an_operator(name)
    cell = operator_cell(page(client_for(Role.VIEWER)[0], "/operators/"), operator)
    assert re.search(
        r'<span class="cell-title-avatar"><span class="avatar-tile" aria-hidden="true">'
        + re.escape(initials)
        + r'</span><a class="cell-title" href="/operators/[^"]+/">'
        + re.escape(html_module.escape(name))
        + "</a></span>$",
        cell,
    ), cell


def test_the_archived_and_needs_review_badges_stay_on_the_avatar_line() -> None:
    operator = an_operator("Old Ops")
    Operator.objects.filter(pk=operator.pk).update(
        archived_at="2026-10-01T00:00:00Z", needs_review=True
    )
    cell = operator_cell(page(client_for(Role.VIEWER)[0], "/operators/?archived=1"), operator)
    line = cell[cell.index('<span class="cell-title-avatar">') :]
    assert line.index('class="avatar-tile"') < line.index('class="cell-title"')
    assert line.index('class="cell-title"') < line.index('<span class="badge">Archived</span>')
    assert "Needs review" in line and line.endswith("</span>")


# --- §3.5 The Outbox's status chips ---------------------------------------------------------------


def test_every_message_status_has_a_chip_tone() -> None:
    assert set(STATUS_TONES) == set(MessageStatus.values)
    assert STATUS_TONES == {
        "queued": "pending",
        "sent": "paid",
        "failed": "unpaid",
        "suppressed": "neutral",
        "catch_all": "neutral",
        "held": "pending",
        "waiting": "pending",
        "combined": "neutral",
    }


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"status": MessageStatus.QUEUED}, ("pending", "Waiting", "")),
        ({"status": MessageStatus.SENT}, ("paid", "Sent", "")),
        (
            {"status": MessageStatus.FAILED, "error": "ConnectTimeout"},
            ("unpaid", "Failed", "ConnectTimeout"),
        ),
        ({"status": MessageStatus.FAILED}, ("unpaid", "Failed", "")),
        ({"status": MessageStatus.SUPPRESSED}, ("neutral", "Suppressed", "")),
        ({"status": MessageStatus.CATCH_ALL}, ("neutral", "Sent to the test inbox", "")),
        (
            {"status": MessageStatus.HELD, "held_reason": HeldReason.SANDBOX},
            ("pending", "Held", "held on sandbox"),
        ),
        (
            {"status": MessageStatus.HELD, "held_reason": HeldReason.NOT_SET_UP},
            ("pending", "Held", "not sent (mail not set up)"),
        ),
        (
            {"status": MessageStatus.WAITING, "scheduled_for": date(2026, 11, 5)},
            ("pending", "Waiting for the 3-day fold", "until Nov 5, 2026"),
        ),
        ({"status": MessageStatus.COMBINED}, ("neutral", "Combined", "")),
    ],
)
def test_the_status_cell_is_the_tone_the_word_and_the_rest_of_the_status_text(
    fields: dict[str, object], expected: tuple[str, str, str]
) -> None:
    assert status_cell(OutboundMessage(**fields)) == expected


def test_a_combined_row_keeps_the_whole_combined_sentence_for_its_link() -> None:
    primary = OutboundMessage(rule="D", status=MessageStatus.SENT)
    combined = OutboundMessage(status=MessageStatus.COMBINED, combined_into=primary)
    assert status_cell(combined) == ("neutral", "Combined", "Combined into the D message: Sent")


def outbox_row(html: str, message: OutboundMessage) -> str:
    start = html.index(f'<tr id="message-{message.pk}">')
    return html[start : html.index("</tr>", start)]


def status_td(row: str) -> str:
    found = re.search(r'<td data-role="status"[^>]*>(.*?)</td>', row, re.S)
    assert found is not None, row
    return found[1]


def chip_html(tone: str, word: str) -> tuple[str, str]:
    """The chip's opening (its pair and glyph) and its end (the words), as {% chip %} draws them."""
    glyph = {"paid": "check", "neutral": "circle"}.get(tone, "dot")
    return (
        f'<span class="status-chip status-chip-{tone}"><svg class="icon icon-{glyph}" width="12"'
    ), f"<span>{word}</span></span>"


def test_the_outbox_page_draws_each_status_as_a_chip_over_its_rest() -> None:
    lease, charge = a_world()
    with override_settings(MAIL_ENABLED=False):
        message = queued(lease, charge, now=at(10))
        messaging.send(message.pk, now=at(10))
    admin = client_for(Role.ADMIN)[0]
    cases = [
        (
            {"status": "held", "held_reason": "not_set_up"},
            "pending",
            "Held",
            "not sent (mail not set up)",
        ),
        ({"status": "sent", "held_reason": ""}, "paid", "Sent", ""),
        (
            {"status": "failed", "held_reason": "", "error": "Timeout"},
            "unpaid",
            "Failed",
            "Timeout",
        ),
        ({"status": "queued", "error": ""}, "pending", "Waiting", ""),
    ]
    for fields, tone, word, rest in cases:
        OutboundMessage.objects.filter(pk=message.pk).update(**fields)
        cell = status_td(outbox_row(page(admin, "/admin-settings/outbox/"), message))
        start, end = chip_html(tone, word)
        assert cell.startswith(start) and end in cell, (fields, cell)
        if rest:
            assert cell.endswith(f'</span><span class="cell-subline">{rest}</span>'), cell
        else:
            assert "cell-subline" not in cell, cell


def test_the_outbox_combined_row_links_its_rest_to_the_message_sent_for_it() -> None:
    lease, charge = a_world()
    with override_settings(MAIL_ENABLED=False):
        primary = queued(lease, charge, rule="D", now=at(10))
        other = queued(lease, charge, rule="C", now=at(10))
        messaging.send(primary.pk, now=at(10))
    OutboundMessage.objects.filter(pk=primary.pk).update(
        status=MessageStatus.HELD, held_reason=HeldReason.NOT_SET_UP
    )
    OutboundMessage.objects.filter(pk=other.pk).update(
        status=MessageStatus.COMBINED, held_reason="", combined_into=primary
    )
    cell = status_td(outbox_row(page(client_for(Role.ADMIN)[0], "/admin-settings/outbox/"), other))
    start, end = chip_html("neutral", "Combined")
    assert cell.startswith(start) and end in cell
    assert cell.endswith(
        f'<span class="cell-subline"><a href="#message-{primary.pk}">'
        "Combined into the D message: Held (not sent (mail not set up))</a></span>"
    ), cell


# --- §3.5 and §3.6 Bank deposits ------------------------------------------------------------------


def suggestion_of(html: str, txn: BankTransaction) -> str:
    start = html.index(f'<tr id="deposit-{txn.pk}">')
    return status_td(html[start : html.index("</tr>", start)])


@pytest.mark.parametrize(
    ("confidence", "tone", "word"),
    [
        (Confidence.EXACT, "paid", "Exact"),
        (Confidence.STRONG, "pending", "Strong"),
        (Confidence.WEAK, "partial", "Weak"),
    ],
)
def test_a_suggestion_leads_with_its_confidence_chip(
    confidence: str, tone: str, word: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = lease_at(2611, "Chip")
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    an_invoice(owner(lease), 611, "1200.00", rent_charge=charge)
    txn = deposit("1200.00", description="WIRE INV-2026-0611")
    real = bank_services.rows

    def with_confidence(
        transactions: Sequence[BankTransaction], today: date
    ) -> list[bank_services.Row]:
        # The page's rows as the matcher made them, the suggestion's confidence set to the case.
        found = real(transactions, today)
        assert found and all(row.suggestion and row.suggestion.lease for row in found)
        return [
            dataclasses.replace(
                row, suggestion=dataclasses.replace(row.suggestion, confidence=confidence)
            )
            if row.suggestion is not None
            else row
            for row in found
        ]

    monkeypatch.setattr(bank_services, "rows", with_confidence)
    cell = suggestion_of(page(client_for(Role.BILLING)[0], "/admin-settings/bank-deposits/"), txn)
    start, end = chip_html(tone, word)
    assert cell.startswith(start) and end in cell, cell
    assert "#2611 Chip" in cell and "badge" not in cell


def test_no_match_is_a_neutral_chip_and_the_kind_stays_a_badge() -> None:
    txn = deposit("13.37", description="UNKNOWN SENDER")
    html = page(client_for(Role.BILLING)[0], "/admin-settings/bank-deposits/")
    cell = suggestion_of(html, txn)
    start, end = chip_html("neutral", "No match")
    assert cell.startswith(start) and end in cell, cell
    row = html[html.index(f'<tr id="deposit-{txn.pk}">') :]
    assert re.search(r'<span class="badge">[A-Z][a-z]+</span>', row[: row.index("</tr>")])


STATES = [
    ("new", "New"),
    ("confirmed", "Confirmed"),
    ("split", "Split"),
    ("ignored", "Ignored"),
    ("all", "All"),
]


def states_of(html: str) -> list[tuple[str, str, bool]]:
    start = html.index('<div class="segmented" id="deposit-states"')
    control = html[start : html.index("</div>", start)]
    assert control.startswith(
        '<div class="segmented" id="deposit-states" role="group" aria-label="State">'
    )
    return [
        (html_module.unescape(href), label, bool(current))
        for href, current, label in re.findall(
            r'<a href="([^"]*)"( aria-current="page")?>([^<]+)</a>', control
        )
    ]


@pytest.mark.parametrize("current", [value for value, _ in STATES])
def test_the_state_is_a_segmented_control_of_links_before_fetch_now(current: str) -> None:
    html = page(client_for(Role.BILLING)[0], f"/admin-settings/bank-deposits/?status={current}")
    assert states_of(html) == [
        (f"/admin-settings/bank-deposits/?status={value}", label, value == current)
        for value, label in STATES
    ]
    actions = header_of(html)[header_of(html).index('<div class="page-actions">') :]
    assert actions.index('id="deposit-states"') < actions.index('id="fetch-now"')


def test_the_filter_card_is_gone_and_new_is_the_default() -> None:
    html = page(client_for(Role.BILLING)[0], "/admin-settings/bank-deposits/")
    assert 'class="card filter-card"' not in html and 'id="filters"' not in html
    assert "filters.js" not in html and 'class="filters-chips"' not in html
    assert [label for _, label, current in states_of(html) if current] == ["New"]


def test_a_hostile_state_is_refused_as_today_and_never_echoed() -> None:
    html = page(client_for(Role.BILLING)[0], "/admin-settings/bank-deposits/?status=%3Cscript%3E")
    assert "<script>" not in html.split("</header>")[0].split('id="page-header"')[-1]
    assert "&lt;script&gt;" not in html and "%3Cscript%3E" not in html
    # The view falls back to New, so the list shown is the new deposits and New says so.
    assert [label for _, label, current in states_of(html) if current] == ["New"]


# --- §3.5 The Invoices tab's status chips ---------------------------------------------------------

INVOICE_CHIPS = [
    (InvoiceStatus.DRAFT, "neutral", "Draft"),
    (InvoiceStatus.READY, "neutral", "Ready"),
    (InvoiceStatus.SENT, "unpaid", "Sent"),
    (InvoiceStatus.CLOSED, "neutral", "Closed"),
    (InvoiceStatus.VOID, "neutral", "Void"),
]


@pytest.mark.parametrize("owner", ["store", "operator"])
def test_the_invoices_tab_draws_a_chip_for_each_internal_status(owner: str) -> None:
    operator = an_operator("Tab Co")
    lease = a_lease(a_store("2612 TAB", 2612), operator)
    invoices = {}
    for number, (status, _, _) in enumerate(INVOICE_CHIPS, start=1):
        charge = a_rent_charge(lease, date(2026, number, 1), due="100.00")
        invoices[status] = an_invoice(
            operator, 6120 + number, "100.00", rent_charge=charge, internal_status=status
        )
    path = f"/stores/{lease.store_id}/" if owner == "store" else f"/operators/{lease.operator_id}/"
    html = page(client_for(Role.BILLING)[0], f"{path}?tab=invoices&filter=all")
    at = html.index('<table id="store-invoices"')
    table = html[at : html.index("</table>", at)]
    for status, tone, word in INVOICE_CHIPS:
        row = table[table.index(f">{invoices[status].invoice_number}<") :]
        row = row[: row.index("</tr>")]
        cell = status_td(row)
        start, end = chip_html(tone, word)
        assert cell.startswith(start) and cell.endswith(end), (status, cell)
