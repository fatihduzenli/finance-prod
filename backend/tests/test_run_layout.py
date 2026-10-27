"""PLAN-012f-b Batch E, slice E-2: Admin › Daily charges to the spec's frame (DESIGN-SPEC §4
"Daily charges", daily-charges-laptop.png).

The header (the "Admin ›" crumb, the title, the long date muted beside it, the two buttons);
the result strip of the latest run, green, yellow when that run skipped a lease, red when the
latest "Update from …" job failed; the four figures; the "Due today" card with its read-only
filter (``?due=``), its columns and status badges and one "Open ›" per row; "Skipped today"
and "Recent runs"; the phone's two-line cards. No per-row Confirm and no row actions.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import Client, override_settings

from accounts.roles import Role
from ledger.models import AuditLog, Lease, Operator, PaymentChannel, RentCharge, Store
from ledger.services import rent_run
from ledger.views import run as run_view
from payments import jobs
from payments.models import Job, JobKind, JobStatus
from tests.test_operator_views import client_for
from tests.test_rent_run import lease

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)
RUN = "/run/"
CREATE = "/run/create-charges/"


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


@pytest.fixture
def billing() -> Client:
    return client_for(Role.BILLING, "billing-layout")[0]


def page(client: Client, query: str = "") -> str:
    response = client.get(RUN + query)
    assert response.status_code == 200
    return str(response.content.decode())


def between(html: str, start: str, end: str) -> str:
    assert start in html, start
    return html.split(start, 1)[1].split(end, 1)[0]


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def strip_of(html: str) -> str:
    return between(html, '<div class="run-strip run-strip', "</div>")


def due_rows(html: str) -> list[str]:
    body = between(html, '<table id="due-today"', "</table>").split("<tbody>", 1)[1]
    return [row for row in body.split("<tr>")[1:] if 'class="muted empty"' not in row]


def named_lease(label: str, number: int | None = None, **overrides: object) -> Lease:
    made = lease(**overrides)
    Store.objects.filter(pk=made.store_id).update(external_label=label, store_number=number)
    return made


def failed_match_job() -> Job:
    job = jobs.enqueue(JobKind.RUN_MATCH, jobs.RENT_RUN, {}, None)
    Job.objects.filter(pk=job.pk).update(status=JobStatus.FAILED)
    return job


# --- The header ---------------------------------------------------------------------------------


def test_the_header_has_the_admin_crumb_the_title_the_date_and_the_two_buttons(
    billing: Client,
) -> None:
    html = page(billing)
    header = between(html, '<header class="page-header-bar" id="page-header">', "</header>")
    assert '<nav class="breadcrumb" aria-label="Breadcrumb" id="breadcrumb"><span>Admin</span>' in (
        header
    )
    assert "<h1>Daily charges</h1>" in header
    assert '<span class="page-date" id="run-date">Thursday, Oct 15, 2026</span>' in header
    assert 'class="btn" type="submit" id="match-invoices"' in header
    assert ">Update from Stax</button>" in header
    assert 'class="btn btn-primary" type="submit" id="create-charges"' in header
    # The tab's title keeps the date.
    assert "Daily charges, Oct 15, 2026" in html


def test_a_viewer_sees_no_button_and_reading_with_a_filter_writes_nothing() -> None:
    lease()
    viewer, _ = client_for(Role.VIEWER)
    before = AuditLog.objects.count()
    for query in ("", "?due=debit", "?due=email", "?due=catchup", "?due=<b>"):
        html = page(viewer, query)
        assert 'id="create-charges"' not in html and 'id="match-invoices"' not in html
    assert AuditLog.objects.count() == before
    assert RentCharge.objects.count() == 0


# --- The result strip ---------------------------------------------------------------------------


def test_before_any_run_the_strip_is_grey_and_says_so(billing: Client) -> None:
    strip = strip_of(page(billing))
    assert strip.startswith('-none" id="run-strip" role="status">')
    assert "No run yet." in strip and "Next automatic run" in strip
    assert 'href="/reconciliation/">Nightly check →</a>' in strip


def test_a_clean_run_makes_the_strip_green_with_its_counts(billing: Client) -> None:
    lease()
    billing.post(CREATE)
    strip = strip_of(page(billing))
    assert strip.startswith('-ok" id="run-strip"')
    assert re.search(r"Run at \d\d:\d\d today\.</strong>", strip)
    assert "1 charge created (0 catch-up), 0 leases skipped, 0 errors." in text_of(strip)


def test_a_skipped_lease_makes_the_strip_yellow(billing: Client) -> None:
    lease()
    lease(collection_status="paused")
    billing.post(CREATE)
    strip = strip_of(page(billing))
    assert strip.startswith('-warn" id="run-strip"')
    assert "1 charge created (0 catch-up), 1 lease skipped, 0 errors." in text_of(strip)


def test_a_failed_update_makes_the_strip_red_whatever_the_run(billing: Client) -> None:
    failed_match_job()
    assert strip_of(page(billing)).startswith('-error" id="run-strip"')
    lease()
    billing.post(CREATE)
    strip = strip_of(page(billing))
    assert strip.startswith('-error" id="run-strip"')
    assert "1 charge created" in strip
    assert '<span id="strip-failure">"Update from Stax" failed: ' in strip.replace("&quot;", '"')


def test_a_run_of_an_earlier_day_names_its_day(billing: Client) -> None:
    lease()
    report = rent_run.create_charges(TODAY, actor=None)
    rent_run.record_run(report, date(2026, 10, 14), None)
    strip = strip_of(page(billing))
    assert re.search(r"Run at \d\d:\d\d on Oct 14, 2026\.</strong>", strip)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 15, 12, 59, tzinfo=UTC), ("today", "09:00")),
        (datetime(2026, 10, 15, 13, 0, tzinfo=UTC), ("tomorrow", "09:00")),
        (datetime(2026, 1, 15, 14, 0, tzinfo=UTC), ("tomorrow", "08:00")),
        # 01:00 UTC is still the evening before in New York: the next run is "tomorrow".
        (datetime(2026, 10, 16, 1, 0, tzinfo=UTC), ("tomorrow", "09:00")),
    ],
)
def test_the_next_automatic_run_follows_the_schedule_in_new_york(
    now: datetime, expected: tuple[str, str]
) -> None:
    assert run_view._next_run(now) == expected


# --- The figures, the card and its filter -------------------------------------------------------


def world(billing: Client) -> dict[str, Lease]:
    """Five leases due on the 15th: a bank debit, a wire, a large bank debit, a paused one,
    and a wire that started in September (its September debit is caught up); then a run."""
    leases = {
        "alpha": named_lease("Alpha", 1001),
        "bravo": named_lease("Bravo", 1002, payment_channel=PaymentChannel.WIRE),
        "charlie": named_lease("Charlie", 1003, rent=D("2500.00")),
        "delta": named_lease("Delta", 1004, collection_status="paused"),
        "echo": named_lease(
            "Echo", 1005, payment_channel=PaymentChannel.WIRE, started_on=date(2026, 9, 1)
        ),
    }
    assert billing.post(CREATE).status_code == 302
    return leases


def kpi(html: str, element_id: str) -> str:
    return text_of(between(html, f'id="{element_id}">', "</div>"))


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"))
def test_the_four_figures_come_from_todays_debits(billing: Client) -> None:
    world(billing)
    html = page(billing)
    assert kpi(html, "kpi-created") == "Created today 5 4 today · 1 catch-up"
    assert kpi(html, "kpi-due") == "Due today $7,000.00 2 bank debits · 2 e-mail"
    assert kpi(html, "kpi-skipped") == "Skipped 1 Collection is paused"
    assert 'class="kpi-value tone-pending">1<' in html
    assert kpi(html, "kpi-large") == "Large debits 1 above $2,000.00 · needs admin"
    assert 'class="kpi-value tone-unpaid">1<' in html


def test_with_nothing_due_the_figures_are_zero_and_not_coloured(billing: Client) -> None:
    html = page(billing)
    assert kpi(html, "kpi-created") == "Created today 0 0 today · 0 catch-up"
    assert kpi(html, "kpi-due").startswith("Due today $0.00 0 bank debits")
    assert "tone-pending" not in html and 'class="kpi-value tone-unpaid"' not in html
    assert "No debit is due today." in html


def row_of(rows: list[str], label: str) -> str:
    [found] = [row for row in rows if f">#{label}" in row or f">{label}<" in row]
    return found


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"))
def test_each_row_has_the_spec_columns_its_status_and_one_open_link(billing: Client) -> None:
    leases = world(billing)
    html = page(billing)
    head = between(html, '<table id="due-today"', "</thead>")
    assert [text_of(cell) for cell in re.findall(r"<th[^>]*>(.*?)</th>", head)] == [
        "Store",
        "Debit",
        "Collected by",
        "Amount",
        "Carried balance",
        "Status",
        "Open",
    ]
    rows = due_rows(html)
    assert len(rows) == 6
    alpha = row_of(rows, "1001 Alpha")
    assert '<a class="cell-title" href="/stores/' in alpha and "#1001 Alpha" in alpha
    assert leases["alpha"].operator is not None
    assert leases["alpha"].operator.display_name in alpha
    assert "Day 15" in alpha and "Bank debit" in alpha and "$1,500.00" in alpha
    assert (
        '<span class="badge badge-pending"><span class="badge-icon" aria-hidden="true">◐</span>'
        " Debit tonight</span>"
    ) in alpha
    bravo = row_of(rows, "1002 Bravo")
    assert "Wire" in bravo and '<span class="badge run-created">Created</span>' in bravo
    assert "Large debit · confirm" in row_of(rows, "1003 Charlie")
    assert "Skipped · Collection is paused." in row_of(rows, "1004 Delta")
    for row in rows:
        assert row.count("Open ›") == 1
        assert "<button" not in row and "<form" not in row
    charge = RentCharge.objects.get(lease=leases["alpha"], period_month=date(2026, 10, 1))
    assert f'href="/charges/{charge.pk}/">Open ›</a>' in alpha
    delta = row_of(rows, "1004 Delta")
    assert f'href="/stores/{leases["delta"].store_id}/">Open ›</a>' in delta


def test_before_the_run_a_row_is_not_created(billing: Client) -> None:
    named_lease("Alpha", 1001)
    [row] = due_rows(page(billing))
    assert "Not created" in row and "badge-neutral" in row


def test_a_carried_balance_is_red_and_none_is_zero(billing: Client) -> None:
    named_lease("Owed", 2001, started_on=date(2026, 9, 1))
    rent_run.create_charges(date(2026, 9, 15), actor=None)
    named_lease("Clear", 2002)
    rows = due_rows(page(billing))
    assert '<td class="money carried-balance tone-unpaid"' in row_of(rows, "2001 Owed")
    assert "$1,500.00" in between(row_of(rows, "2001 Owed"), "carried-balance", "</td>")
    clear = row_of(rows, "2002 Clear")
    assert '<td class="money carried-balance"' in clear and "$0.00" in clear


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", {"1001", "1002", "1003", "1004", "1005"}),
        ("?due=all", {"1001", "1002", "1003", "1004", "1005"}),
        ("?due=debit", {"1001", "1003", "1004"}),
        ("?due=email", {"1002", "1005"}),
        ("?due=catchup", {"1005"}),
        ("?due=nonsense", {"1001", "1002", "1003", "1004", "1005"}),
    ],
)
def test_the_filter_keeps_the_rows_of_its_kind(
    billing: Client, query: str, expected: set[str]
) -> None:
    world(billing)
    html = page(billing, query)
    assert numbers(due_rows(html)) == expected
    asked = query.removeprefix("?due=")
    chosen = asked if asked in dict(run_view.DUE_FILTERS) else "all"
    segments = between(html, '<nav class="segmented" aria-label="Show" id="due-filter">', "</nav>")
    assert segments.count('aria-current="page"') == 1
    assert f'id="due-filter-{chosen}" aria-current="page"' in segments
    assert '<a href="/run/" id="due-filter-all"' in segments and ">All 6</a>" in segments
    assert '<a href="/run/?due=catchup" id="due-filter-catchup"' in segments


def numbers(rows: list[str]) -> set[str]:
    return {number for row in rows for number in re.findall(r">#(\d{4}) ", row)}


def test_a_caught_up_charge_shows_its_date_and_only_after_todays_run(billing: Client) -> None:
    world(billing)
    [row] = due_rows(page(billing, "?due=catchup"))
    assert "Sep 15 · catch-up" in row and "$1,500.00" in row
    charge = RentCharge.objects.get(period_month=date(2026, 9, 1))
    assert f'href="/charges/{charge.pk}/">Open ›</a>' in row
    # A run of another day does not count as today's catch-up.
    rent_run.record_run(rent_run.create_charges(TODAY, actor=None), date(2026, 10, 14), None)
    html = page(billing, "?due=catchup")
    assert due_rows(html) == [] and "Nothing due today matches this filter." in html


# --- Skipped today and Recent runs --------------------------------------------------------------


def test_skipped_today_lists_each_store_and_its_reason_without_actions(billing: Client) -> None:
    named_lease("Delta", 1004, collection_status="paused")
    card = between(page(billing), 'id="skipped-today-card"', "</section>")
    assert '<ul class="run-list" id="skipped-today">' in card
    assert ">#1004 Delta</a> · Collection is paused.</li>" in card
    assert "<button" not in card and "Debit again" not in card and "Set up autopay" not in card


def test_skipped_today_says_when_nothing_was_skipped(billing: Client) -> None:
    card = between(page(billing), 'id="skipped-today-card"', "</section>")
    assert '<p class="muted empty" id="skipped-today">No lease due today was skipped.</p>' in card


def test_recent_runs_lists_the_latest_five_with_who_counts_and_ok(billing: Client) -> None:
    html = page(billing)
    assert '<p class="muted empty" id="recent-runs">No run yet.</p>' in html
    lease(started_on=date(2026, 9, 1), payment_channel=PaymentChannel.WIRE)
    billing.post(CREATE)
    for _ in range(5):
        rent_run.record_run(rent_run.create_charges(TODAY, actor=None), TODAY, None)
    card = between(page(billing), 'id="recent-runs-card"', "</section>")
    lines = re.findall(r"<li>(.*?)</li>", card, re.S)
    assert len(lines) == rent_run.RECENT_RUNS == 5
    assert all(re.match(r"Oct 15 · \d\d:\d\d · automatic", text_of(line)) for line in lines)
    assert text_of(lines[0]).endswith("0 created · 0 skipped ✓ OK")
    # The button's run, sixth newest, is no longer listed; one query shows it again.
    [first] = rent_run.recent_runs(6)[-1:]
    assert first.actor is not None and (first.created, first.caught_up) == (1, 1)


def test_a_run_by_a_person_names_them_with_its_catch_up(billing: Client) -> None:
    lease(started_on=date(2026, 9, 1), payment_channel=PaymentChannel.WIRE)
    billing.post(CREATE)
    card = between(page(billing), 'id="recent-runs-card"', "</section>")
    # PLAN-012f-c D11: the staff name (the username here), the e-mail as its title.
    assert '· by <span title="billing-layout@example.com">billing-layout</span></span>' in card
    assert "2 created (1 catch-up) · 0 skipped" in text_of(card)
    assert '<span class="badge badge-paid">' in card


# --- Escaping, the phone, the style sheet -------------------------------------------------------


def test_store_and_operator_names_are_escaped(billing: Client) -> None:
    made = named_lease("<script>x</script>", None, collection_status="paused")
    assert made.operator is not None
    Operator.objects.filter(pk=made.operator.pk).update(display_name="<b>Op</b>")
    named_lease("<i>due</i>", None)
    html = page(billing)
    assert "<script>x</script>" not in html and "<b>Op</b>" not in html and "<i>due</i>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html and "&lt;i&gt;due&lt;/i&gt;" in html


def test_the_phone_draws_the_due_rows_as_two_line_cards(billing: Client) -> None:
    named_lease("Alpha", 1001)
    html = page(billing)
    assert '<table id="due-today" class="role-cards">' in html
    [row] = due_rows(html)
    assert '<td data-role="title" data-label="Store">' in row
    assert 'data-role="amount" data-label="Amount"' in row
    assert '<td data-role="status" data-label="Status">' in row


def test_the_style_sheet_carries_the_strip_colours_and_the_phone_rule() -> None:
    css = (Path(__file__).resolve().parents[1] / "finance/static/finance/app.css").read_text()
    for rule in (
        ".run-strip-ok { background: var(--paid-bg); color: var(--paid-text);",
        ".run-strip-warn { background: var(--pending-bg); color: var(--pending-text);",
        ".run-strip-error { background: var(--unpaid-bg); color: var(--error-text);",
        ".kpi-value.tone-pending { color: var(--pending-text); }",
        ".badge.run-created { background: var(--month-current); color: var(--link); }",
    ):
        assert rule in css, rule
    # The phone rule lives in the stylesheet's one phone block (PLAN-012f-b E4-1).
    section = css.split("/* --- Daily charges (PLAN-012f-b E-2)", 1)[1].split("/* ---", 1)[0]
    assert "@media" not in section
    start = css.index("@media (max-width: 719.98px) {")
    phone = css[start : css.index("\n}\n", start)]
    assert ".run-pair { grid-template-columns: minmax(0, 1fr); }" in phone
