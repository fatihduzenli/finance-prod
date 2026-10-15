"""The rent run (docs/qa/PLAN-007-leases-and-rent-run.md §6 rows 5, 6, 7 and 9; D6 to D8).

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`; the breaker and the
failure budget are fresh in-memory instances for every test. Nothing opens a socket.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.test import override_settings

from ledger.models import (
    AuditLog,
    CollectionStatus,
    Invoice,
    Lease,
    LeaseRentHistory,
    Payment,
    PaymentChannel,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import leases as lease_service
from ledger.services import rent_run
from ledger.services.rent_run import MatchAction
from payments.errors import ProviderUnavailable
from payments.fake import FakeProvider
from payments.models import ExternalRef
from payments.provider import InvoiceRecord
from payments.stax.breaker import CircuitBreaker, Outcome, reset_breaker
from payments.stax.budget import FailureBudget, reset_budget
from payments.stax.state import MemoryStateStore
from tests.test_lease_services import (
    CUSTOMER,
    _race,
    edit,
    make_lease,
    make_operator,
    method,
)
from tests.test_operator_sync import requires_postgresql

pytestmark = pytest.mark.django_db

D = Decimal
#: 2026-10-01 09:00 UTC: autopay enabled now starts on the 15th of October.
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
DAY = date(2026, 10, 15)
PERIOD = date(2026, 10, 1)
RENT = Decimal("1500.00")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def health() -> Iterator[tuple[CircuitBreaker, FailureBudget]]:
    breaker = CircuitBreaker(MemoryStateStore(), on_open=lambda opened, reopen: None)
    budget = FailureBudget(MemoryStateStore())
    reset_breaker(breaker)
    reset_budget(budget)
    yield breaker, budget
    reset_breaker()
    reset_budget()


def lease(**overrides: Any) -> Lease:
    """A lease of its own store and operator; the operator has its own customer ID.

    Unless a test gives its own start, it starts on 1 October 2026, so the run's
    catch-up (PLAN-012b-b D1) finds no earlier month to charge, and its rent row keeps
    the 1 January 2026 start these tests were written with (a rent change from
    2 October is then allowed)."""
    values: dict[str, Any] = {"debit_day": 15, "started_on": PERIOD}
    values.update(overrides)
    if "operator" not in values:
        tag = uuid.uuid4().hex[:8]
        values["operator"] = make_operator(email=f"{tag}@example.com", customer=f"cus-{tag}")
    row = make_lease(**values)
    if "started_on" not in overrides:
        LeaseRentHistory.objects.filter(lease=row).update(effective_from=date(2026, 1, 1))
    return row


def enrolled(fake: FakeProvider, **overrides: Any) -> tuple[Lease, list[str]]:
    """A lease with active autopay; its provider schedule IDs in seq order."""
    row = lease(**overrides)
    customer = lease_service.customer_ref(row.operator)
    bank = f"pm-{customer}"
    fake.add_payment_method(customer, method(bank))
    outcome = lease_service.enable_autopay(row.pk, bank, actor=None, now=NOW)
    assert outcome.ok, outcome.text
    refs = [lease_service.schedule_ref(s) for s in lease_service.live_schedules(row)]
    return row, refs


def invoice(
    fake: FakeProvider,
    schedule: str,
    *,
    dated: str = "2026-10-15 12:00:00",
    total: Decimal | None = RENT,
    status: str = "FAKE-SENT",
    external_id: str | None = None,
) -> InvoiceRecord:
    """An invoice the provider generated from ``schedule`` (as listed by GET /invoice)."""
    record = InvoiceRecord(
        external_id=external_id or f"fake-inv-{len(fake.invoices) + 1:04d}",
        raw_status=status,
        fetched_at=NOW,
        customer_external_id=CUSTOMER,
        total=total,
        raw_summary={"schedule_id": schedule, "invoice_date_at": dated},
    )
    fake.invoices[record.external_id] = record
    return record


def charge_of(row: Lease, seq: int = 1, period: date = PERIOD) -> RentCharge:
    return RentCharge.objects.get(lease=row, period_month=period, seq=seq)


def audit_actions(entity_type: str) -> list[str]:
    return list(
        AuditLog.objects.filter(entity_type=entity_type)
        .order_by("id")
        .values_list("action", flat=True)
    )


# --- Row 5: who is due, idempotent charges ------------------------------------------------------


def test_leases_due_lists_the_leases_whose_debit_day_is_today() -> None:
    due = lease(debit_day=15)
    lease(debit_day=16)
    run = rent_run.leases_due(DAY)
    assert [(item.lease.pk, item.seq, item.amount) for item in run.due] == [
        (due.pk, 1, D("1500.00"))
    ]
    assert run.skipped == ()


def test_create_charges_twice_creates_each_charge_once() -> None:
    row = lease()
    first = rent_run.create_charges(DAY, actor=None)
    second = rent_run.create_charges(DAY, actor=None)
    assert (len(first.created), len(first.already)) == (1, 0)
    assert (len(second.created), len(second.already)) == (0, 1)
    charge = charge_of(row)
    assert second.already[0].pk == charge.pk
    assert (charge.due_on, charge.amount_due, charge.amount_paid, charge.status) == (
        DAY,
        D("1500.00"),
        D("0.00"),
        RentChargeStatus.DUE,
    )
    assert RentCharge.objects.count() == 1
    assert audit_actions("rent_charge") == ["rent_charge.created"]


@pytest.mark.parametrize(
    ("status", "channel", "reason"),
    [
        (CollectionStatus.PAUSED, PaymentChannel.PROVIDER_ACH, rent_run.SKIP_PAUSED),
        (CollectionStatus.PROSPECTIVE, PaymentChannel.WIRE, rent_run.SKIP_PROSPECTIVE),
        (CollectionStatus.ENDED, PaymentChannel.ZELLE, rent_run.SKIP_ENDED),
        (CollectionStatus.PAUSED, PaymentChannel.UNKNOWN, rent_run.SKIP_PAUSED),
    ],
)
def test_leases_that_are_not_active_are_skipped_with_a_reason_and_get_no_charge(
    status: str, channel: str, reason: str
) -> None:
    row = lease(collection_status=status, payment_channel=channel)
    report = rent_run.create_charges(DAY, actor=None)
    assert report.created == ()
    assert [(item.lease.pk, item.reason) for item in report.skipped] == [(row.pk, reason)]
    assert not RentCharge.objects.exists()


def test_an_active_lease_with_an_unknown_channel_is_skipped() -> None:
    # The database refuses this combination; the run refuses it again on its own.
    row = Lease(collection_status=CollectionStatus.ACTIVE, payment_channel=PaymentChannel.UNKNOWN)
    assert rent_run._skip_reason(row, DAY) == rent_run.SKIP_UNKNOWN_CHANNEL


def test_wire_zelle_and_other_leases_are_charged_like_autopay_leases() -> None:
    rows = [
        lease(payment_channel=channel)
        for channel in (PaymentChannel.WIRE, PaymentChannel.ZELLE, PaymentChannel.OTHER)
    ]
    report = rent_run.create_charges(DAY, actor=None)
    assert {charge.lease_id for charge in report.created} == {row.pk for row in rows}


def test_a_lease_that_starts_later_or_has_no_rent_yet_is_skipped() -> None:
    later = lease(started_on=date(2026, 10, 16))
    no_rent = lease(started_on=None)
    LeaseRentHistory.objects.filter(lease=no_rent).update(effective_from=date(2026, 10, 20))
    run = rent_run.leases_due(DAY)
    assert run.due == ()
    assert {(item.lease.pk, item.reason) for item in run.skipped} == {
        (later.pk, rent_run.SKIP_NOT_STARTED),
        (no_rent.pk, rent_run.SKIP_NO_RENT),
    }


def test_twice_monthly_leases_get_seq_1_and_2_on_their_own_days() -> None:
    row = lease(rent=D("1000.01"), debit_day=5, second_debit_day=20)
    first = rent_run.create_charges(date(2026, 10, 5), actor=None)
    second = rent_run.create_charges(date(2026, 10, 20), actor=None)
    assert [(c.seq, c.amount_due) for c in first.created] == [(1, D("500.01"))]
    assert [(c.seq, c.amount_due) for c in second.created] == [(2, D("500.00"))]
    assert rent_run.create_charges(date(2026, 10, 15), actor=None).created == ()
    assert RentCharge.objects.filter(lease=row, period_month=PERIOD).count() == 2


def test_a_debit_day_past_the_month_end_falls_on_the_last_day() -> None:
    row = lease(
        debit_day=31, collection_status=CollectionStatus.ACTIVE, started_on=date(2026, 9, 1)
    )
    assert rent_run.leases_due(date(2026, 9, 29)).due == ()
    report = rent_run.create_charges(date(2026, 9, 30), actor=None)
    assert [(c.lease_id, c.period_month, c.due_on) for c in report.created] == [
        (row.pk, date(2026, 9, 1), date(2026, 9, 30))
    ]


def test_the_charge_uses_the_rent_in_effect_on_the_due_date() -> None:
    row = lease()
    edit(row, D("1650.00"), date(2026, 11, 1))
    rent_run.create_charges(DAY, actor=None)
    rent_run.create_charges(date(2026, 11, 15), actor=None)
    assert charge_of(row).amount_due == D("1500.00")
    assert charge_of(row, period=date(2026, 11, 1)).amount_due == D("1650.00")


def test_the_run_shows_last_months_charge_of_the_same_debit() -> None:
    row = lease(started_on=date(2026, 9, 1))
    rent_run.create_charges(date(2026, 9, 15), actor=None)
    [item] = rent_run.leases_due(DAY).due
    assert item.charge is None
    assert item.last_month is not None and item.last_month.period_month == date(2026, 9, 1)
    rent_run.create_charges(DAY, actor=None)
    [item] = rent_run.leases_due(DAY).due
    assert item.charge == charge_of(row)


def test_creating_charges_never_calls_the_provider(fake: FakeProvider) -> None:
    lease()
    rent_run.create_charges(DAY, actor=None)
    assert fake.calls == []


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_runs_create_each_charge_once() -> None:
    rows = [lease() for _ in range(3)]
    reports = _race(
        lambda: rent_run.create_charges(DAY, actor=None),
        lambda: rent_run.create_charges(DAY, actor=None),
    )
    assert sum(len(report.created) for report in reports) == 3
    assert sum(len(report.already) for report in reports) == 3
    assert RentCharge.objects.filter(lease__in=rows).count() == 3


# --- Row 6: matching ----------------------------------------------------------------------------


def test_a_schedule_invoice_in_the_period_is_linked_to_its_charge(fake: FakeProvider) -> None:
    row, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    record = invoice(fake, ref)
    report = rent_run.match_schedule_invoices(actor=None)
    assert report.schedules == 1
    assert [line.action for line in report.lines] == [MatchAction.LINKED]
    [local] = Invoice.objects.all()
    charge = charge_of(row)
    assert local.rent_charge == charge
    assert local.operator == row.operator
    assert local.invoice_number == "INV-2026-0001"
    assert (local.issue_date, local.due_date) == (DAY, DAY)
    assert (local.subtotal, local.tax, local.total) == (D("1500.00"), D("0.00"), D("1500.00"))
    assert (local.allow_card, local.allow_ach) == (False, True)
    assert local.provider_status == "FAKE-SENT"
    ref_row = ExternalRef.objects.get(external_kind="invoice")
    assert (ref_row.entity_type, ref_row.entity_id, ref_row.external_id) == (
        "invoice",
        local.pk,
        record.external_id,
    )
    assert not charge.needs_review and charge.amount_due == D("1500.00")
    assert "invoice.matched" in audit_actions("invoice")
    # One list call, filtered by the schedule.
    [(name, (query,))] = [call for call in fake.calls if call[0] == "list_invoices"]
    assert query.schedule_external_id == ref  # type: ignore[attr-defined]


def test_provider_status_is_written_only_inside_provider_write(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from payments import invoice_sync
    from payments.sync_writer import provider_write

    seen: list[str] = []

    def spy() -> Any:
        seen.append("entered")
        return provider_write()

    monkeypatch.setattr(invoice_sync, "provider_write", spy)
    _, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, ref)
    rent_run.match_schedule_invoices(actor=None)
    assert seen == ["entered"]
    assert Invoice.objects.get().provider_status == "FAKE-SENT"


def test_a_total_mismatch_flags_the_charge_and_never_changes_the_amount_due(
    fake: FakeProvider,
) -> None:
    row, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, ref, total=D("1499.99"))
    report = rent_run.match_schedule_invoices(actor=None)
    [line] = report.lines
    assert line.action is MatchAction.MISMATCH
    assert "collection on this charge stops" in line.message
    charge = charge_of(row)
    assert (charge.amount_due, charge.needs_review) == (D("1500.00"), True)
    assert Invoice.objects.get().total == D("1499.99")
    assert rent_run.charge_review_message(charge) == rent_run.TOTAL_MISMATCH
    entry = AuditLog.objects.get(action="rent_charge.total_mismatch")
    assert (entry.after or {})["provider_total"] == "1499.99"
    assert (entry.after or {})["amount_due"] == "1500.00"


def test_matching_twice_links_once_and_assigns_the_number_once(fake: FakeProvider) -> None:
    _, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, ref)
    rent_run.match_schedule_invoices(actor=None)
    again = rent_run.match_schedule_invoices(actor=None)
    assert [line.action for line in again.lines] == [MatchAction.ALREADY]
    assert Invoice.objects.count() == 1
    assert Invoice.objects.get().invoice_number == "INV-2026-0001"
    assert audit_actions("invoice") == ["invoice.matched"]


def test_two_charges_get_consecutive_numbers(fake: FakeProvider) -> None:
    _, [first] = enrolled(fake)
    _, [second] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, first)
    invoice(fake, second)
    rent_run.match_schedule_invoices(actor=None)
    assert sorted(Invoice.objects.values_list("invoice_number", flat=True)) == [
        "INV-2026-0001",
        "INV-2026-0002",
    ]


def test_twice_monthly_invoices_link_to_the_charge_of_their_own_schedule(
    fake: FakeProvider,
) -> None:
    row, [one, two] = enrolled(fake, debit_day=5, second_debit_day=20)
    rent_run.create_charges(date(2026, 10, 5), actor=None)
    rent_run.create_charges(date(2026, 10, 20), actor=None)
    invoice(fake, one, dated="2026-10-05", total=D("750.00"))
    invoice(fake, two, dated="2026-10-20", total=D("750.00"))
    rent_run.match_schedule_invoices(actor=None)
    assert charge_of(row, 1).invoices.count() == 1
    assert charge_of(row, 2).invoices.count() == 1


@pytest.mark.parametrize(
    "dated",
    ["", "not-a-date", "2026-10-31T23:30:00-05:00", "2026-11-01T00:30:00+05:00", None],
    ids=["empty", "garbage", "west-of-utc-crosses-month", "east-of-utc-crosses-day", "missing"],
)
def test_an_invoice_whose_period_cannot_be_determined_is_reported_not_guessed(
    fake: FakeProvider, dated: str | None
) -> None:
    row, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    record = invoice(fake, ref)
    summary: dict[str, Any] = {"schedule_id": ref}
    if dated is not None:
        summary["invoice_date_at"] = dated
    fake.invoices[record.external_id] = InvoiceRecord(
        external_id=record.external_id,
        raw_status=record.raw_status,
        fetched_at=NOW,
        total=record.total,
        raw_summary=summary,
    )
    [line] = rent_run.match_schedule_invoices(actor=None).lines
    assert (line.action, line.message) == (MatchAction.REPORTED, rent_run.MATCH_NO_DATE)
    assert not Invoice.objects.exists() and not charge_of(row).needs_review


def test_a_utc_timestamp_on_the_same_day_is_accepted(fake: FakeProvider) -> None:
    _, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, ref, dated="2026-10-15T12:00:00Z")
    [line] = rent_run.match_schedule_invoices(actor=None).lines
    assert line.action is MatchAction.LINKED


def test_an_invoice_without_a_charge_in_its_period_is_reported(fake: FakeProvider) -> None:
    _, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, ref, dated="2026-11-15")
    [line] = rent_run.match_schedule_invoices(actor=None).lines
    assert line.action is MatchAction.REPORTED
    assert line.message == rent_run.MATCH_NO_CHARGE.format(period="2026-11", seq=1)
    assert not Invoice.objects.exists()


def test_an_invoice_naming_another_schedule_is_not_linked(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    stray = invoice(fake, "fake-schedule-other")
    # A provider that ignored the filter would return it for this schedule.
    monkeypatch.setattr(fake, "list_invoices", lambda query: [stray])
    [line] = rent_run.match_schedule_invoices(actor=None).lines
    assert (line.action, line.message) == (MatchAction.REPORTED, rent_run.MATCH_OTHER_SCHEDULE)
    assert not Invoice.objects.exists()


@pytest.mark.parametrize("total", [None, D("-1.00"), D("1500.001")])
def test_an_invoice_without_a_usable_total_is_reported(
    fake: FakeProvider, total: Decimal | None
) -> None:
    _, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, ref, total=total)
    [line] = rent_run.match_schedule_invoices(actor=None).lines
    assert (line.action, line.message) == (MatchAction.REPORTED, rent_run.MATCH_NO_TOTAL)


def test_a_second_invoice_for_an_invoiced_charge_is_reported_and_flags_it_once(
    fake: FakeProvider,
) -> None:
    row, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, ref, dated="2026-10-15")
    invoice(fake, ref, dated="2026-10-16")
    report = rent_run.match_schedule_invoices(actor=None)
    assert [line.action for line in report.lines] == [MatchAction.LINKED, MatchAction.REPORTED]
    assert charge_of(row).needs_review
    assert rent_run.charge_review_message(charge_of(row)) == rent_run.SECOND_INVOICE
    rent_run.match_schedule_invoices(actor=None)
    assert Invoice.objects.count() == 1
    assert audit_actions("rent_charge").count("rent_charge.second_invoice") == 1


def test_paused_schedules_are_not_read(fake: FakeProvider) -> None:
    row, [ref] = enrolled(fake)
    lease_service.pause_autopay(row.pk, actor=None)
    fake.calls.clear()
    report = rent_run.match_schedule_invoices(actor=None)
    assert report.schedules == 0 and fake.calls == []


def test_a_provider_failure_stops_matching_and_is_audited(fake: FakeProvider) -> None:
    _, [ref] = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, ref)
    fake.fail_next("list_invoices", ProviderUnavailable(503))
    report = rent_run.match_schedule_invoices(actor=None)
    assert report.failure == rent_run.MATCH_FAILED and report.lines == []
    entry = AuditLog.objects.get(action="rent_run.match_failed")
    assert (entry.after or {})["failure"] == "ProviderUnavailable"
    assert ref not in str(entry.after)


# --- Row 9: breaker and budget before any provider call -----------------------------------------


def test_matching_is_refused_without_a_call_while_the_breaker_is_open(
    fake: FakeProvider, health: tuple[CircuitBreaker, FailureBudget]
) -> None:
    breaker, _ = health
    _, [ref] = enrolled(fake)
    invoice(fake, ref)
    for _ in range(3):
        breaker.record(Outcome.CLIENT_ERROR)
    assert breaker.is_open()
    fake.calls.clear()
    report = rent_run.match_schedule_invoices(actor=None)
    assert report.refused and "paused after repeated failures" in report.refused[0]
    assert fake.calls == []
    assert breaker.is_open()  # reading the state did not move it to half-open


def test_matching_is_refused_without_a_call_while_the_budget_is_exhausted(
    fake: FakeProvider, health: tuple[CircuitBreaker, FailureBudget]
) -> None:
    _, budget = health
    _, [ref] = enrolled(fake)
    for _ in range(5):
        budget.record_http_failure()
    fake.calls.clear()
    report = rent_run.match_schedule_invoices(actor=None)
    assert report.refused and "debit would be refused" in report.refused[0]
    assert fake.calls == []


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("1500.00"))
def test_the_large_debit_flag_starts_above_the_threshold() -> None:
    lease(rent=D("1500.00"))
    lease(rent=D("1500.01"))
    flags = sorted((item.amount, item.large) for item in rent_run.leases_due(DAY).due)
    assert flags == [(D("1500.00"), False), (D("1500.01"), True)]


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("1500.00"), PROVIDER_MAX_TXN_AMOUNT=D("3000.00"))
def test_the_over_limit_flag_starts_above_the_confirmed_limit_and_charges_are_created() -> None:
    lease(rent=D("3000.00"))
    lease(rent=D("3000.01"))
    flags = sorted(
        (item.amount, item.large, item.over_limit) for item in rent_run.leases_due(DAY).due
    )
    assert flags == [(D("3000.00"), True, False), (D("3000.01"), True, True)]
    report = rent_run.create_charges(DAY, actor=None)
    assert len(report.created) == 2


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("1500.00"), PROVIDER_MAX_TXN_AMOUNT=None)
def test_no_over_limit_flag_while_the_limit_is_unknown() -> None:
    lease(rent=D("999999.99"))
    [item] = rent_run.leases_due(DAY).due
    assert (item.large, item.over_limit) == (True, False)


# --- Row 7: manual payments ---------------------------------------------------------------------


def charged(**overrides: Any) -> RentCharge:
    row = lease(payment_channel=PaymentChannel.WIRE, **overrides)
    rent_run.create_charges(DAY, actor=None)
    return charge_of(row)


def pay(charge: RentCharge, amount: str, **overrides: Any) -> rent_run.PaymentOutcome:
    values: dict[str, Any] = {
        "method": PaymentMethod.WIRE,
        "amount": D(amount),
        "paid_on": DAY,
        "note": "Wire received, bank reference in the statement of 15 October.",
        "actor": None,
        "today": DAY,
    }
    values.update(overrides)
    return rent_run.record_manual_payment(charge.pk, **values)


def test_a_partial_then_a_full_payment_recompute_paid_and_status(fake: FakeProvider) -> None:
    charge = charged()
    first = pay(charge, "500.00")
    assert first.ok and first.payment is not None
    charge.refresh_from_db()
    assert (charge.amount_paid, charge.balance, charge.status) == (
        D("500.00"),
        D("1000.00"),
        RentChargeStatus.PARTIAL,
    )
    assert pay(charge, "1000.00", method=PaymentMethod.ZELLE).ok
    charge.refresh_from_db()
    assert (charge.amount_paid, charge.balance, charge.status) == (
        D("1500.00"),
        D("0.00"),
        RentChargeStatus.PAID,
    )
    payment = Payment.objects.get(pk=first.payment.pk)
    assert (payment.kind, payment.method, payment.status, payment.settled_on) == (
        "rent",
        PaymentMethod.WIRE,
        PaymentStatus.SETTLED,
        DAY,
    )
    entries = AuditLog.objects.filter(action="rent_charge.payment_recorded").order_by("id")
    assert [(e.before or {})["status"] for e in entries] == ["due", "partial"]
    assert [(e.after or {})["status"] for e in entries] == ["partial", "paid"]
    assert fake.calls == []  # no provider call


@pytest.mark.parametrize("note", ["", "   \n "])
def test_the_note_is_mandatory(note: str) -> None:
    charge = charged()
    outcome = pay(charge, "10.00", note=note)
    assert (outcome.ok, outcome.message, outcome.field) == (
        False,
        rent_run.PAY_NOTE_REQUIRED,
        "note",
    )
    assert not Payment.objects.exists()


def test_a_payment_above_the_balance_is_refused_with_a_fixed_message() -> None:
    charge = charged()
    assert pay(charge, "1000.00").ok
    outcome = pay(charge, "500.01")
    assert (outcome.ok, outcome.message) == (False, rent_run.PAY_OVER_BALANCE)
    assert outcome.message == "The amount is more than the balance due. Nothing was recorded."
    charge.refresh_from_db()
    assert charge.amount_paid == D("1000.00") and Payment.objects.count() == 1


def test_nothing_is_recorded_on_a_paid_or_waived_charge() -> None:
    charge = charged()
    assert pay(charge, "1500.00").ok
    assert pay(charge, "0.01").message == rent_run.PAY_NOTHING_DUE
    other = charged()
    RentCharge.objects.filter(pk=other.pk).update(status=RentChargeStatus.WAIVED)
    assert pay(other, "1.00").message == rent_run.PAY_WAIVED


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        ("0", rent_run.PAY_NOT_POSITIVE),
        ("0.00", rent_run.PAY_NOT_POSITIVE),
        ("-5.00", rent_run.PAY_NOT_POSITIVE),
        ("NaN", rent_run.PAY_NOT_POSITIVE),
        ("Infinity", rent_run.PAY_NOT_POSITIVE),
        ("10.001", rent_run.PAY_NOT_CENTS),
    ],
)
def test_zero_negative_and_inexact_amounts_are_refused(amount: str, message: str) -> None:
    charge = charged()
    assert pay(charge, amount).message == message
    assert not Payment.objects.exists()


def test_a_float_amount_is_refused() -> None:
    charge = charged()
    outcome = rent_run.record_manual_payment(
        charge.pk,
        method=PaymentMethod.WIRE,
        amount=10.5,  # type: ignore[arg-type]
        paid_on=DAY,
        note="Wire received.",
        actor=None,
        today=DAY,
    )
    assert outcome.message == rent_run.PAY_NOT_POSITIVE


def test_a_future_date_is_refused_and_today_is_allowed() -> None:
    charge = charged()
    assert pay(charge, "1.00", paid_on=date(2026, 10, 16)).message == rent_run.PAY_FUTURE
    assert pay(charge, "1.00", paid_on=DAY).ok


@pytest.mark.parametrize("method_value", [PaymentMethod.ACH, "cash", "card", ""])
def test_only_zelle_wire_and_other_are_accepted(method_value: str) -> None:
    charge = charged()
    assert pay(charge, "1.00", method=method_value).message == rent_run.PAY_METHOD
    assert not Payment.objects.exists()


def test_a_note_with_a_card_number_is_refused_and_nothing_is_saved() -> None:
    charge = charged()
    outcome = pay(charge, "10.00", note="paid with 4111 1111 1111 1111")
    assert (outcome.ok, outcome.message) == (False, rent_run.PAY_NOTE_PAYMENT_DATA)
    charge.refresh_from_db()
    assert charge.amount_paid == D("0.00") and not Payment.objects.exists()
    assert not AuditLog.objects.filter(action="rent_charge.payment_recorded").exists()


def test_a_resubmitted_form_records_the_payment_once() -> None:
    charge = charged()
    key = uuid.uuid4()
    assert pay(charge, "100.00", creation_key=key).ok
    again = pay(charge, "100.00", creation_key=key)
    assert (again.ok, again.replayed, again.message) == (True, True, rent_run.PAY_ALREADY)
    charge.refresh_from_db()
    assert charge.amount_paid == D("100.00") and Payment.objects.count() == 1


def test_amount_paid_counts_settled_payments_only() -> None:
    charge = charged()
    Payment.objects.create(
        rent_charge=charge,
        kind="rent",
        method=PaymentMethod.ACH,
        amount=D("700.00"),
        status=PaymentStatus.RETURNED,
    )
    assert pay(charge, "1500.00").ok
    charge.refresh_from_db()
    assert (charge.amount_paid, charge.status) == (D("1500.00"), RentChargeStatus.PAID)


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_payments_cannot_exceed_the_balance() -> None:
    charge = charged()
    outcomes = _race(lambda: pay(charge, "1000.00"), lambda: pay(charge, "1000.00"))
    assert sorted(outcome.ok for outcome in outcomes) == [False, True]
    charge.refresh_from_db()
    assert charge.amount_paid == D("1000.00")
