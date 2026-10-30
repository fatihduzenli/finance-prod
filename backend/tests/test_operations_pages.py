"""Operations pages (docs/qa/PLAN-012d-store-workspace.md §6 D, Batch D): the run page's list
of the charges it created, after the button and after the daily tick (D11); the
dashboard's one line on today's rent run (D11); the store page's "Create missing charges"
(D13); the grid's default window and month pickers, the swap and the cap, a future month
billed in advance (D12); and the Charges tab's rent and one-off rows when a returned
payment was rescheduled, which total the header (finding 12d-2). The adversarial cases are
the QA owner's (test_plan012d_d_adversarial.py)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionStatus,
    Lease,
    OneOffCategory,
    OneOffChargeStatus,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import grid, rent_run, workspace
from payments.models import Job, JobKind
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_rent_charge,
    a_store,
    an_invoice,
    an_operator,
)
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
AUG, SEP, OCT, NOV = (date(2026, month, 1) for month in (8, 9, 10, 11))


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    return TODAY


def wire_lease(started: date, *, label: str = "", debit_day: int = 5) -> Lease:
    return a_lease(
        a_store(label),
        an_operator(),
        started=started,
        channel=PaymentChannel.WIRE,
        debit_day=debit_day,
    )


def months(lease: Lease) -> list[date]:
    return list(
        RentCharge.objects.filter(lease=lease)
        .order_by("period_month")
        .values_list("period_month", flat=True)
    )


def body(client: Client, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200, (url, response.status_code)
    return response.content.decode()


def created_table(html: str) -> str:
    assert 'id="created-charges"' in html
    return html.split('id="created-charges"')[1].split("</table>")[0]


def tick() -> str:
    out = StringIO()
    call_command("collections_tick", stdout=out)
    Job.objects.filter(kind=JobKind.DISPUTES_SYNC).delete()
    return out.getvalue()


# --- D11: the run page's list and the tick's row --------------------------------------------------


def test_the_button_lists_what_it_created_with_links_and_writes_one_run_row() -> None:
    caught_up = wire_lease(date(2026, 9, 13), label="2477")
    due_today = wire_lease(date(2026, 9, 1), label="3100", debit_day=15)
    billing, user = client_for(Role.BILLING)
    response = billing.post(reverse("ledger:run:create_charges"), {}, follow=True)
    assert response.status_code == 200
    table = created_table(response.content.decode())
    for lease in (caught_up, due_today):
        assert reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}) in table
        for charge in RentCharge.objects.filter(lease=lease):
            assert reverse("ledger:charges:detail", kwargs={"pk": charge.pk}) in table
    today_charge = RentCharge.objects.get(lease=due_today, period_month=OCT)
    today_link = reverse("ledger:charges:detail", kwargs={"pk": today_charge.pk})
    [today_row] = [row for row in table.split("<tr>") if today_link in row]
    assert 'data-label="Created">today</td>' in today_row
    assert (
        table.count('data-label="Created">catch-up</td>') == 3
    )  # Sep and Oct of 2477, Sep of 3100
    [run] = AuditLog.objects.filter(action=rent_run.TICKED_ACTION)
    assert run.actor == user and run.entity_id == "2026-10-15"
    assert run.after is not None
    assert (run.after["created"], run.after["caught_up"], run.after["already"]) == (1, 3, 0)
    assert run.after["created_ids"] == [str(today_charge.pk)]
    assert set(run.after["caught_up_ids"]) == {
        str(pk)
        for pk in RentCharge.objects.exclude(pk=today_charge.pk).values_list("pk", flat=True)
    }
    # PLAN-012f-c D11: the staff name (no first or last name: the username), the e-mail as
    # its title.
    shown = f'<span title="{user.email}">{user.get_username()}</span>'
    assert f"by {shown}" in response.content.decode()


def test_the_daily_tick_writes_its_row_and_the_run_page_shows_its_list() -> None:
    lease = wire_lease(date(2026, 9, 13), label="2477")
    tick()
    [run] = AuditLog.objects.filter(action=rent_run.TICKED_ACTION)
    assert run.actor is None and run.after is not None
    assert run.after["caught_up"] == 2 and len(run.after["caught_up_ids"]) == 2
    html = body(client_for(Role.VIEWER)[0], reverse("ledger:run:today"))
    # PLAN-012f-b E-2: the daily tick is "automatic" in the Recent runs card.
    assert re.search(r"Oct 15 · \d\d:\d\d · automatic</span>", html)
    table = created_table(html)
    assert reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}) in table
    assert "Sep 2026" in table and "Oct 2026" in table
    # A second tick creates nothing: its row says so and the page lists nothing.
    tick()
    assert AuditLog.objects.filter(action=rent_run.TICKED_ACTION).count() == 2
    table = created_table(body(client_for(Role.VIEWER)[0], reverse("ledger:run:today")))
    assert "This run created no charge." in table
    assert months(lease) == [SEP, OCT]


def test_the_run_page_without_any_run_shows_no_list() -> None:
    html = body(client_for(Role.VIEWER)[0], reverse("ledger:run:today"))
    assert 'id="created-charges"' not in html


def test_a_run_row_with_unreadable_values_is_read_safely() -> None:
    AuditLog.objects.create(
        action=rent_run.TICKED_ACTION,
        entity_type=rent_run.ENTITY_RUN,
        entity_id="2026-10-15",
        after={"created": "x", "created_ids": ["not-a-uuid", 7], "caught_up_ids": "nope"},
    )
    record = rent_run.last_run()
    assert record is not None
    assert (record.created, record.caught_up, record.charges) == (0, 0, ())
    assert rent_run.run_line(TODAY) == rent_run.RunLine(created=0, skipped=0)


# --- D11: the dashboard's line ------------------------------------------------------------------


def test_the_dashboard_line_sums_todays_runs_and_links_the_run_page() -> None:
    viewer, _ = client_for(Role.VIEWER)
    html = body(viewer, "/")
    assert 'id="run-line"' in html and "Daily charges today: not created yet<" in html
    line = html.split('id="run-line"')[1].split("</p>")[0]
    assert f'href="{reverse("ledger:run:today")}"' in line
    paused = wire_lease(date(2026, 9, 1), label="9000", debit_day=15)
    Lease.objects.filter(pk=paused.pk).update(collection_status=CollectionStatus.PAUSED)
    wire_lease(date(2026, 9, 13), label="2477")
    # Yesterday's run is not today's.
    rent_run.record_run(rent_run.ChargeRunReport((), (), ()), TODAY - timedelta(days=1), None)
    tick()  # creates 2 (catch-up), skips the paused lease due today
    billing, _ = client_for(Role.BILLING)
    billing.post(reverse("ledger:run:create_charges"), {})  # creates nothing more
    html = body(viewer, "/")
    assert "Daily charges today: 2 created, 1 skipped" in html


# --- D13: Create missing charges ----------------------------------------------------------------


def test_create_missing_charges_names_what_it_created_and_then_says_nothing_missing() -> None:
    lease = wire_lease(date(2026, 8, 13), label="2477", debit_day=15)
    ended = a_lease(
        lease.store,
        an_operator(),
        started=date(2025, 12, 1),
        channel=PaymentChannel.WIRE,
        debit_day=15,
        rent="1000.00",
    )
    Lease.objects.filter(pk=ended.pk).update(ended_on=date(2026, 7, 31))
    elsewhere = wire_lease(date(2026, 8, 13), label="3100", debit_day=15)
    url = reverse("ledger:stores:create_charges", kwargs={"pk": lease.store_id})
    billing, user = client_for(Role.BILLING)
    response = billing.post(url, {})
    assert response.status_code == 302
    assert response["Location"] == workspace.tab_url(lease.store_id, workspace.TAB_CHARGES)
    html = body(billing, response["Location"])
    assert "Created: " in html
    # August prorated from the 13th (19 of 31 days of $3,500); October due today.
    assert "Rent Aug 2026 $2,145.16 (catch-up)" in html
    assert "Rent Sep 2026 $3,500.00 (catch-up)" in html
    assert "Rent Oct 2026 $3,500.00 (today)" in html
    assert "Rent Dec 2025 $1,000.00 (catch-up)" in html and "Rent Jul 2026 $1,000.00" in html
    assert months(lease) == [AUG, SEP, OCT]
    assert len(months(ended)) == 8  # December 2025 to July 2026
    assert months(elsewhere) == []
    audited = AuditLog.objects.filter(action="rent_charge.created")
    assert audited.count() == 11 and {row.actor_id for row in audited} == {user.pk}
    today_row = audited.get(entity_id=str(RentCharge.objects.get(lease=lease, period_month=OCT).pk))
    assert today_row.after is not None and "catch_up" not in today_row.after
    assert all(
        row.after is not None and row.after["catch_up"] is True
        for row in audited.exclude(pk=today_row.pk)
    )
    # Not a run of the whole rent roll: no run row, the run page and the dashboard keep theirs.
    assert not AuditLog.objects.filter(action=rent_run.TICKED_ACTION).exists()
    again = billing.post(url, {}, follow=True).content.decode()
    assert "Nothing missing" in again and "Created: " not in again
    assert AuditLog.objects.filter(action="rent_charge.created").count() == 11


def test_create_missing_charges_names_a_paused_lease_and_is_on_the_charges_tab() -> None:
    # PLAN-012e-2 D5: a text link (a POST button styled .link-action) on the Charges tab.
    lease = wire_lease(date(2026, 8, 13), label="2477")
    store_page = reverse("ledger:stores:detail", kwargs={"pk": lease.store_id})
    url = reverse("ledger:stores:create_charges", kwargs={"pk": lease.store_id})
    billing, _ = client_for(Role.BILLING)
    html = body(billing, store_page)
    tab = html.split('id="tab-charges"')[1].split("</section>")[0]
    form = tab[tab.index(f'<form method="post" action="{url}"') :].split("</form>")[0]
    assert 'id="create-missing-charges">Create missing charges</button>' in form
    assert 'class="link-action"' in form and "csrfmiddlewaretoken" in form
    assert url not in html.split('id="action-bar"')[1].split("</nav>")[0]
    assert "create-missing-charges" not in body(client_for(Role.VIEWER)[0], store_page)
    Lease.objects.filter(pk=lease.pk).update(collection_status=CollectionStatus.PAUSED)
    html = billing.post(url, {}, follow=True).content.decode()
    assert "Nothing missing" in html and "Skipped 2477: Collection is paused." in html
    assert months(lease) == []


def test_create_missing_charges_on_an_unknown_store_is_404() -> None:
    billing, _ = client_for(Role.BILLING)
    for pk in ("00000000-0000-0000-0000-000000000000", "not-a-uuid"):
        response = billing.post(reverse("ledger:stores:create_charges", kwargs={"pk": pk}), {})
        assert response.status_code == 404


# --- D12: the grid window and pickers ----------------------------------------------------------


def month_columns(html: str) -> list[str]:
    head = html.split('<table class="grid" id="grid">')[1].split("</thead>")[0]
    # PLAN-012f D6: the current month's header also carries month-current.
    return re.findall(r'class="month-col[^"]*"[^>]*>([^<]*)<', head)


def picker_options(html: str, name: str) -> list[tuple[str, str, bool]]:
    """The (value, label, selected) of the grid's From or To select."""
    select = html.split(f'<select name="{name}" id="{name}">')[1].split("</select>")[0]
    options = []
    for part in select.split('<option value="')[1:]:
        value, rest = part.split('"', 1)
        label = rest.split(">", 1)[1].split("</option>")[0]
        options.append((value, label, rest.startswith(" selected")))
    return options


def test_the_grid_opens_on_ten_past_months_this_month_and_the_next() -> None:
    viewer, _ = client_for(Role.VIEWER)
    html = body(viewer, "/grid/")
    columns = month_columns(html)
    assert columns[0] == "Dec 2025" and columns[-1] == "Nov 2026" and len(columns) == 12
    # The pickers are plain selects (no HTML month input, which Safari does not render)
    # offering the current month +/- 24 months, labelled by name, the range selected.
    assert 'type="month"' not in html
    for name, chosen in (("from", "2025-12"), ("to", "2026-11")):
        options = picker_options(html, name)
        assert len(options) == 49
        assert options[0][:2] == ("2024-10", "Oct 2024")
        assert options[24][:2] == ("2026-10", "Oct 2026")
        assert options[-1][:2] == ("2028-10", "Oct 2028")
        assert [value for value, _, selected in options if selected] == [chosen]
    # PLAN-014 A5: the shared filters form's Apply submits the range and the filters.
    assert '<button class="btn btn-dark" type="submit" id="filters-apply">Apply</button>' in html
    assert 'id="grid-messages"' not in html


def test_the_grid_swaps_a_reversed_range_caps_a_wide_one_and_ignores_hostile_values() -> None:
    viewer, _ = client_for(Role.VIEWER)
    swapped = body(viewer, "/grid/?from=2026-11&to=2026-06")
    months = ("Jun", "Jul", "Aug", "Sep", "Oct", "Nov")
    assert month_columns(swapped) == [f"{name} 2026" for name in months]
    assert "The end month was before the start month" in swapped
    wide = body(viewer, "/grid/?from=2020-01&to=2026-12")
    columns = month_columns(wide)
    assert (columns[0], columns[-1], len(columns)) == ("Jan 2025", "Dec 2026", grid.MAX_MONTHS)
    assert "At most 24 months are shown" in wide
    # A range outside the pickers' months stays submittable: its months are added.
    old = body(viewer, "/grid/?from=2022-01&to=2022-03")
    for name, chosen in (("from", "2022-01"), ("to", "2022-03")):
        options = picker_options(old, name)
        assert len(options) == 52
        assert [value for value, _, _ in options[:4]] == [
            "2022-01",
            "2022-02",
            "2022-03",
            "2024-10",
        ]
        assert options[0][1] == "Jan 2022"
        assert [value for value, _, selected in options if selected] == [chosen]
    # A range partly inside the window adds only the months outside it.
    edge = body(viewer, "/grid/?from=2024-08&to=2024-12")
    assert [value for value, _, _ in picker_options(edge, "from")[:3]] == [
        "2024-08",
        "2024-09",
        "2024-10",
    ]
    assert len(picker_options(edge, "to")) == 51
    hostile = body(viewer, "/grid/?from=2026-13&to=%3Cscript%3Ealert(1)%3C/script%3E")
    assert "alert(1)" not in hostile and "&lt;script" not in hostile and "2026-13" not in hostile
    assert month_columns(hostile)[0] == "Dec 2025"
    assert "The months could not be read" in hostile


def test_a_future_month_billed_in_advance_shows_it_with_the_invoice_number() -> None:
    lease = wire_lease(date(2026, 9, 1), label="2477")
    assert lease.operator is not None
    invoice = an_invoice(lease.operator, 42, "3500.00")
    advance = a_rent_charge(lease, NOV, RentChargeStatus.PAID, "3500.00", "3500.00")
    RentCharge.objects.filter(pk=advance.pk).update(billed_by_invoice=invoice)
    viewer, _ = client_for(Role.VIEWER)
    html = body(viewer, "/grid/")
    november = html.split('data-month="2026-11"')[1].split("</td>")[0]
    # PLAN-012f D6: the cell block, the amount over "Paid" and the invoice number.
    assert '<span class="cell-amount">$3,500.00</span>' in november
    assert "</span> Paid · INV-2026-0042</span>" in november
    assert reverse("ledger:charges:detail", kwargs={"pk": advance.pk}) in november
    built = grid.build(grid.parse_query({}, TODAY), TODAY)
    [row] = [row for row in built.rows if row.store.pk == lease.store_id]
    states = {cell.month: cell.status for cell in row.cells}
    # PLAN-012e-4 D3: before the lease started, blank (outside the lease); a past or current
    # month inside it with no charge, blank; the future month billed in advance shows it.
    assert states[date(2026, 8, 1)] == ""
    assert next(cell for cell in row.cells if cell.month == date(2026, 8, 1)).label == "Aug 2026"
    assert states[OCT] == "" and states[NOV] == grid.STATUS_PAID


# --- Finding 12d-2: the Charges tab after a rescheduled return ---------------------------------


def a_returned(amount: str, **charge: object) -> Payment:
    kind = PaymentKind.RENT if "rent_charge" in charge else PaymentKind.ONE_OFF
    return Payment.objects.create(
        kind=kind,
        method=PaymentMethod.ACH,
        amount=D(amount),
        status=PaymentStatus.RETURNED,
        returned_at=NOW,
        **charge,
    )


def a_redebit(lease: Lease, returned: Payment, price: str, paid: str = "0.00") -> None:
    assert lease.operator is not None
    redebit = a_one_off_charge(
        lease.operator,
        price,
        date(2026, 10, 20),
        lease=lease,
        paid=paid,
        status=OneOffChargeStatus.PARTIAL if D(paid) > 0 else OneOffChargeStatus.OWED,
        category=OneOffCategory.REDEBIT,
        source_payment=returned,
    )
    a_one_off_charge(
        lease.operator,
        "25.00",
        date(2026, 10, 20),
        lease=lease,
        category=OneOffCategory.NSF_FEE,
        companion_of=redebit,
    )


def tab_rows(lease: Lease, chosen: str) -> tuple[dict[object, workspace.ChargeRow], Decimal]:
    data = workspace.charges_tab(lease.store, chosen, Role.ADMIN)
    return {row.charge_id: row for row in data["charge_rows"]}, data["charge_total"]


def test_a_rescheduled_rent_return_is_carried_on_the_tab_as_in_the_header() -> None:
    lease = wire_lease(date(2026, 9, 1), label="2477")
    # The re-debit collected 400 of 1,000 again; that counts as paid on the rent charge.
    rent = a_rent_charge(lease, SEP, RentChargeStatus.PARTIAL, "1000.00", "400.00")
    a_redebit(lease, a_returned("1000.00", rent_charge=rent), "1000.00", "400.00")
    header = workspace.store_workspace(lease.store, TODAY, Role.ADMIN).header
    assert header.open_balance == D("625.00")  # 600 left on the re-debit and its 25 fee
    opened, total = tab_rows(lease, workspace.FILTER_OPEN)
    assert rent.pk not in opened and total == header.open_balance
    every, total = tab_rows(lease, workspace.FILTER_ALL)
    row = every[rent.pk]
    assert (row.balance, row.status) == (D("0.00"), workspace.CARRIED_BY_REDEBIT)
    assert total == header.open_balance
    html = body(client_for(Role.VIEWER)[0], workspace.tab_url(lease.store_id, "charges"))
    assert 'id="charges-balance">$625.00<' in html


def test_a_partly_carried_rent_row_owes_the_rest_and_keeps_its_status() -> None:
    lease = wire_lease(date(2026, 9, 1), label="2477")
    rent = a_rent_charge(lease, SEP, RentChargeStatus.UNPAID, "1000.00")
    a_redebit(lease, a_returned("600.00", rent_charge=rent), "600.00")
    header = workspace.store_workspace(lease.store, TODAY, Role.ADMIN).header
    assert header.open_balance == D("1025.00")
    opened, total = tab_rows(lease, workspace.FILTER_OPEN)
    assert (opened[rent.pk].balance, opened[rent.pk].status) == (D("400.00"), "Unpaid")
    assert total == header.open_balance


def test_a_rescheduled_one_off_return_is_carried_on_the_tab_as_in_the_header() -> None:
    lease = wire_lease(date(2026, 9, 1), label="2477")
    assert lease.operator is not None
    charge = a_one_off_charge(lease.operator, "500.00", date(2026, 9, 20), lease=lease)
    a_redebit(lease, a_returned("500.00", one_off_charge=charge), "500.00")
    header = workspace.store_workspace(lease.store, TODAY, Role.ADMIN).header
    assert header.open_balance == D("525.00")
    opened, total = tab_rows(lease, workspace.FILTER_OPEN)
    assert charge.pk not in opened and total == header.open_balance
    every, total = tab_rows(lease, workspace.FILTER_ALL)
    assert (every[charge.pk].balance, every[charge.pk].status) == (
        D("0.00"),
        workspace.CARRIED_BY_REDEBIT,
    )
    assert total == header.open_balance
    # The re-debit and its fee are owed, not carried.
    assert all(
        row.status != workspace.CARRIED_BY_REDEBIT for pk, row in every.items() if pk != charge.pk
    )
