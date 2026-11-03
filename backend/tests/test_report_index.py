"""The Reports index of docs/qa/PLAN-014b item 2: three groups of cards, each one link with its
icon and title on one row, description, live figure and sub-line beside "Open ›" (as
docs/design/reports-dark-laptop.png); the Customer statement card's operator search under its
link; the header line. Every figure equals what its report's own
page shows for the same day or month (the reconciliation tests); the query count does not grow
with the data; the figures run under the reports' statement timeout."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.roles import Role
from finance.dates import day_short_text
from finance.templatetags.money import money
from ledger.models import (
    CalendarEntry,
    CalendarEntryKind,
    CollectionStatus,
    Operator,
    RentChargeStatus,
    Store,
)
from ledger.services import report_index
from ledger.views import reports as report_views
from payments.models import (
    MismatchKind,
    ReconciliationMismatch,
    ReconciliationRun,
    ReconciliationStatus,
    ReconciliationTrigger,
)
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_operator,
)
from tests.test_operator_views import client_for
from tests.test_pnl import a_cost

pytestmark = pytest.mark.django_db

D = Decimal
#: A Tuesday: the calendar's next seven days are Oct 7 to Oct 13.
TODAY = date(2026, 10, 6)
SEP, OCT = date(2026, 9, 1), date(2026, 10, 1)
THIS_MONTH = {"from": "2026-10", "to": "2026-10"}
INDEX = "/reports/"
CARDS = ("aging", "payments-by-method", "deposits", "revenue", "pnl", "calendar")


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def viewer() -> Client:
    return client_for(Role.VIEWER)[0]


def get(client: Client, path: str, params: dict[str, str] | None = None) -> Any:
    response = client.get(path, params or {})
    assert response.status_code == 200, path
    return response


def card(html: str, slug: str) -> str:
    """The card's link: from ``id="report-<slug>"`` to its closing ``</a>``."""
    match = re.search(rf'<a class="report-card[^"]*" id="report-{slug}"[^>]*>(.*?)</a>', html, re.S)
    assert match is not None, slug
    return match.group(1)


def value_and_sub(html: str, slug: str) -> tuple[str, str]:
    body = card(html, slug)
    value = re.search(r'<span class="report-value[^"]*">(.*?)</span>', body, re.S)
    sub = re.search(r'<span class="report-sub">(.*?)</span>', body, re.S)
    assert value is not None and sub is not None, slug
    return value.group(1).strip(), sub.group(1).strip()


def a_finding(kind: str, resolved: bool = False) -> None:
    run = ReconciliationRun.objects.create(
        trigger=ReconciliationTrigger.SCHEDULED,
        status=ReconciliationStatus.DONE,
        started_at=datetime(2026, 10, 1, 6, tzinfo=UTC),
    )
    ReconciliationMismatch.objects.create(
        run=run,
        kind=kind,
        entity_type="payment",
        entity_id=uuid.uuid4(),
        detail=f"{kind} detail",
        resolved_at=timezone.now() if resolved else None,
        resolution_note="Checked" if resolved else "",
    )


def build(count: int, start: int = 0) -> list[Store]:
    """``count`` stores, each with a lease debited on the 10th (in the next seven days), an
    unpaid September rent, a part-paid October rent paid this month, a damage charge, and a
    landlord cost; and one open bank finding a store."""
    made = []
    for n in range(start, start + count):
        operator = an_operator(f"Index Operator {n}")
        store = a_store(f"Index {n:03d}", 700 + n)
        lease = a_lease(store, operator, debit_day=10, rent="1000.00")
        a_rent_charge(lease, SEP, RentChargeStatus.UNPAID, "1000.00")
        october = a_rent_charge(lease, OCT, RentChargeStatus.PARTIAL, "1000.00", "400.00")
        a_payment(october, "400.00", date(2026, 10, 2))
        a_one_off_charge(operator, "125.00", date(2026, 9, 20), lease=lease)
        a_cost(store, "600.00")
        a_finding(MismatchKind.DEPOSIT_UNMATCHED)
        made.append(store)
    return made


# --- Each figure equals its report's own -------------------------------------------------------


def test_the_aging_figure_is_the_aging_pages_total_and_store_count() -> None:
    build(3)
    client = viewer()
    report = get(client, "/reports/aging/").context["report"]
    stores = [row for row in report.rows if row.store_id is not None]
    figures = get(client, INDEX).context["live"]
    assert (figures.aging_total, figures.aging_stores) == (report.totals.total, len(stores))
    assert figures.aging_total == D("3") * (D("1000.00") + D("600.00") + D("125.00"))
    html = get(client, INDEX).content.decode()
    assert value_and_sub(html, "aging") == (money(report.totals.total), "unpaid · 3 stores")
    assert 'class="report-value tone-unpaid"' in card(html, "aging")


def test_the_collected_figure_is_payments_by_method_for_this_month() -> None:
    build(2)
    a_payment(a_rent_charge(a_lease(), SEP, due="50.00"), "50.00", date(2026, 9, 30))
    client = viewer()
    report = get(client, "/reports/payments-by-method/", THIS_MONTH).context["report"]
    figures = get(client, INDEX).context["live"]
    assert figures.collected_month == report.total.total == D("800.00")
    html = get(client, INDEX).content.decode()
    assert value_and_sub(html, "payments-by-method") == ("$800.00", "collected in October")


def test_the_unmatched_figure_is_the_bank_account_checks_list() -> None:
    build(2)
    a_finding(MismatchKind.SETTLED_PAYMENT_UNDEPOSITED)
    a_finding(MismatchKind.DEPOSIT_UNMATCHED, resolved=True)
    a_finding(MismatchKind.PENDING_TOO_OLD)  # not an unmatched kind
    client = viewer()
    report = get(client, "/reports/deposits/", THIS_MONTH).context["report"]
    figures = get(client, INDEX).context["live"]
    assert figures.unmatched == len(report.unmatched) == 3
    html = get(client, INDEX).content.decode()
    since = day_short_text(report.unmatched[0].found_on)
    assert figures.unmatched_since == report.unmatched[0].found_on
    assert value_and_sub(html, "deposits") == ("3 unmatched", f"since {since}")
    assert 'class="report-value tone-pending"' in card(html, "deposits")


def test_the_billed_figure_is_revenue_by_month_for_this_month() -> None:
    build(2)
    client = viewer()
    report = get(client, "/reports/revenue/", THIS_MONTH).context["report"]
    figures = get(client, INDEX).context["live"]
    assert figures.billed_month == report.total.total_billed
    assert figures.billed_month > 0
    html = get(client, INDEX).content.decode()
    assert value_and_sub(html, "revenue") == (money(report.total.total_billed), "billed in October")


def test_the_margin_figure_is_the_pnl_pages_billed_margin_for_this_month() -> None:
    build(2)
    client = viewer()
    total = get(client, "/reports/pnl/", THIS_MONTH).context["total"]
    figures = get(client, INDEX).context["live"]
    assert total.margin_billed is not None
    assert (figures.margin_month, figures.margin_missing) == (total.margin_billed, "")
    html = get(client, INDEX).content.decode()
    assert value_and_sub(html, "pnl") == (money(total.margin_billed), "October margin")
    assert 'class="report-value tone-paid"' in card(html, "pnl")


def test_an_unknown_cost_shows_a_dash_and_names_what_is_missing() -> None:
    lease = a_lease(a_store("No cost", 801), an_operator("No Cost Co"))
    a_rent_charge(lease, OCT, RentChargeStatus.UNPAID, "900.00")
    client = viewer()
    total = get(client, "/reports/pnl/", THIS_MONTH).context["total"]
    figures = get(client, INDEX).context["live"]
    assert total.margin_billed is None and figures.margin_month is None
    assert figures.margin_missing == ", ".join(total.unknown) != ""
    html = get(client, INDEX).content.decode()
    assert value_and_sub(html, "pnl") == ("—", f"margin unknown: {', '.join(total.unknown)}")


def test_a_negative_margin_takes_the_unpaid_tone() -> None:
    lease = a_lease(a_store("Loss", 802), an_operator("Loss Co"))
    a_rent_charge(lease, OCT, RentChargeStatus.UNPAID, "100.00")
    a_cost(lease.store, "900.00")
    client = viewer()
    total = get(client, "/reports/pnl/", THIS_MONTH).context["total"]
    html = get(client, INDEX).content.decode()
    assert total.margin_billed is not None and total.margin_billed < 0
    assert value_and_sub(html, "pnl") == (money(total.margin_billed), "October margin")
    assert 'class="report-value tone-unpaid"' in card(html, "pnl")


def test_the_calendar_figure_is_the_calendar_pages_next_seven_days() -> None:
    build(2)
    CalendarEntry.objects.create(
        kind=CalendarEntryKind.REMINDER, on=date(2026, 10, 8), title="Call the landlord"
    )
    CalendarEntry.objects.create(
        kind=CalendarEntryKind.NOTE, on=date(2026, 10, 20), title="Outside the window"
    )
    client = viewer()
    groups = get(client, "/calendar/").context["next_days"]
    figures = get(client, INDEX).context["live"]
    assert figures.calendar_week == sum(group.count for group in groups) == 3
    first = groups[0].items[0]
    assert (first.title, first.on) == ("Call the landlord", date(2026, 10, 8))
    assert figures.calendar_next == "Oct 8 · Call the landlord"
    html = get(client, INDEX).content.decode()
    assert value_and_sub(html, "calendar") == (
        "3 this week",
        "next: Oct 8 · Call the landlord",
    )


def test_a_charge_of_an_ended_lease_counts_exactly_where_the_aging_report_counts_it() -> None:
    """Adversarial: the figure follows the report, nothing else. Store A's ended lease (another
    operator than its current one) still owes September; its waived October does not count."""
    store = a_store("Ended A", 901)
    ended = a_lease(store, an_operator("Former Co"), status=CollectionStatus.ENDED)
    ended.ended_on = date(2026, 9, 30)
    ended.save(update_fields=["ended_on"])
    a_rent_charge(ended, SEP, RentChargeStatus.UNPAID, "700.00")
    a_rent_charge(ended, OCT, RentChargeStatus.WAIVED, "700.00")
    a_lease(store, an_operator("Current Co"), started=date(2026, 10, 1))
    other = a_lease(a_store("Other B", 902), an_operator("Other Co"))
    a_rent_charge(other, SEP, RentChargeStatus.UNPAID, "300.00")
    client = viewer()
    report = get(client, "/reports/aging/").context["report"]
    figures = get(client, INDEX).context["live"]
    assert {row.store_id: row.total for row in report.rows} == {
        store.pk: D("700.00"),
        other.store_id: D("300.00"),
    }
    assert (figures.aging_total, figures.aging_stores) == (D("1000.00"), 2)
    assert figures.aging_total == report.totals.total


def test_a_one_off_charge_without_a_lease_counts_in_the_total_but_not_as_a_store() -> None:
    a_one_off_charge(an_operator("Loose Co"), "80.00", date(2026, 9, 1))
    figures = get(viewer(), INDEX).context["live"]
    report = get(viewer(), "/reports/aging/").context["report"]
    assert [row.store_id for row in report.rows] == [None]
    assert (figures.aging_total, figures.aging_stores) == (D("80.00"), 0)


# --- Zero states ------------------------------------------------------------------------------


def test_with_no_data_every_card_shows_its_zero_state() -> None:
    html = get(viewer(), INDEX).content.decode()
    assert value_and_sub(html, "aging") == ("$0.00", "unpaid · 0 stores")
    assert "tone-unpaid" not in card(html, "aging")
    assert value_and_sub(html, "payments-by-method") == ("$0.00", "collected in October")
    assert value_and_sub(html, "deposits") == ("0 unmatched", "nothing to review")
    assert value_and_sub(html, "revenue") == ("$0.00", "billed in October")
    assert value_and_sub(html, "pnl") == ("$0.00", "October margin")
    assert "tone-" not in card(html, "pnl")
    assert value_and_sub(html, "calendar") == ("0 this week", "nothing scheduled")


def test_one_store_and_one_item_are_singular() -> None:
    build(1)
    CalendarEntry.objects.all().delete()
    html = get(viewer(), INDEX).content.decode()
    assert value_and_sub(html, "aging")[1] == "unpaid · 1 store"
    assert value_and_sub(html, "deposits")[0] == "1 unmatched"
    assert value_and_sub(html, "calendar")[0] == "1 this week"


# --- The page ---------------------------------------------------------------------------------


class Links(HTMLParser):
    """Each ``<a>``'s id and text, and every interactive element found inside a link."""

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[str, str] = {}
        self.nested: list[str] = []
        self._open: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._open is not None and tag in ("a", "form", "input", "button", "select", "label"):
            self.nested.append(f"{tag} in {self._open}")
        if tag == "a":
            self._open = dict(attrs).get("id") or "?"
            self.links[self._open] = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._open = None

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self.links[self._open] += data


def content_of(html: str) -> str:
    start = html.index('id="reports-collections"')
    return html[start : html.index("</main>", start)]


def test_three_groups_each_card_one_link_with_open_and_its_id() -> None:
    html = get(viewer(), INDEX).content.decode()
    assert re.findall(r'<section class="report-group"[^>]*>\s*<h2[^>]*>([^<]+)</h2>', html) == [
        "Collections",
        "Finance",
        "Operators &amp; schedule",
    ]
    parser = Links()
    parser.feed(content_of(html))
    assert parser.nested == []
    expected = {f"report-{slug}" for slug in (*CARDS, "statement")}
    assert set(parser.links) == expected
    # The statement card's link holds no "Open ›" (the reference shows the search instead).
    for name, text in parser.links.items():
        assert text.count("Open ›") == (0 if name == "report-statement" else 1), name
    hrefs = dict(re.findall(r'id="(report-[a-z-]+)" href="([^"]+)"', html))
    assert hrefs == {
        "report-aging": "/reports/aging/",
        "report-payments-by-method": "/reports/payments-by-method/",
        "report-deposits": "/reports/deposits/",
        "report-revenue": "/reports/revenue/",
        "report-pnl": "/reports/pnl/",
        "report-statement": "/operators/",
        "report-calendar": "/calendar/",
    }
    # Every card holds its icon.
    assert content_of(html).count('<span class="report-icon"><svg') == 7


def test_the_header_line() -> None:
    html = get(viewer(), INDEX).content.decode()
    assert "<h1>Reports</h1>" in html
    assert '<span class="page-date" id="reports-lede">Every report downloads as a spreadsheet.' in (
        html
    )


def test_the_statement_card_searches_the_operator_list() -> None:
    an_operator("Findable Fuels")
    an_operator("Other Oils")
    client = viewer()
    html = get(client, INDEX).content.decode()
    form = re.search(r'<form class="report-statement-form"[^>]*>(.*?)</form>', html, re.S)
    assert form is not None
    assert 'method="get" action="/operators/"' in form.group(0)
    assert '<label class="visually-hidden" for="report-statement-q">Operator</label>' in (
        form.group(1)
    )
    assert 'name="q" id="report-statement-q"' in form.group(1)
    assert 'placeholder="Find an operator…"' in form.group(1)
    # One field: Enter submits it (implicit submission), no button.
    assert "<button" not in form.group(1)
    found = get(client, "/operators/", {"q": "Findable"}).content.decode()
    assert "Findable Fuels" in found and "Other Oils" not in found


def test_a_viewer_opens_it_and_cannot_post() -> None:
    client = viewer()
    assert client.get(INDEX).status_code == 200
    assert client.post(INDEX).status_code == 403


# --- Queries and the statement timeout ---------------------------------------------------------


def queries(client: Client) -> int:
    with CaptureQueriesContext(connection) as captured:
        get(client, INDEX)
    return len(captured.captured_queries)


def test_the_query_count_does_not_grow_with_the_data() -> None:
    build(2)
    client = viewer()
    get(client, INDEX)  # warm the session
    small = queries(client)
    build(4, start=2)
    for operator in Operator.objects.all()[:3]:
        a_one_off_charge(operator, "10.00", date(2026, 10, 1))
    assert queries(client) == small


def test_the_figures_are_computed_inside_the_reports_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"inside": False, "entered": 0, "computed_inside": False}
    real_bounded = report_views.bounded
    real_figures = report_index.figures

    def spy_bounded(compute: Any) -> Any:
        state["entered"] += 1
        state["inside"] = True
        try:
            return real_bounded(compute)
        finally:
            state["inside"] = False

    def spy_figures(today: date) -> report_index.ReportFigures:
        state["computed_inside"] = bool(state["inside"])
        return real_figures(today)

    monkeypatch.setattr(report_views, "bounded", spy_bounded)
    monkeypatch.setattr(report_index, "figures", spy_figures)
    get(viewer(), INDEX)
    assert state["entered"] == 1 and state["computed_inside"] is True


def test_a_cancelled_index_shows_the_cards_without_figures_and_the_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def too_slow(compute: Any) -> Any:
        raise report_views.ReportTooSlow

    monkeypatch.setattr(report_views, "bounded", too_slow)
    build(1)
    html = get(viewer(), INDEX).content.decode()
    assert report_views.TOO_SLOW in html
    assert 'class="report-value' not in html
    for slug in CARDS:
        assert "Open ›" in card(html, slug)
