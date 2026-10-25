"""Add payment by bank debit, the services (docs/qa/PLAN-012f-b Batch C, slice C-1, D14).

:func:`ledger.services.one_off_charges.bank_debit_options` (the card offered or greyed,
from local data only), :func:`ledger.services.one_off_charges.debit` (today through the
existing Charge now path, a later date through the single-occurrence schedule), and Cash
as a manual method (migration 0053). Nothing reaches a network: the payment provider is
a :class:`~payments.fake.FakeProvider`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    MoneyMethod,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services import one_off_charges as service
from ledger.services import rent_run
from ledger.services.one_off_charges import ChargeInput
from payments import jobs, offline_marking
from payments.fake import FakeProvider
from payments.models import Job, JobKind, JobStatus
from payments.provider import OfflinePayment, PayWithStoredMethod, ScheduleInput
from tests.job_helpers import run_queued_jobs
from tests.test_charge_now import LAST_FOUR, NICKNAME, store_methods
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator, calls, created, fresh, item
from tests.test_operator_views import client_for
from tests.test_setup_charges import fee

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


def today() -> date:
    return timezone.localdate()


def admin() -> Any:
    return client_for(Role.ADMIN)[1]


def billing() -> Any:
    return client_for(Role.BILLING)[1]


def viewer() -> Any:
    return client_for(Role.VIEWER)[1]


def an_operator_with_bank(fake: FakeProvider, **kwargs: Any) -> Operator:
    operator = an_operator(fake, **kwargs)
    store_methods(operator)
    return operator


def invoiced(fake: FakeProvider, amount: str = "100.00", **kwargs: Any) -> OneOffCharge:
    """An e-mailed arrears charge whose invoice is at the provider, as the worker leaves it."""
    operator = kwargs.pop("operator", None) or an_operator_with_bank(fake)
    charge = created(operator, item(CollectionMethod.EMAIL, amount, **kwargs))
    run_queued_jobs()
    fake.calls.clear()
    return fresh(charge)


def setup_charge(fake: FakeProvider, amount: str = "250.00") -> OneOffCharge:
    """A setup charge of a lease's checklist: collection ``none``, no invoice, no job."""
    operator = an_operator_with_bank(fake)
    lease = make_lease(operator=operator)
    charge = created(
        operator,
        ChargeInput(
            fee_type=fee("sign_fee"),
            price=D(amount),
            cost=None,
            vendor="",
            due_date=today(),
            note="",
        ),
        lease=lease,
    )
    assert charge.collection == CollectionMethod.NONE
    return fresh(charge)


def debit(charge: OneOffCharge, amount: str, on: date | None, **kwargs: Any) -> service.Outcome:
    kwargs.setdefault("actor", admin())
    kwargs.setdefault("confirmed", False)
    return service.debit(charge, D(amount), on, **kwargs)


def actions(charge: OneOffCharge) -> list[str]:
    return list(
        AuditLog.objects.filter(entity_type="one_off_charge", entity_id=str(charge.pk))
        .order_by("id")
        .values_list("action", flat=True)
    )


def written(charge: OneOffCharge) -> tuple[int, int, str, date]:
    """What a refusal must leave as it was: audit rows, jobs, collection and due date."""
    row = fresh(charge)
    return (
        AuditLog.objects.count(),
        Job.objects.count(),
        row.collection,
        row.due_date,
    )


# --- bank_debit_options ------------------------------------------------------------------


def test_options_offer_the_linked_bank_with_last_four_and_nickname_only(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake, "100.00")
    options = service.bank_debit_options(charge)
    assert options.allowed and options.reason == ""
    assert options.enabled is True
    assert options.method == service.BankMethod("checking", LAST_FOUR, NICKNAME)
    assert set(service.BankMethod.__slots__) == {"bank_type", "last_four", "nickname"}
    assert (options.balance, options.large) == (D("100.00"), False)


def test_options_without_a_provider_customer_say_no_bank_account(fake: FakeProvider) -> None:
    operator = an_operator(fake, customer=None)
    charge = created(operator, item(CollectionMethod.MANUAL, "80.00"))
    options = service.bank_debit_options(charge)
    assert options.method is None
    assert options.reason == str(service.DEBIT_NO_METHOD)
    assert options.reason.startswith("No bank account on file")


def test_options_without_a_linked_method_say_no_bank_account(fake: FakeProvider) -> None:
    charge = invoiced(fake, operator=an_operator(fake, bank=None))
    options = service.bank_debit_options(charge)
    assert (options.method, options.reason) == (None, str(service.DEBIT_NO_METHOD))


def test_options_for_a_card_method_say_no_bank_account(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    store_methods(operator, method="card")
    charge = invoiced(fake, operator=operator)
    options = service.bank_debit_options(charge)
    assert (options.method, options.reason) == (None, str(service.DEBIT_NO_METHOD))


def test_options_with_the_flag_off_say_bank_debits_are_off(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        options = service.bank_debit_options(charge)
    assert options.enabled is False
    assert options.reason == str(service.DEBIT_OFF)
    assert options.method is not None


def test_options_for_a_paid_charge_say_it_is_paid(fake: FakeProvider) -> None:
    charge = invoiced(fake, "60.00")
    outcome = service.record_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=D("60.00"),
        paid_on=today(),
        note="Paid in full by Zelle.",
        actor=billing(),
    )
    assert outcome.ok, outcome.message
    options = service.bank_debit_options(fresh(charge))
    assert (options.balance, options.reason) == (D("0.00"), service.DEBIT_PAID)


def test_options_for_a_waived_charge_say_it_is_closed(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    OneOffCharge.objects.filter(pk=charge.pk).update(
        status=OneOffChargeStatus.WAIVED, waived_at=timezone.now(), waive_note="Goodwill."
    )
    assert service.bank_debit_options(fresh(charge)).reason == service.DEBIT_CLOSED


def test_options_above_the_threshold_ask_for_the_large_debit_confirmation(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake, "900.00")
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("500.00")):
        options = service.bank_debit_options(charge)
    assert options.large is True and options.allowed


# --- debit today: the Charge now path -----------------------------------------------------


def test_a_debit_today_switches_an_emailed_charge_to_charge_now_and_pays_once(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake, "100.00")
    key = uuid.uuid4()
    outcome = debit(charge, "100.00", today(), key=key)
    assert outcome.ok, outcome.message
    row = fresh(charge)
    assert row.collection == CollectionMethod.CHARGE_NOW
    assert row.charge_now_key is not None
    assert Job.objects.filter(kind=JobKind.INVOICE_PAY, entity_id=charge.pk).count() == 1
    assert service.DEBIT_NOW_ACTION in actions(charge)
    assert service.CHARGE_NOW_REQUESTED_ACTION in actions(charge)
    run_queued_jobs()
    [(_, request)] = calls(fake, "pay_invoice")
    assert isinstance(request, PayWithStoredMethod)
    assert request.idempotency_id == str(row.charge_now_key)
    assert fresh(charge).charge_now_key == row.charge_now_key
    # Rule 2: the payment is the provider's pending one, never a local status write.
    assert not Payment.objects.filter(one_off_charge=charge, status=PaymentStatus.SETTLED).exists()


def test_a_replayed_debit_today_writes_once(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    key = uuid.uuid4()
    assert debit(charge, "100.00", today(), key=key).ok
    count = AuditLog.objects.count()
    again = debit(charge, "100.00", today(), key=key)
    assert (again.ok, again.message) == (True, service.DEBIT_ALREADY)
    assert AuditLog.objects.count() == count
    assert Job.objects.filter(kind=JobKind.INVOICE_PAY, entity_id=charge.pk).count() == 1


def test_a_partial_debit_today_is_refused_and_writes_nothing(fake: FakeProvider) -> None:
    charge = invoiced(fake, "100.00")
    before = written(charge)
    outcome = debit(charge, "40.00", today())
    assert (outcome.ok, outcome.field) == (False, "amount")
    assert outcome.message == service.DEBIT_WHOLE_BALANCE.format(amount="$100.00")
    assert written(charge) == before


def test_an_amount_above_the_balance_is_refused(fake: FakeProvider) -> None:
    charge = invoiced(fake, "100.00")
    before = written(charge)
    outcome = debit(charge, "100.01", today())
    assert (outcome.ok, outcome.message) == (False, service.DEBIT_OVER_BALANCE)
    assert written(charge) == before


@pytest.mark.parametrize("amount", ["0.00", "-5.00", "10.005"])
def test_an_amount_not_positive_or_not_in_cents_is_refused(fake: FakeProvider, amount: str) -> None:
    charge = invoiced(fake)
    before = written(charge)
    outcome = debit(charge, amount, today())
    assert (outcome.ok, outcome.field) == (False, "amount")
    assert written(charge) == before


def test_a_past_date_is_refused(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    before = written(charge)
    outcome = debit(charge, "100.00", today() - timedelta(days=1))
    assert (outcome.ok, outcome.message) == (False, service.DEBIT_DATE_PAST)
    assert written(charge) == before


def test_billing_cannot_debit_today(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    before = written(charge)
    outcome = debit(charge, "100.00", today(), actor=billing())
    assert (outcome.ok, outcome.message) == (False, service.CHARGE_NOW_ADMIN)
    assert written(charge) == before


def test_a_charge_now_refusal_rolls_the_collection_switch_back(fake: FakeProvider) -> None:
    # The invoice is not at the provider yet: Charge now refuses, nothing stays written.
    charge = created(an_operator_with_bank(fake), item(CollectionMethod.EMAIL, "100.00"))
    before = written(charge)
    outcome = debit(charge, "100.00", today())
    assert (outcome.ok, outcome.message) == (False, str(service.CHARGE_NOW_NOT_LINKED))
    assert written(charge) == before
    assert fresh(charge).charge_now_key is None


def test_a_large_debit_needs_the_confirmation_and_is_audited_with_it(fake: FakeProvider) -> None:
    charge = invoiced(fake, "900.00")
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("500.00")):
        before = written(charge)
        refused = debit(charge, "900.00", today())
        assert (refused.ok, refused.field) == (False, "confirm")
        assert written(charge) == before
        outcome = debit(charge, "900.00", today(), confirmed=True)
    assert outcome.ok, outcome.message
    [row] = AuditLog.objects.filter(entity_id=str(charge.pk), action=service.LARGE_DEBIT_ACTION)
    assert row.after["amount"] == "900.00"


# --- debit on a later date: the single-occurrence schedule --------------------------------


def test_a_later_date_makes_the_setup_charge_autopay_and_schedules_one_debit(
    fake: FakeProvider,
) -> None:
    charge = setup_charge(fake, "250.00")
    on = today() + timedelta(days=20)
    outcome = debit(charge, "250.00", on, actor=billing(), key=uuid.uuid4())
    assert outcome.ok, outcome.message
    row = fresh(charge)
    assert (row.collection, row.due_date) == (CollectionMethod.AUTOPAY_DUE, on)
    assert outcome.job is not None and outcome.job.kind == JobKind.SCHEDULE_CREATE
    [audit] = AuditLog.objects.filter(
        entity_id=str(charge.pk), action=service.DEBIT_SCHEDULED_ACTION
    )
    assert audit.before == {"collection": "none", "due_date": today().isoformat()}
    assert audit.after["due_date"] == on.isoformat()
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE, job.result
    [(schedule,)] = calls(fake, "create_scheduled_invoice")
    assert isinstance(schedule, ScheduleInput)
    assert schedule.rule == f"DTSTART={on:%Y%m%d}T120000Z;FREQ=DAILY;COUNT=1"
    assert schedule.amounts.total == D("250.00")
    assert not calls(fake, "pay_invoice")


def test_a_replayed_later_date_writes_once(fake: FakeProvider) -> None:
    charge = setup_charge(fake)
    on = today() + timedelta(days=5)
    key = uuid.uuid4()
    assert debit(charge, "250.00", on, actor=billing(), key=key).ok
    before = written(charge)
    again = debit(charge, "250.00", on, actor=billing(), key=key)
    assert (again.ok, again.message) == (True, service.DEBIT_ALREADY)
    assert written(charge) == before
    assert Job.objects.filter(kind=JobKind.SCHEDULE_CREATE, entity_id=charge.pk).count() == 1


def test_a_later_date_on_a_charge_with_an_invoice_is_refused(fake: FakeProvider) -> None:
    # A schedule would make a second invoice beside the e-mailed one: billed twice.
    charge = invoiced(fake)
    before = written(charge)
    outcome = debit(charge, "100.00", today() + timedelta(days=3), actor=billing())
    assert (outcome.ok, outcome.message) == (False, service.DEBIT_HAS_INVOICE)
    assert written(charge) == before


def test_a_second_schedule_is_refused(fake: FakeProvider) -> None:
    charge = setup_charge(fake)
    assert debit(charge, "250.00", today() + timedelta(days=3), actor=billing()).ok
    before = written(charge)
    outcome = debit(charge, "250.00", today() + timedelta(days=4), actor=billing())
    assert (outcome.ok, outcome.message) == (False, service.DEBIT_ALREADY_SCHEDULED)
    assert written(charge) == before


def test_a_date_beyond_the_horizon_and_a_viewer_are_refused(fake: FakeProvider) -> None:
    charge = setup_charge(fake)
    before = written(charge)
    far = debit(charge, "250.00", today() + timedelta(days=366), actor=billing())
    assert (far.ok, far.field) == (False, "on")
    looker = debit(charge, "250.00", today() + timedelta(days=3), actor=viewer())
    assert (looker.ok, looker.message) == (False, service.DEBIT_SCHEDULE_ROLE)
    assert written(charge) == before


def test_a_later_line_of_a_builder_invoice_is_not_debited_from_here(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    charge = invoiced(fake)
    monkeypatch.setattr(service, "is_later_line", lambda _charge: True)
    before = written(charge)
    outcome = debit(charge, "100.00", today())
    assert (outcome.ok, outcome.message) == (False, service.DEBIT_NOT_ELIGIBLE)
    assert written(charge) == before


# --- Cash (D14, migration 0053) -----------------------------------------------------------


def test_cash_is_a_manual_method_between_check_and_other() -> None:
    assert (PaymentMethod.CASH.value, PaymentMethod.CASH.label) == ("cash", "Cash")
    assert (MoneyMethod.CASH.value, MoneyMethod.CASH.label) == ("cash", "Cash")
    assert rent_run.MANUAL_METHODS == ("zelle", "wire", "check", "cash", "other")
    assert offline_marking.memo("cash", date(2026, 10, 2), "At the counter.") == (
        "Paid by cash on 10/02/2026. At the counter."
    )


def test_cash_records_a_manual_payment_and_marks_the_provider_invoice(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake, "75.00")
    outcome = service.record_payment(
        charge.pk,
        method=PaymentMethod.CASH,
        amount=D("75.00"),
        paid_on=today(),
        note="Cash handed in at the office.",
        actor=billing(),
    )
    assert outcome.ok, outcome.message
    assert outcome.payment is not None and outcome.mark_due is True
    assert (outcome.payment.method, outcome.payment.kind) == ("cash", PaymentKind.ONE_OFF)
    jobs.enqueue(JobKind.ONE_OFF_PAYMENT_MARK, charge, {"payment": outcome.payment.pk}, None)
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE, job.result
    [(_, offline)] = calls(fake, "mark_invoice_paid_cash_or_check")
    assert isinstance(offline, OfflinePayment)
    assert offline.method == "cash"
    assert offline.note.startswith("Paid by cash on ")
    assert fresh(charge).status == OneOffChargeStatus.PAID


def _migrate(target: tuple[str, str]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])


def _payment_check() -> str:
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, "payments")
    assert "payments_method_valid" in constraints
    if connection.vendor == "sqlite":
        cursor = connection.cursor()
        cursor.execute("SELECT sql FROM sqlite_master WHERE name = 'payments'")
        return str(cursor.fetchone()[0])
    cursor = connection.cursor()
    cursor.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
        ["payments_method_valid"],
    )
    return str(cursor.fetchone()[0])


@pytest.mark.django_db(transaction=True)
def test_migration_0053_adds_cash_to_the_method_check_and_reverses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        _migrate(("ledger", "0052_operator_kind"))
        assert "'cash'" not in _payment_check()
        _migrate(("ledger", "0053_cash_method"))
        assert "'cash'" in _payment_check()
        _migrate(("ledger", "0052_operator_kind"))
        assert "'cash'" not in _payment_check()
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaves)
    assert "'cash'" in _payment_check()
