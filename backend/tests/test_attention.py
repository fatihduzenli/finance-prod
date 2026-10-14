"""The attention list (docs/qa/PLAN-011b D6): every kind appears with its link, the
returned-debit text verbatim, the boundaries of each window, the operator's own items as
banners, the dashboard shows the list above the figures, and the reads are bounded; a
lease-end balance stays listed until it is paid (PLAN-012e D14)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape

from accounts.roles import Role
from ledger.models import (
    CalendarEntry,
    CollectionSchedule,
    CollectionStatus,
    ContactKind,
    InstallmentPlan,
    InsurancePremium,
    Lease,
    LeaseEnd,
    OneOffCharge,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
    Store,
    StoreCost,
    StoreCostSource,
    StoreStatus,
)
from ledger.services import attention, contacts
from payments.models import (
    MismatchKind,
    ReconciliationMismatch,
    ReconciliationRun,
    ReconciliationStatus,
    ReconciliationTrigger,
    WebhookEvent,
    WebhookOutcome,
)
from tests import test_lease_end as le
from tests import test_transfer as tt
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_operator,
)
from tests.test_operator_views import client_for
from tests.test_setup_charges import pay

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    return TODAY


def kinds(found: attention.Attention) -> list[str]:
    return [item.kind for item in found.items]


def of_kind(kind: str, operator: Operator | None = None) -> list[attention.Item]:
    found = attention.attention(TODAY, operator=operator.pk if operator else None, limit=None)
    return [item for item in found.items if item.kind == kind]


def opened(store: Store, day: date) -> Store:
    Store.objects.filter(pk=store.pk).update(open_date=day, needs_open_date=False)
    store.refresh_from_db()
    return store


def returned_installment(lease: Lease) -> InstallmentPlan:
    """A plan of one installment's returned debit, not resolved (PLAN-012e D11)."""
    assert lease.operator is not None
    plan = InstallmentPlan.objects.create(
        rent_charge=a_rent_charge(lease, date(2026, 7, 1), RentChargeStatus.UNPAID),
        operator=lease.operator,
        lease=lease,
        count=2,
        first_due=date(2026, 9, 1),
        cadence="monthly",
        collection="autopay_due",
        status="attention",
        confirmed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    installment = a_one_off_charge(
        lease.operator,
        "1750.00",
        date(2026, 9, 1),
        lease=lease,
        category="installment",
        collection="autopay_due",
        description="Installment 1 of 2: Rent 2026-07",
        plan=plan,
        installment_no=1,
    )
    Payment.objects.create(
        one_off_charge=installment,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("1750.00"),
        status=PaymentStatus.RETURNED,
        initiated_on=date(2026, 9, 1),
        returned_at=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        return_code="R01",
    )
    return plan


def returned_payment(lease: Lease, amount: str = "3500.00") -> Payment:
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.UNPAID)
    return Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D(amount),
        status=PaymentStatus.RETURNED,
        initiated_on=date(2026, 10, 1),
        # 01:30 UTC on Oct 4 is the evening of Oct 3 in New York.
        returned_at=datetime(2026, 10, 4, 1, 30, tzinfo=UTC),
        return_code="R01",
    )


# --- Each kind -----------------------------------------------------------------------------


def test_overdue_charges_worst_first_with_links(today: date) -> None:
    lease = a_lease(a_store("2204 FOREST PARK", 2204), an_operator("Acme"), debit_day=5)
    old = a_rent_charge(lease, date(2026, 8, 1), RentChargeStatus.UNPAID, "3500.00")
    recent = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.PARTIAL, "3500.00", "1000.00")
    a_rent_charge(lease, date(2026, 11, 1), RentChargeStatus.DUE, "3500.00")  # not yet due
    items = of_kind(attention.OVERDUE)
    assert [item.url for item in items] == [f"/charges/{old.pk}/", f"/charges/{recent.pk}/"]
    assert items[0].text == "Overdue 62 days: 2204 FOREST PARK, Rent 2026-08, $3,500.00"
    assert items[0].severity == attention.HIGH
    assert items[1].text == "Overdue 1 day: 2204 FOREST PARK, Rent 2026-10, $2,500.00"
    assert items[1].severity == attention.MEDIUM


def test_a_charge_due_today_is_not_overdue(today: date) -> None:
    lease = a_lease(debit_day=6)
    a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.UNPAID)
    assert of_kind(attention.OVERDUE) == []


def test_a_returned_debit_reads_exactly_as_planned(today: date) -> None:
    lease = a_lease(a_store("3409 LEICESTER", 3409), an_operator("Beta"))
    payment = returned_payment(lease, "1850.00")
    [item] = of_kind(attention.RETURNED_DEBIT)
    assert (
        item.text
        == "Returned debit not rescheduled: 3409 LEICESTER, $1,850.00, returned 2026-10-03"
    )
    assert item.url == f"/returns/#return-{payment.pk}"
    assert item.severity == attention.HIGH and item.since == date(2026, 10, 3)
    # Resolved: gone.
    Payment.objects.filter(pk=payment.pk).update(
        resolved_at=NOW, resolution_note="Paid by wire.", resolution_kind="note"
    )
    assert of_kind(attention.RETURNED_DEBIT) == []


def test_a_broken_promise_is_listed(today: date) -> None:
    operator = an_operator("Acme")
    lease = a_lease(a_store("S"), operator)
    a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.UNPAID)
    contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.PROMISE,
            happened_on=date(2026, 10, 1),
            body="Friday.",
            promised_date=TODAY - timedelta(days=1),
            promised_amount=D("3500.00"),
        ),
        actor=None,
    )
    [item] = of_kind(attention.BROKEN_PROMISE)
    assert item.url == f"/operators/{operator.pk}/#contacts"


def test_insurance_due_in_30_days_and_a_missing_premium(today: date) -> None:
    InsurancePremium.objects.create(policy_year=2026, amount=D("695.00"))
    soon = a_lease(opened(a_store("Soon"), date(2020, 11, 5)), an_operator("A"))  # day 30
    late = a_lease(opened(a_store("Late"), date(2020, 11, 6)), an_operator("B"))  # day 31
    assert late.pk
    due = of_kind(attention.INSURANCE_DUE)
    assert [(item.text, item.url) for item in due] == [
        ("Insurance due 2026-11-05: Soon (A)", f"/leases/{soon.pk}/")
    ]
    # A charge still waiting for its premium.
    waiting = a_lease(opened(a_store("Waiting"), date(2020, 3, 1)), an_operator("C"))
    an_insurance_charge(waiting, 2025, date(2025, 3, 1), None)
    missing = of_kind(attention.INSURANCE_PREMIUM_MISSING)
    assert [item.text for item in missing] == [
        "Insurance premium for 2025 missing, due 2025-03-01: Waiting (C)"
    ]


def test_an_upcoming_anniversary_without_a_premium_is_missing(today: date) -> None:
    lease = a_lease(opened(a_store("NoPremium"), date(2020, 10, 20)), an_operator("D"))
    [item] = of_kind(attention.INSURANCE_PREMIUM_MISSING)
    assert item.text == "Insurance premium for 2026 missing, due 2026-10-20: NoPremium (D)"
    assert item.url == f"/leases/{lease.pk}/"
    assert of_kind(attention.INSURANCE_DUE) == []


def test_leases_ending_in_60_days_including_today(today: date) -> None:
    ends_today = a_lease(a_store("Today"), an_operator("A"))
    ends_day_60 = a_lease(a_store("Sixty"), an_operator("B"))
    ends_day_61 = a_lease(a_store("SixtyOne"), an_operator("C"))
    Lease.objects.filter(pk=ends_today.pk).update(ended_on=TODAY)
    Lease.objects.filter(pk=ends_day_60.pk).update(ended_on=TODAY + timedelta(days=60))
    Lease.objects.filter(pk=ends_day_61.pk).update(ended_on=TODAY + timedelta(days=61))
    items = of_kind(attention.LEASE_ENDING)
    assert [item.url for item in items] == [
        f"/leases/{ends_today.pk}/",
        f"/leases/{ends_day_60.pk}/",
    ]
    assert items[0].text == "Lease ends 2026-10-06: Today (A)"


def test_landlord_leases_ending_in_90_days(today: date) -> None:
    store = a_store("Landlord")
    other = a_store("Later")
    for row_store, end in (
        (store, TODAY + timedelta(days=90)),
        (other, TODAY + timedelta(days=91)),
    ):
        StoreCost.objects.create(
            store=row_store,
            landlord_rent=D("2000.00"),
            landlord_lease_end=end,
            effective_from=date(2025, 1, 1),
            source=StoreCostSource.STAFF,
        )
    [item] = of_kind(attention.LANDLORD_LEASE_ENDING)
    assert item.text == "Landlord lease ends 2027-01-04: Landlord"
    assert item.url == f"/stores/{store.pk}/"


def test_stores_without_an_open_date_and_operators_without_an_email(today: date) -> None:
    store = a_store("No date")
    Store.objects.create(external_label="Closed", status=StoreStatus.CLOSED)
    operator = an_operator("No mail")
    Operator.objects.create(display_name="Has mail", email="ops@example.com")
    Operator.objects.create(display_name="Archived", archived_at=NOW)
    assert [item.url for item in of_kind(attention.STORE_NO_OPEN_DATE)] == [f"/stores/{store.pk}/"]
    assert [item.url for item in of_kind(attention.OPERATOR_NO_EMAIL)] == [
        f"/operators/{operator.pk}/"
    ]


def test_vacant_stores_after_30_days(today: date) -> None:
    # The last lease ended 31 days ago: vacant since the day after it ended.
    ended_long_ago = a_store("Ended31")
    lease = a_lease(ended_long_ago, status=CollectionStatus.ENDED)
    Lease.objects.filter(pk=lease.pk).update(ended_on=TODAY - timedelta(days=31))
    # Ended 29 days ago: not yet.
    recent = a_lease(a_store("Ended29"), status=CollectionStatus.ENDED)
    Lease.objects.filter(pk=recent.pk).update(ended_on=TODAY - timedelta(days=29))
    # Never leased, opened 31 days ago: vacant; opened 10 days ago: not yet.
    never = opened(a_store("Never31"), TODAY - timedelta(days=31))
    opened(a_store("Never10"), TODAY - timedelta(days=10))
    # A store with an active lease is not vacant.
    a_lease(opened(a_store("Leased"), date(2020, 1, 1)))
    items = of_kind(attention.VACANT_STORE)
    assert {item.url for item in items} == {
        f"/stores/{ended_long_ago.pk}/",
        f"/stores/{never.pk}/",
    }
    texts = {item.text for item in items}
    assert f"Vacant since {TODAY - timedelta(days=30)}: Ended31" in texts


def test_a_schedule_flagged_after_a_rent_change(today: date) -> None:
    lease = a_lease(a_store("Flagged"), an_operator("A"))
    CollectionSchedule.objects.create(
        lease=lease,
        kind=ScheduleKind.RENT,
        seq=1,
        amount=D("3500.00"),
        needs_review=True,
        state=ScheduleState.ACTIVE,
    )
    CollectionSchedule.objects.create(
        lease=lease,
        kind=ScheduleKind.INSURANCE,
        seq=1,
        amount=D("695.00"),
        needs_review=True,
        state=ScheduleState.ENDED,
    )
    [item] = of_kind(attention.SCHEDULE_REVIEW)
    assert item.text == "Rent schedule to review after a lease change: Flagged (A)"
    assert item.url == f"/leases/{lease.pk}/"


def test_dead_letters_and_open_mismatches_are_counted(today: date) -> None:
    for n in range(2):
        WebhookEvent.objects.create(
            event_type="update_transaction",
            provider_object_id=f"t{n}",
            dedupe_hash=f"{n:064x}",
            outcome=WebhookOutcome.DEAD_LETTER,
        )
    run = ReconciliationRun.objects.create(
        trigger=ReconciliationTrigger.MANUAL, status=ReconciliationStatus.DONE, started_at=NOW
    )
    ReconciliationMismatch.objects.create(
        run=run,
        kind=MismatchKind.PENDING_TOO_OLD,
        entity_type="payment",
        entity_id="00000000-0000-0000-0000-000000000001",
        detail="Pending for 9 business days.",
    )
    [dead] = of_kind(attention.DEAD_LETTERS)
    assert dead.text == "Dead-letter webhook events: 2" and dead.severity == attention.HIGH
    assert dead.url == "/webhooks/?outcome=dead_letter"
    [mismatch] = of_kind(attention.RECONCILIATION_MISMATCHES)
    assert mismatch.text == "Unresolved reconciliation mismatches: 1"
    assert mismatch.url == "/reconciliation/"


# --- The lists -----------------------------------------------------------------------------


def test_the_operator_filter_keeps_only_that_operators_items(today: date) -> None:
    mine = an_operator("Mine")
    other = an_operator("Other")
    my_lease = a_lease(a_store("My store"), mine)
    other_lease = a_lease(a_store("Their store"), other)
    a_rent_charge(my_lease, date(2026, 9, 1), RentChargeStatus.UNPAID)
    a_rent_charge(other_lease, date(2026, 9, 1), RentChargeStatus.UNPAID)
    returned_payment(other_lease)
    found = attention.attention(TODAY, operator=mine.pk)
    assert found.items and all(item.operator_id == mine.pk for item in found.items)
    assert attention.VACANT_STORE not in kinds(found)
    assert attention.STORE_NO_OPEN_DATE not in kinds(found)


def test_items_are_ordered_by_severity_and_limited_per_kind(today: date) -> None:
    for n in range(4):
        lease = a_lease(a_store(f"S{n}"), an_operator(f"O{n}"))
        a_rent_charge(lease, date(2026, 8, 1), RentChargeStatus.UNPAID)
    found = attention.attention(TODAY, limit=2)
    overdue = [item for item in found.items if item.kind == attention.OVERDUE]
    assert len(overdue) == 2 and found.more[attention.OVERDUE] == 2
    assert ("overdue charges", 2) in found.more_lines
    ranks = [("high", "medium", "low").index(item.severity) for item in found.items]
    assert ranks == sorted(ranks)


def build(n: int) -> None:
    """``n`` of every kind of item."""
    InsurancePremium.objects.get_or_create(policy_year=2026, defaults={"amount": D("600.00")})
    for i in range(n):
        operator = an_operator(f"Op {n}-{i}")
        store = opened(a_store(f"St {n}-{i}"), date(2020, 10, 20))
        lease = a_lease(store, operator)
        Lease.objects.filter(pk=lease.pk).update(ended_on=TODAY + timedelta(days=20))
        a_rent_charge(lease, date(2026, 8, 1), RentChargeStatus.UNPAID)
        returned_payment(lease)
        returned_installment(lease)
        LeaseEnd.objects.create(
            lease=lease,
            ended_on=TODAY - timedelta(days=1),
            reason="moved_out",
            status="confirmed",
            confirmed_at=NOW,
            debt_charge=a_one_off_charge(
                operator,
                "300.00",
                TODAY + timedelta(days=14),
                lease=lease,
                category="other",
                description="Lease-end balance",
            ),
        )
        an_insurance_charge(lease, 2025, date(2025, 10, 20), None)
        CollectionSchedule.objects.create(
            lease=lease, kind=ScheduleKind.RENT, seq=1, amount=D("3500.00"), needs_review=True
        )
        contacts.add_entry(
            operator.pk,
            contacts.ContactInput(
                kind=ContactKind.PROMISE,
                happened_on=date(2026, 10, 1),
                body="Soon.",
                promised_date=date(2026, 10, 2),
                promised_amount=D("100.00"),
            ),
            actor=None,
        )
        StoreCost.objects.create(
            store=store,
            landlord_rent=D("1.00"),
            landlord_lease_end=TODAY + timedelta(days=9),
            effective_from=date(2025, 1, 1),
            source=StoreCostSource.STAFF,
        )
        a_store(f"No date {n}-{i}")
        vacant = opened(a_store(f"Vacant {n}-{i}"), date(2020, 1, 1))
        assert vacant.pk
        CalendarEntry.objects.create(kind="note", on=TODAY, title=f"Today {i}", operator=operator)
        WebhookEvent.objects.create(
            event_type="x",
            provider_object_id=f"{n}-{i}",
            dedupe_hash=f"{n * 100 + i:064x}",
            outcome=WebhookOutcome.DEAD_LETTER,
        )


def queries(operator: Operator | None = None) -> int:
    with CaptureQueriesContext(connection) as captured:
        found = attention.attention(TODAY, operator=operator.pk if operator else None)
    assert found.items
    return len(captured)


def test_the_list_reads_a_fixed_number_of_queries(today: date) -> None:
    build(1)
    small = queries()
    small_operator = queries(Operator.objects.order_by("display_name").first())
    build(6)
    every = attention.attention(TODAY, limit=None)
    assert set(kinds(every)) >= set(attention.KINDS) - {attention.RECONCILIATION_MISMATCHES}
    assert queries() == small
    assert queries(Operator.objects.order_by("display_name").first()) == small_operator


def test_a_returned_installment_is_one_item_per_plan_linking_to_it(today: date) -> None:
    lease = a_lease(a_store("2204 FOREST PARK", 2204), an_operator("Acme"))
    plan = returned_installment(lease)
    items = [
        item
        for item in attention.attention(TODAY).items
        if item.kind == attention.INSTALLMENT_RETURNED
    ]
    assert [(item.text, item.url, item.since, item.severity) for item in items] == [
        (
            "Installment returned: 2204 FOREST PARK, Rent 2026-07, plan 0 of 2 paid",
            f"/plans/{plan.pk}/",
            date(2026, 9, 4),
            attention.HIGH,
        )
    ]
    assert attention.attention(TODAY, operator=lease.operator_id).items
    Payment.objects.filter(one_off_charge__plan=plan).update(
        resolved_at=datetime(2026, 10, 5, tzinfo=UTC),
        resolution_note="Paid by wire",
        resolution_kind="note",
    )
    assert not [
        item
        for item in attention.attention(TODAY).items
        if item.kind == attention.INSTALLMENT_RETURNED
    ]


# --- The pages -----------------------------------------------------------------------------


def test_the_dashboard_shows_the_list_above_the_figures(today: date) -> None:
    lease = a_lease(a_store("2204 FOREST PARK", 2204), an_operator("Acme"))
    returned_payment(lease)
    client, _ = client_for(Role.VIEWER)
    page = client.get("/").content.decode()
    assert page.index('id="attention"') < page.index('class="figures"')
    assert (
        "Returned debit not rescheduled: 2204 FOREST PARK, $3,500.00, returned 2026-10-03" in page
    )
    assert 'href="/returns/#return-' in page


def test_the_dashboard_says_when_nothing_needs_attention(today: date) -> None:
    client, _ = client_for(Role.VIEWER)
    page = client.get("/").content.decode()
    assert 'id="attention-none"' in page


def test_the_operator_page_shows_its_own_items_as_banners(today: date) -> None:
    operator = an_operator("Acme")
    lease = a_lease(a_store("Mine"), operator)
    returned_payment(lease)
    other = a_lease(a_store("Theirs"), an_operator("Other"))
    returned_payment(other)
    client, _ = client_for(Role.VIEWER)
    page = client.get(f"/operators/{operator.pk}/").content.decode()
    assert 'class="attention attention-banners"' in page
    assert "Returned debit not rescheduled: Mine" in page
    assert "Theirs" not in page


# --- PLAN-012e D14: lease-end balances ----------------------------------------------------


def balances(operator: uuid.UUID | None = None) -> list[attention.Item]:
    found = attention.attention(le.TODAY, operator=operator, limit=None)
    return [item for item in found.items if item.kind == attention.LEASE_END_BALANCE]


def a_plain_balance() -> LeaseEnd:
    """A lease ended (not transferred) with $300.00 of deductions above the deposit."""
    lease = le.a_store_lease()
    le.october(lease, paid="1225.81")
    le.deposit(lease, "500.00")
    end = le.draft(lease)
    assert le.deductions(end, ("damage", "Broken door", "800.00")).ok
    assert le.confirm(end).ok
    return LeaseEnd.objects.get(pk=end.pk)


def expected(end: LeaseEnd, amount: str, marker: str = "") -> str:
    lease = end.lease
    assert lease.operator is not None
    return (
        f"Lease-end balance: {lease.operator.display_name} on store "
        f"{lease.store.external_label}, {amount} open since {le.ENDED.isoformat()}{marker}"
    )


def test_a_transfer_balance_allowed_by_an_admin_is_listed_with_its_marker() -> None:
    _, user = client_for(Role.ADMIN)
    row = tt.owing(user)
    assert tt.allow(row, user).ok
    assert tt.confirm(row, user).ok
    end = le.fresh(row.lease_end)
    charge = end.debt_charge
    assert charge is not None
    [item] = balances()
    marker = f" (transfer, allowed by {user.get_username()})"
    assert (item.text, item.url, item.since, item.severity, item.operator_id) == (
        expected(end, "$500.00", marker),
        reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk}),
        le.ENDED,
        attention.HIGH,
        row.outgoing_lease.operator_id,
    )
    # The outgoing operator's banners carry it; the incoming operator's do not.
    assert [found.text for found in balances(row.outgoing_lease.operator_id)] == [item.text]
    assert not balances(le.fresh(row).incoming_operator_id)


def test_a_plain_lease_end_balance_is_listed_without_the_marker() -> None:
    end = a_plain_balance()
    [item] = balances()
    assert item.text == expected(end, "$300.00")
    assert "(transfer" not in item.text


def test_the_balance_shrinks_with_a_payment_and_goes_once_paid() -> None:
    end = a_plain_balance()
    charge = end.debt_charge
    assert charge is not None
    assert pay(charge, "100.00").ok
    [item] = balances()
    assert item.text == expected(end, "$200.00")
    assert pay(OneOffCharge.objects.get(pk=charge.pk), "200.00").ok
    assert OneOffCharge.objects.get(pk=charge.pk).status == "paid"
    assert not balances()


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "waived", "waived_at": NOW, "waive_note": "Forgiven."},
        {"status": "voided", "voided_at": NOW, "void_note": "Entered in error."},
    ],
)
def test_a_waived_or_voided_balance_is_not_listed(fields: dict[str, object]) -> None:
    end = a_plain_balance()
    assert balances()
    assert end.debt_charge_id is not None
    OneOffCharge.objects.filter(pk=end.debt_charge_id).update(**fields)
    assert not balances()


def test_a_draft_lease_end_or_one_without_a_balance_is_not_listed() -> None:
    lease = le.a_store_lease()
    le.october(lease, paid="1225.81")
    le.deposit(lease, "500.00")
    end = le.draft(lease)
    assert le.deductions(end, ("damage", "Broken door", "800.00")).ok
    assert not balances()
    assert le.deductions(end, ("damage", "Broken door", "400.00")).ok
    assert le.settle(le.fresh(end)).ok
    assert le.confirm(end).ok
    assert LeaseEnd.objects.get(pk=end.pk).debt_charge is None
    assert not balances()


def test_a_viewer_sees_the_balance_on_the_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    end = a_plain_balance()
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: le.TODAY)
    client, _ = client_for(Role.VIEWER)
    page = client.get("/").content.decode()
    assert f'data-kind="{attention.LEASE_END_BALANCE}"' in page
    assert escape(expected(end, "$300.00")) in page
    assert f'href="/one-off-charges/{end.debt_charge_id}/"' in page
