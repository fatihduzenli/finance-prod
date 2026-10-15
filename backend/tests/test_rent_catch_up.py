"""The rent run's catch-up and the first charge on save
(docs/qa/PLAN-012b-b-catch-up-rent-lines-wizard-fixes.md D1 to D3; §6 "A", the
implementer's cases): a lease saved days or a month after its start gets its charges at
once, the first month prorated and each due on its debit date (or the start date); a
lease that starts later gets nothing until the run on or after its start; a twice-monthly
lease gets both debits; nothing after the end month; the 24-month bound; the run's report
and page; idempotent across runs. The adversarial cases are the QA owner's.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from accounts.roles import Role
from ledger.models import AuditLog, CollectionStatus, Lease, PaymentChannel, RentCharge
from ledger.services import leases, rent_run
from tests.test_lease_services import lease_fields, make_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 16)
TEXT_13 = "13–31 Oct, 19 of 31 days of $2,000.00 = $1,225.81"


def saved(today: date = TODAY, rent: str = "2000.00", **overrides: Any) -> Lease:
    """A lease saved through the service on ``today``."""
    values: dict[str, Any] = {"debit_day": 1, "started_on": date(2026, 10, 13)}
    values.update(overrides)
    if "operator" not in values:
        values["operator"] = make_operator(email=None, customer=None)
    lease, created = leases.create_lease(
        lease_fields(**values), rent=D(rent), creation_key=uuid.uuid4(), actor=None, today=today
    )
    assert created
    return lease


def rows(lease: Lease) -> list[tuple[date, int, Decimal, date, str | None]]:
    return [
        (row.period_month, row.seq, row.amount_due, row.due_on, row.proration_text)
        for row in RentCharge.objects.filter(lease=lease).order_by("period_month", "seq")
    ]


# --- D2: the first charge on save -----------------------------------------------------------


def test_a_lease_saved_three_days_after_its_start_has_its_prorated_charge_at_once() -> None:
    lease = saved()
    assert rows(lease) == [(date(2026, 10, 1), 1, D("1225.81"), date(2026, 10, 13), TEXT_13)]
    created = AuditLog.objects.get(action="rent_charge.created")
    assert created.after is not None and created.after["catch_up"] is True
    # The run on a later day creates nothing more for it.
    report = rent_run.create_charges(TODAY, actor=None)
    assert report.caught_up == () and report.created == ()
    assert len(rows(lease)) == 1


def test_a_lease_saved_a_month_late_has_two_charges_each_due_on_its_debit_date() -> None:
    lease = saved(today=date(2026, 11, 20), debit_day=15)
    assert rows(lease) == [
        (date(2026, 10, 1), 1, D("1225.81"), date(2026, 10, 15), TEXT_13),
        (date(2026, 11, 1), 1, D("2000.00"), date(2026, 11, 15), None),
    ]


def test_a_lease_that_starts_later_gets_nothing_until_the_run_on_its_start() -> None:
    lease = saved(started_on=date(2026, 11, 13))
    assert rows(lease) == []
    assert rent_run.create_charges(date(2026, 11, 12), actor=None).caught_up == ()
    assert rows(lease) == []
    report = rent_run.create_charges(date(2026, 11, 13), actor=None)
    assert [charge.lease_id for charge in report.caught_up] == [lease.pk]
    assert rows(lease) == [
        (
            date(2026, 11, 1),
            1,
            D("1200.00"),
            date(2026, 11, 13),
            "13–30 Nov, 18 of 30 days of $2,000.00 = $1,200.00",
        )
    ]


def test_saving_an_edit_creates_the_missing_charges_in_its_transaction() -> None:
    lease = built(date(2026, 10, 13))
    assert rows(lease) == []
    fields = {name: getattr(lease, name) for name in leases.LEASE_FIELDS}
    fields["debit_day"] = 15
    leases.update_lease(
        lease.pk, fields, rent=D("2000.00"), rent_effective_from=None, actor=None, today=TODAY
    )
    assert rows(lease) == [(date(2026, 10, 1), 1, D("1225.81"), date(2026, 10, 15), TEXT_13)]


def test_a_refused_edit_creates_nothing() -> None:
    lease = built(date(2026, 10, 13))
    fields = {name: getattr(lease, name) for name in leases.LEASE_FIELDS}
    with pytest.raises(leases.LeaseRefused):
        leases.update_lease(
            lease.pk,
            fields,
            rent=D("2100.00"),
            rent_effective_from=date(2026, 10, 13),
            actor=None,
            today=TODAY,
        )
    assert rows(lease) == []


@pytest.mark.parametrize(
    ("status", "channel"),
    [
        (CollectionStatus.PROSPECTIVE, PaymentChannel.WIRE),
        (CollectionStatus.PAUSED, PaymentChannel.WIRE),
        (CollectionStatus.PROSPECTIVE, PaymentChannel.UNKNOWN),
    ],
)
def test_a_lease_not_active_gets_no_charge_on_save(status: str, channel: str) -> None:
    lease = saved(collection_status=status, payment_channel=channel)
    assert rows(lease) == []


# --- D1: the run's catch-up --------------------------------------------------------------------


def built(started: date, **fields: Any) -> Lease:
    """A lease written directly (no service, so nothing on save)."""
    values: dict[str, Any] = {
        "store": lease_fields()["store"],
        "operator": make_operator(email=None, customer=None),
        "debit_day": 1,
        "payment_channel": PaymentChannel.WIRE,
        "collection_status": CollectionStatus.ACTIVE,
        "started_on": started,
    }
    values.update(fields)
    lease = Lease.objects.create(**values)
    lease.rent_history.create(amount=D("2000.00"), effective_from=started, source="staff")
    return lease


def test_a_twice_monthly_lease_gets_both_debits_and_waits_for_those_not_yet_due() -> None:
    lease = built(date(2026, 9, 10), debit_day=5, second_debit_day=20)
    report = rent_run.create_charges(TODAY, actor=None)
    assert len(report.caught_up) == 3
    text = "10–30 Sep, 21 of 30 days of $2,000.00 = $1,400.00"
    # Debit 1 of September (the 5th) is before the start: due on the start date. Debit 2
    # of October (the 20th) is after the day of the run: it waits.
    assert rows(lease) == [
        (date(2026, 9, 1), 1, D("700.00"), date(2026, 9, 10), text),
        (date(2026, 9, 1), 2, D("700.00"), date(2026, 9, 20), text),
        (date(2026, 10, 1), 1, D("1000.00"), date(2026, 10, 5), None),
    ]


def test_nothing_is_caught_up_after_the_end_month() -> None:
    ended = built(date(2026, 7, 1))
    Lease.objects.filter(pk=ended.pk).update(ended_on=date(2026, 8, 31))
    rent_run.create_charges(TODAY, actor=None)
    assert [m for m, *_ in rows(ended)] == [date(2026, 7, 1), date(2026, 8, 1)]


def test_a_month_with_no_rent_in_effect_or_already_charged_is_left_alone() -> None:
    lease = built(date(2026, 8, 1))
    lease.rent_history.update(effective_to=date(2026, 8, 31))
    lease.rent_history.create(amount=D("2100.00"), effective_from=date(2026, 10, 1), source="staff")
    RentCharge.objects.create(
        lease=lease,
        period_month=date(2026, 8, 1),
        seq=1,
        due_on=date(2026, 8, 1),
        amount_due=D("1.00"),
        status="paid",
        amount_paid=D("1.00"),
    )
    rent_run.create_charges(TODAY, actor=None)
    # August kept as it was, September has no rent, October at the new rent.
    assert [(m, a) for m, _, a, *_ in rows(lease)] == [
        (date(2026, 8, 1), D("1.00")),
        (date(2026, 10, 1), D("2100.00")),
    ]


def test_the_catch_up_looks_back_at_most_24_months() -> None:
    lease = built(date(2023, 1, 1))
    report = rent_run.create_charges(TODAY, actor=None)
    months = [m for m, *_ in rows(lease)]
    assert len(report.caught_up) == rent_run.CATCH_UP_MONTHS == 24
    assert months[0] == date(2024, 11, 1) and months[-1] == date(2026, 10, 1)


def test_the_run_is_idempotent_and_audits_the_catch_up() -> None:
    lease = built(date(2026, 8, 13))
    first = rent_run.create_charges(TODAY, actor=None)
    second = rent_run.create_charges(TODAY, actor=None)
    assert len(first.caught_up) == 3 and second.caught_up == ()
    assert len(rows(lease)) == 3
    audits = AuditLog.objects.filter(action="rent_charge.created")
    assert audits.count() == 3
    assert all(row.after is not None and row.after["catch_up"] is True for row in audits)


def test_todays_debit_is_created_not_caught_up() -> None:
    lease = built(date(2026, 9, 1), debit_day=16)
    report = rent_run.create_charges(TODAY, actor=None)
    assert [c.period_month for c in report.created] == [date(2026, 10, 1)]
    assert [c.period_month for c in report.caught_up] == [date(2026, 9, 1)]
    assert len(rows(lease)) == 2


def test_create_month_is_the_single_month_helper() -> None:
    lease = built(date(2026, 10, 13))
    charge, made = rent_run.create_month(lease, date(2026, 10, 1), None)
    assert made and (charge.amount_due, charge.due_on, charge.proration_text) == (
        D("1225.81"),
        date(2026, 10, 13),
        TEXT_13,
    )
    again, made_again = rent_run.create_month(lease, date(2026, 10, 1), None)
    assert again.pk == charge.pk and not made_again
    with pytest.raises(ValueError, match=rent_run.SKIP_NO_RENT):
        rent_run.create_month(lease, date(2026, 9, 1), None)
    with pytest.raises(ValueError, match=rent_run.NO_SUCH_DEBIT):
        rent_run.create_month(lease, date(2026, 11, 1), None, seq=2)


# --- The run page ------------------------------------------------------------------------------


def test_the_run_page_reports_the_catch_up_and_states_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    built(date(2026, 9, 13))
    client, _ = client_for(Role.BILLING)
    page = client.get("/run/").content.decode()
    assert "at most 24 months" in page
    response = client.post("/run/create-charges/", follow=True)
    text = response.content.decode()
    assert "Charges created: 0. Created (catch-up): 2. Already created: 0." in text
