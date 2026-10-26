"""The rent-charge bank debit (docs/qa/PLAN-012f-b §10 D19, slice C2): the schema, the
readers of the second re-debit source and the availability rule.

Migration 0056: ``one_off_charges.source_rent_charge``, the CHECK
``one_off_charges_redebit_one_source`` and the partial unique index
``one_off_charges_one_open_debit_per_rent_charge``. The readers: what a rent charge's
bank debit collects is paid on the rent charge
(:func:`ledger.services.rent_run.redebit_collected`, the chain walk
:func:`ledger.services.one_off_charges.redebit_sources`), owed once in the Unpaid
balances (:func:`ledger.services.receivables.carried_by_redebits`) and counted as rent
collected in the P&L. The availability:
:func:`ledger.services.one_off_charges.rent_debit_options` in D19's order, from local
data only. Nothing reaches a network: the payment provider is
a :class:`~payments.fake.FakeProvider`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from accounts.roles import Role
from finance.dates import day_text
from ledger.models import (
    ZERO,
    AuditLog,
    CollectionMethod,
    InstallmentPlan,
    Invoice,
    InvoiceStatus,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import (
    calendar,
    installments,
    leases,
    nsf,
    pnl,
    receivables,
    rent_run,
    workspace,
)
from ledger.services import one_off_charges as service
from payments.errors import ProviderRejected
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind, ProviderSnapshot
from payments.provider import PayWithStoredMethod, ScheduleInput
from tests.job_helpers import run_queued_jobs
from tests.test_charge_now import LAST_FOUR, NICKNAME, store_methods
from tests.test_lease_services import BANK as LEASE_BANK
from tests.test_lease_services import linked_lease, make_lease
from tests.test_one_off_charges import actions, an_operator, calls
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
MONTH = date(2026, 9, 1)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


# --- Builders ----------------------------------------------------------------------------


def today() -> date:
    return timezone.localdate()


def an_operator_with_bank(fake: FakeProvider) -> Operator:
    operator = an_operator(fake)
    store_methods(operator)
    return operator


def a_rent(
    fake: FakeProvider,
    amount: str = "1500.00",
    *,
    status: str = RentChargeStatus.DUE,
    bank: bool = True,
    operator: Operator | None = None,
    **fields: Any,
) -> RentCharge:
    """A rent charge of a lease whose operator has (by default) a usable linked bank method."""
    owner = operator or (an_operator_with_bank(fake) if bank else an_operator(fake, bank=None))
    lease = make_lease(operator=owner)
    return RentCharge.objects.create(
        lease=lease,
        period_month=MONTH,
        seq=1,
        due_on=MONTH.replace(day=5),
        amount_due=D(amount),
        status=status,
        **fields,
    )


def a_payment(rent: RentCharge, status: str, amount: str | None = None, **fields: Any) -> Payment:
    return Payment.objects.create(
        rent_charge=rent,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D(amount) if amount is not None else rent.amount_due,
        status=status,
        initiated_on=rent.due_on,
        **fields,
    )


def a_debit(
    rent: RentCharge,
    *,
    collection: str = CollectionMethod.AUTOPAY_DUE,
    status: str = OneOffChargeStatus.OWED,
    **fields: Any,
) -> OneOffCharge:
    """A bank debit of the rent charge as D19 makes it (the action is not built here)."""
    lease = rent.lease
    values: dict[str, Any] = {
        "operator": lease.operator,
        "lease": lease,
        "category": OneOffCategory.REDEBIT,
        "collection": collection,
        "description": "Bank debit of Rent Sep 2026",
        "price": rent.balance,
        "due_date": today() + timedelta(days=3),
        "status": status,
        "source_rent_charge": rent,
    }
    if status == OneOffChargeStatus.VOIDED:
        values |= {"voided_at": timezone.now(), "void_note": "The debit failed."}
    return OneOffCharge.objects.create(**{**values, **fields})


def a_redebit_of(
    payment: Payment, *, collection: str = CollectionMethod.AUTOPAY_DUE
) -> OneOffCharge:
    """A re-debit of a returned rent payment, as the returns queue's Reschedule makes it."""
    rent = payment.rent_charge
    assert rent is not None
    return OneOffCharge.objects.create(
        operator=rent.lease.operator,
        lease=rent.lease,
        category=OneOffCategory.REDEBIT,
        collection=collection,
        description=f"{nsf.REDEBIT_PREFIX}Rent 2026-09",
        price=payment.amount,
        due_date=today() + timedelta(days=3),
        status=OneOffChargeStatus.OWED,
        source_payment=payment,
    )


def a_plan(rent: RentCharge, status: str = "active") -> InstallmentPlan:
    operator = rent.lease.operator
    assert operator is not None
    return InstallmentPlan.objects.create(
        rent_charge=rent,
        operator=operator,
        lease=rent.lease,
        count=2,
        first_due=today() + timedelta(days=10),
        cadence="monthly",
        collection="autopay_due",
        status=status,
        confirmed_at=timezone.now(),
    )


def fresh_rent(rent: RentCharge) -> RentCharge:
    return RentCharge.objects.get(pk=rent.pk)


def reason(rent: RentCharge) -> str:
    return service.rent_debit_options(fresh_rent(rent)).reason


# --- The schema (migration 0056) ---------------------------------------------------------


def _saved_fails(charge: OneOffCharge) -> bool:
    try:
        with transaction.atomic():
            charge.save()
    except IntegrityError:
        return True
    return False


def test_a_rent_charge_bank_debit_is_a_redebit_with_the_rent_charge_alone(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake)
    debit = a_debit(rent)
    assert debit.source_payment_id is None and debit.source_rent_charge_id == rent.pk
    assert list(rent.bank_debits.all()) == [debit]


def test_the_check_refuses_a_redebit_with_both_sources(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    returned = a_payment(rent, PaymentStatus.RETURNED)
    charge = OneOffCharge(
        operator=rent.lease.operator,
        lease=rent.lease,
        category=OneOffCategory.REDEBIT,
        collection=CollectionMethod.AUTOPAY_DUE,
        description="Bank debit of Rent Sep 2026",
        price=D("10.00"),
        due_date=today(),
        source_payment=returned,
        source_rent_charge=rent,
    )
    assert _saved_fails(charge)
    assert not OneOffCharge.objects.exists()


def test_the_check_refuses_a_redebit_with_no_source(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    charge = OneOffCharge(
        operator=rent.lease.operator,
        category=OneOffCategory.REDEBIT,
        collection=CollectionMethod.AUTOPAY_DUE,
        description="Bank debit",
        price=D("10.00"),
        due_date=today(),
    )
    assert _saved_fails(charge)


def test_the_check_refuses_a_rent_source_on_another_category(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    charge = OneOffCharge(
        operator=rent.lease.operator,
        category=OneOffCategory.ARREARS,
        collection=CollectionMethod.EMAIL,
        description="Arrears",
        price=D("10.00"),
        due_date=today(),
        source_rent_charge=rent,
    )
    assert _saved_fails(charge)


@pytest.mark.parametrize("first", [OneOffChargeStatus.OWED, OneOffChargeStatus.PARTIAL])
def test_the_unique_index_refuses_a_second_open_debit(fake: FakeProvider, first: str) -> None:
    rent = a_rent(fake)
    paid = D("1.00") if first == OneOffChargeStatus.PARTIAL else ZERO
    a_debit(rent, status=first, amount_paid=paid)
    second = OneOffCharge(
        operator=rent.lease.operator,
        lease=rent.lease,
        category=OneOffCategory.REDEBIT,
        collection=CollectionMethod.AUTOPAY_DUE,
        description="Bank debit of Rent Sep 2026",
        price=D("1500.00"),
        due_date=today(),
        source_rent_charge=rent,
    )
    assert _saved_fails(second)
    assert rent.bank_debits.count() == 1


@pytest.mark.parametrize("first", [OneOffChargeStatus.PAID, OneOffChargeStatus.VOIDED])
def test_a_closed_debit_leaves_room_for_a_new_one(fake: FakeProvider, first: str) -> None:
    rent = a_rent(fake)
    paid = rent.balance if first == OneOffChargeStatus.PAID else ZERO
    a_debit(rent, status=first, amount_paid=paid)
    a_debit(rent)
    assert rent.bank_debits.count() == 2


def _migrate(target: tuple[str, str]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])


def _one_off_schema() -> tuple[set[str], set[str]]:
    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(cursor, "one_off_charges")
        }
        constraints = set(connection.introspection.get_constraints(cursor, "one_off_charges"))
    return columns, constraints


NEW = {"one_off_charges_redebit_one_source", "one_off_charges_one_open_debit_per_rent_charge"}
OLD = "one_off_charges_source_payment_iff_redebit"


@pytest.mark.django_db(transaction=True)
def test_migration_0056_adds_the_source_and_reverses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        _migrate(("ledger", "0055_installment_plan_paused"))
        columns, constraints = _one_off_schema()
        assert "source_rent_charge_id" not in columns
        assert OLD in constraints and not NEW & constraints
        _migrate(("ledger", "0056_rent_charge_bank_debit"))
        columns, constraints = _one_off_schema()
        assert "source_rent_charge_id" in columns
        assert NEW <= constraints and OLD not in constraints
        _migrate(("ledger", "0055_installment_plan_paused"))
        columns, constraints = _one_off_schema()
        assert "source_rent_charge_id" not in columns
        assert OLD in constraints and not NEW & constraints
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaves)
    columns, constraints = _one_off_schema()
    assert "source_rent_charge_id" in columns and NEW <= constraints


def test_migration_0056_adds_no_guarded_column() -> None:
    from importlib import import_module

    module = import_module("ledger.migrations.0056_rent_charge_bank_debit")
    assert module.GUARDED_COLUMNS == {}


# --- The readers of the second source ------------------------------------------------------


def _pay_the_debit(debit: OneOffCharge, amount: str) -> None:
    outcome = service.record_payment(
        debit.pk,
        method=PaymentMethod.ZELLE,
        amount=D(amount),
        paid_on=today(),
        note="Paid by Zelle.",
        actor=client_for(Role.BILLING)[1],
    )
    assert outcome.ok, outcome.message


def test_what_the_debit_collects_is_paid_on_the_rent_charge(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    debit = a_debit(rent)
    assert rent_run.redebit_collected(rent_charge=rent) == ZERO
    _pay_the_debit(debit, "600.00")
    assert rent_run.redebit_collected(rent_charge=rent) == D("600.00")
    assert (fresh_rent(rent).amount_paid, fresh_rent(rent).status) == (
        D("600.00"),
        RentChargeStatus.PARTIAL,
    )
    _pay_the_debit(debit, "900.00")
    # The settlement of the debit marks the rent charge paid (D19).
    assert (fresh_rent(rent).amount_paid, fresh_rent(rent).status) == (
        D("1500.00"),
        RentChargeStatus.PAID,
    )
    assert rent_run.own_paid(fresh_rent(rent)) == D("1500.00")


def test_the_chain_walk_ends_at_the_rent_charge(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    debit = a_debit(rent)
    with transaction.atomic():
        [(source, state)] = service.redebit_sources(debit)
    assert isinstance(source, RentCharge) and source.pk == rent.pk
    assert state == rent_run.charge_state(fresh_rent(rent))


def test_a_redebit_of_a_returned_debit_payment_walks_to_the_rent_charge(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake)
    debit = a_debit(rent)
    returned = Payment.objects.create(
        one_off_charge=debit,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=debit.price,
        status=PaymentStatus.RETURNED,
        initiated_on=today(),
    )
    again = OneOffCharge.objects.create(
        operator=debit.operator,
        lease=debit.lease,
        category=OneOffCategory.REDEBIT,
        collection=CollectionMethod.AUTOPAY_DUE,
        description="Re-debit of Bank debit of Rent Sep 2026",
        price=debit.price,
        due_date=today(),
        source_payment=returned,
    )
    with transaction.atomic():
        found = [source.pk for source, _ in service.redebit_sources(again)]
    assert found == [debit.pk, rent.pk]


def test_the_unpaid_balances_carry_the_amount_once(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    operator = rent.lease.operator
    assert operator is not None
    before = receivables.open_balances(today(), operator=operator.pk)
    assert receivables.total(before) == D("1500.00")
    debit = a_debit(rent)
    after = receivables.open_balances(today(), operator=operator.pk)
    # The rent charge is carried by its debit: 1500, not 3000.
    assert receivables.total(after) == D("1500.00")
    assert [(row.kind, row.charge_id) for row in after] == [(receivables.KIND_ONE_OFF, debit.pk)]
    carried, _ = receivables.carried_by_redebits(operator.pk)
    assert carried == {(receivables.KIND_RENT, rent.pk): D("1500.00")}
    _pay_the_debit(debit, "1500.00")
    assert receivables.open_balances(today(), operator=operator.pk) == []


def test_a_voided_debit_carries_nothing(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    a_debit(rent, status=OneOffChargeStatus.VOIDED)
    carried, _ = receivables.carried_by_redebits()
    assert carried == {}


def test_the_pnl_counts_the_debit_as_rent_of_the_rent_charge(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    debit = a_debit(rent)
    settled_on = date(2026, 10, 20)
    Payment.objects.create(
        one_off_charge=debit,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("1500.00"),
        status=PaymentStatus.SETTLED,
        settled_on=settled_on,
        initiated_on=settled_on,
    )
    september = pnl.total(pnl.rows(pnl.PnlQuery(MONTH, MONTH)))
    october = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 10, 1), date(2026, 10, 1))))
    # Billed once, as the rent charge's month's rent; the debit is never billed.
    assert (september.rent_billed, september.fees_billed) == (D("1500.00"), ZERO)
    assert (october.rent_billed, october.fees_billed) == (ZERO, ZERO)
    # Collected as rent, in the month the debit settled, like any rent payment.
    assert (october.rent_collected, october.fees_collected) == (D("1500.00"), ZERO)


def test_the_pnl_counts_a_redebit_of_a_returned_debit_payment_as_rent(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    debit = a_debit(rent)
    returned = Payment.objects.create(
        one_off_charge=debit,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("1500.00"),
        status=PaymentStatus.RETURNED,
        initiated_on=today(),
    )
    again = OneOffCharge.objects.create(
        operator=debit.operator,
        lease=debit.lease,
        category=OneOffCategory.REDEBIT,
        collection=CollectionMethod.AUTOPAY_DUE,
        description="Re-debit of Bank debit of Rent Sep 2026",
        price=D("1500.00"),
        due_date=today(),
        source_payment=returned,
    )
    settled_on = date(2026, 11, 4)
    Payment.objects.create(
        one_off_charge=again,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("1500.00"),
        status=PaymentStatus.SETTLED,
        settled_on=settled_on,
        initiated_on=settled_on,
    )
    november = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 11, 1), date(2026, 11, 1))))
    assert (november.rent_collected, november.fees_collected) == (D("1500.00"), ZERO)


# --- Availability (rent_debit_options) -----------------------------------------------------


def test_never_debited_the_card_is_offered_for_the_whole_balance(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00", amount_paid=D("200.00"), status=RentChargeStatus.PARTIAL)
    options = service.rent_debit_options(fresh_rent(rent))
    assert options.allowed and options.reason == ""
    assert options.balance == D("1300.00") and options.enabled
    assert options.method is not None
    assert (options.method.last_four, options.method.nickname) == (LAST_FOUR, NICKNAME)
    assert options.large is False


def test_a_charge_collected_by_email_is_offered(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    operator = rent.lease.operator
    assert operator is not None
    Invoice.objects.create(
        invoice_number="INV-2026-09001",
        operator=operator,
        rent_charge=rent,
        issue_date=today(),
        due_date=today(),
        memo="Rent Sep 2026",
        subtotal=rent.amount_due,
        tax=ZERO,
        fees=ZERO,
        surcharge=ZERO,
        total=rent.amount_due,
        internal_status=InvoiceStatus.SENT,
        allow_card=False,
        allow_ach=True,
        legacy=False,
    )
    assert reason(rent) == ""


def test_last_debit_returned_with_no_redebit_is_offered(fake: FakeProvider) -> None:
    rent = a_rent(fake, status=RentChargeStatus.UNPAID)
    a_payment(rent, PaymentStatus.RETURNED, returned_at=timezone.now(), return_code="R01")
    assert reason(rent) == ""


def test_last_debit_failed_and_cancelled_is_offered(fake: FakeProvider) -> None:
    rent = a_rent(fake, status=RentChargeStatus.UNPAID)
    a_debit(rent, collection=CollectionMethod.CHARGE_NOW, status=OneOffChargeStatus.VOIDED)
    assert reason(rent) == ""


def test_a_pending_charge_is_greyed(fake: FakeProvider) -> None:
    rent = a_rent(fake, status=RentChargeStatus.PENDING)
    assert reason(rent) == service.RENT_DEBIT_PENDING == "A debit is already pending."


def test_a_pending_payment_greys_the_card(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    a_payment(rent, PaymentStatus.PENDING, "500.00")
    assert reason(rent) == service.RENT_DEBIT_PENDING


def test_an_open_debit_sourced_by_the_charge_greys_the_card(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    a_debit(rent)
    assert reason(rent) == service.RENT_DEBIT_PENDING


@pytest.mark.parametrize("collection", [CollectionMethod.CHARGE_NOW, CollectionMethod.EMAIL])
def test_any_open_debit_sourced_by_the_charge_greys_the_card(
    fake: FakeProvider, collection: str
) -> None:
    # Fable 2026-10-26: whatever its collection or payments (a refused Charge now leaves it
    # owed); staff cancel it from its page to offer the card again.
    rent = a_rent(fake)
    a_debit(rent, collection=collection)
    assert reason(rent) == service.RENT_DEBIT_PENDING == "A debit is already pending."


def test_a_charge_on_a_live_plan_is_collected_by_the_plan(fake: FakeProvider) -> None:
    rent = a_rent(fake, status=RentChargeStatus.UNPAID)
    a_plan(rent)
    assert reason(rent) == service.DEBIT_ON_PLAN
    assert service.DEBIT_ON_PLAN == "This charge is collected by its installment plan."


def test_an_open_debit_with_a_pending_payment_greys_the_card(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    debit = a_debit(rent, collection=CollectionMethod.CHARGE_NOW)
    Payment.objects.create(
        one_off_charge=debit,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=debit.price,
        status=PaymentStatus.PENDING,
        initiated_on=today(),
    )
    assert reason(rent) == service.RENT_DEBIT_PENDING


def test_an_open_redebit_of_a_returned_payment_greys_the_card(fake: FakeProvider) -> None:
    rent = a_rent(fake, status=RentChargeStatus.UNPAID)
    returned = a_payment(rent, PaymentStatus.RETURNED, returned_at=timezone.now())
    a_redebit_of(returned)
    assert reason(rent) == service.RENT_DEBIT_PENDING


def test_an_emailed_redebit_with_no_pending_payment_leaves_the_card_offered(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake, status=RentChargeStatus.UNPAID)
    returned = a_payment(rent, PaymentStatus.RETURNED, returned_at=timezone.now())
    a_redebit_of(returned, collection=CollectionMethod.EMAIL)
    assert reason(rent) == ""


def test_a_paid_redebit_of_a_returned_payment_does_not_grey_the_card(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00", amount_paid=D("500.00"), status=RentChargeStatus.PARTIAL)
    returned = a_payment(rent, PaymentStatus.RETURNED, "500.00", returned_at=timezone.now())
    redebit = a_redebit_of(returned)
    OneOffCharge.objects.filter(pk=redebit.pk).update(
        status=OneOffChargeStatus.PAID, amount_paid=D("500.00")
    )
    assert reason(rent) == ""


def test_a_waived_charge_is_closed(fake: FakeProvider) -> None:
    rent = a_rent(fake, status=RentChargeStatus.WAIVED)
    assert reason(rent) == service.DEBIT_CLOSED


def test_a_paid_charge_is_paid(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00", amount_paid=D("1500.00"), status=RentChargeStatus.PAID)
    options = service.rent_debit_options(fresh_rent(rent))
    assert (options.reason, options.balance) == (service.DEBIT_PAID, ZERO)


def test_nothing_owed_reads_paid(fake: FakeProvider) -> None:
    rent = a_rent(fake, "0.00")
    assert reason(rent) == service.DEBIT_PAID


def test_the_flag_off_greys_the_card(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        options = service.rent_debit_options(fresh_rent(rent))
    assert (options.reason, options.enabled) == (str(service.DEBIT_OFF), False)


def test_no_bank_method_greys_the_card(fake: FakeProvider) -> None:
    rent = a_rent(fake, bank=False)
    options = service.rent_debit_options(fresh_rent(rent))
    assert (options.reason, options.method) == (str(service.DEBIT_NO_METHOD), None)


def test_the_reasons_come_in_the_order_of_d19(fake: FakeProvider) -> None:
    # Waived beats everything; paid beats a live debit; a live debit beats the flag; the
    # flag beats the bank method.
    owner = an_operator(fake, bank=None)
    waived = a_rent(fake, status=RentChargeStatus.WAIVED, operator=owner)
    a_payment(waived, PaymentStatus.PENDING)
    a_plan(waived)
    paid = a_rent(
        fake, "10.00", amount_paid=D("10.00"), status=RentChargeStatus.PAID, operator=owner
    )
    a_payment(paid, PaymentStatus.PENDING)
    on_plan = a_rent(fake, status=RentChargeStatus.PENDING, operator=owner)
    a_plan(on_plan)
    live = a_rent(fake, status=RentChargeStatus.PENDING, operator=owner)
    off = a_rent(fake, operator=owner)
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        assert reason(waived) == service.DEBIT_CLOSED
        assert reason(paid) == service.DEBIT_PAID
        assert reason(on_plan) == service.DEBIT_ON_PLAN
        assert reason(live) == service.RENT_DEBIT_PENDING
        assert reason(off) == str(service.DEBIT_OFF)


def test_the_large_debit_rule_follows_the_balance(fake: FakeProvider) -> None:
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("1000.00")):
        rent = a_rent(fake, "1500.00")
        assert service.rent_debit_options(fresh_rent(rent)).large is True


def test_availability_writes_nothing_and_calls_no_provider(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    fake.calls.clear()
    before = (OneOffCharge.objects.count(), Payment.objects.count(), fresh_rent(rent).updated_at)
    service.rent_debit_options(fresh_rent(rent))
    assert (
        OneOffCharge.objects.count(),
        Payment.objects.count(),
        fresh_rent(rent).updated_at,
    ) == before
    assert fake.calls == []


# --- The action (rent_debit) ---------------------------------------------------------------


def admin() -> Any:
    return client_for(Role.ADMIN)[1]


def billing() -> Any:
    return client_for(Role.BILLING)[1]


def rent_debit(rent: RentCharge, on: date | None, **kwargs: Any) -> service.Outcome:
    kwargs.setdefault("actor", admin())
    kwargs.setdefault("confirmed", False)
    kwargs.setdefault("key", uuid.uuid4())
    return service.rent_debit(fresh_rent(rent), on, **kwargs)


def written() -> tuple[int, int, int, int]:
    """What a refusal must leave as it was: charges, invoices, jobs, audit rows."""
    return (
        OneOffCharge.objects.count(),
        Invoice.objects.count(),
        Job.objects.count(),
        AuditLog.objects.count(),
    )


def started(rent: RentCharge) -> list[AuditLog]:
    return list(
        AuditLog.objects.filter(
            action=service.RENT_DEBIT_ACTION, entity_type="rent_charge", entity_id=str(rent.pk)
        )
    )


def test_today_needs_an_admin(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    before = written()
    outcome = rent_debit(rent, today(), actor=billing())
    assert (outcome.ok, outcome.message) == (False, service.CHARGE_NOW_ADMIN)
    assert written() == before


def test_today_creates_the_debit_its_invoice_and_one_create_job(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00", amount_paid=D("200.00"), status=RentChargeStatus.PARTIAL)
    actor = admin()
    key = uuid.uuid4()
    outcome = rent_debit(rent, today(), actor=actor, key=key)
    assert (outcome.ok, outcome.message) == (True, service.CHARGE_NOW_REQUESTED)
    [debit] = OneOffCharge.objects.all()
    assert debit.source_rent_charge_id == rent.pk and debit.source_payment_id is None
    assert debit.category == OneOffCategory.REDEBIT
    assert debit.collection == CollectionMethod.CHARGE_NOW
    assert (debit.price, debit.due_date, debit.status) == (D("1300.00"), today(), "owed")
    assert debit.description == "Bank debit of Rent Sep 2026"
    assert (debit.lease_id, debit.operator_id) == (rent.lease_id, rent.lease.operator_id)
    assert debit.charge_now_key == key and debit.needs_review is False
    [invoice] = Invoice.objects.all()
    assert (invoice.one_off_charge_id, invoice.total, invoice.due_date) == (
        debit.pk,
        D("1300.00"),
        today(),
    )
    assert invoice.allow_card is False and invoice.allow_ach is True
    [job] = Job.objects.all()
    assert job.kind == JobKind.INVOICE_CREATE and job.entity_id == debit.pk
    assert job.params == {service.CHARGE_NOW_PARAM: True, "confirmed": False}
    [audit] = started(rent)
    assert audit.actor_id == actor.pk
    assert audit.after == {
        "amount": "1300.00",
        "on": today().isoformat(),
        "charge": str(debit.pk),
        "debit_key": str(key),
        "note": "",
    }
    # The rent charge is unchanged until the debit's payment settles (Rule 2).
    assert (fresh_rent(rent).amount_paid, fresh_rent(rent).status) == (D("200.00"), "partial")


def test_today_makes_one_provider_attempt(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    key = uuid.uuid4()
    assert rent_debit(rent, today(), key=key).ok
    run_queued_jobs()
    [(_, request)] = calls(fake, "pay_invoice")
    assert isinstance(request, PayWithStoredMethod) and request.idempotency_id == str(key)
    debit = OneOffCharge.objects.get()
    assert [job.kind for job in Job.objects.order_by("created_at")] == [
        JobKind.INVOICE_CREATE,
        JobKind.INVOICE_PAY,
    ]
    assert service.CHARGED_ACTION in actions(debit)
    # The provider's payment is pending: the card stays greyed.
    assert reason(rent) == service.RENT_DEBIT_PENDING


def test_a_replayed_key_writes_nothing(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    key = uuid.uuid4()
    assert rent_debit(rent, today(), key=key).ok
    before = written()
    again = rent_debit(rent, today(), key=key)
    assert (again.ok, again.message) == (True, service.DEBIT_ALREADY)
    assert written() == before


def test_a_second_debit_is_refused_while_one_is_open(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    assert rent_debit(rent, today()).ok
    before = written()
    again = rent_debit(rent, today() + timedelta(days=3), actor=billing())
    assert (again.ok, again.message) == (False, service.RENT_DEBIT_PENDING)
    assert written() == before


def test_a_later_date_schedules_the_debit(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    on = today() + timedelta(days=5)
    actor = billing()
    outcome = rent_debit(rent, on, actor=actor)
    assert outcome.ok, outcome.message
    debit = OneOffCharge.objects.get()
    assert (debit.collection, debit.due_date, debit.price) == (
        CollectionMethod.AUTOPAY_DUE,
        on,
        D("1500.00"),
    )
    assert not Invoice.objects.exists()
    [job] = Job.objects.all()
    assert (job.kind, job.entity_id) == (JobKind.SCHEDULE_CREATE, debit.pk)
    assert service.DEBIT_SCHEDULED_ACTION in actions(debit)
    [audit] = started(rent)
    assert audit.actor_id == actor.pk and audit.after is not None
    assert audit.after["on"] == on.isoformat()
    # The balance is unchanged until settlement.
    assert fresh_rent(rent).balance == D("1500.00")
    run_queued_jobs()
    [(schedule,)] = calls(fake, "create_scheduled_invoice")
    assert isinstance(schedule, ScheduleInput)


def test_a_viewer_cannot_schedule(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    before = written()
    outcome = rent_debit(rent, today() + timedelta(days=3), actor=client_for(Role.VIEWER)[1])
    assert (outcome.ok, outcome.message) == (False, service.DEBIT_SCHEDULE_ROLE)
    assert written() == before


REFUSALS = {
    "past": lambda: service.DEBIT_DATE_PAST,
    "card": lambda: service.TEXT_PAYMENT_DATA,
    "waived": lambda: service.DEBIT_CLOSED,
    "no bank": lambda: str(service.DEBIT_NO_METHOD),
    "flag off": lambda: str(service.DEBIT_OFF),
    "ended": lambda: service.LEASE_ENDED,
}


@pytest.mark.parametrize("case", list(REFUSALS))
def test_a_refusal_leaves_no_debit_behind(fake: FakeProvider, case: str) -> None:
    message = REFUSALS[case]()
    rent = a_rent(
        fake,
        bank=case != "no bank",
        status=RentChargeStatus.WAIVED if case == "waived" else RentChargeStatus.DUE,
    )
    on = today() + timedelta(days=3)
    kwargs: dict[str, Any] = {"actor": admin()}
    if case == "past":
        on = today() - timedelta(days=1)
    if case == "card":
        kwargs["note"] = "Card 4111 1111 1111 1111"
    if case == "ended":
        lease = rent.lease
        lease.ended_on = today()
        lease.save(update_fields=["ended_on"])
    before = written()
    with override_settings(STAX_PAY_INVOICE_ENABLED=case != "flag off"):
        outcome = rent_debit(rent, on, **kwargs)
    assert (outcome.ok, outcome.message) == (False, message)
    assert written() == before


def test_a_large_debit_needs_the_confirmation(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("1000.00")):
        before = written()
        refused = rent_debit(rent, today())
        assert (refused.ok, refused.field) == (False, "confirm")
        assert written() == before
        assert rent_debit(rent, today(), confirmed=True).ok
    debit = OneOffCharge.objects.get()
    assert service.LARGE_DEBIT_ACTION in actions(debit)
    assert Job.objects.get().params["confirmed"] is True


def test_the_settled_debit_marks_the_rent_charge_paid(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    assert rent_debit(rent, today() + timedelta(days=2), actor=billing()).ok
    _pay_the_debit(OneOffCharge.objects.get(), "1500.00")
    assert fresh_rent(rent).status == RentChargeStatus.PAID
    assert reason(rent) == service.DEBIT_PAID


def test_a_refused_debit_greys_the_card_until_it_is_cancelled(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    assert rent_debit(rent, today()).ok
    fake.fail_next("pay_invoice", ProviderRejected(422, error_fields=("payment_method_id",)))
    run_queued_jobs()
    debit = OneOffCharge.objects.get()
    assert debit.status == OneOffChargeStatus.OWED
    assert service.CHARGE_FAILED_ACTION in actions(debit)
    assert reason(rent) == service.RENT_DEBIT_PENDING
    cancelled = service.cancel_charge(debit.pk, note="The bank refused it.", actor=billing())
    assert cancelled.ok, cancelled.message
    run_queued_jobs()
    assert OneOffCharge.objects.get().status == OneOffChargeStatus.VOIDED
    assert reason(rent) == ""


def test_a_rent_charge_with_an_open_debit_is_not_split(fake: FakeProvider) -> None:
    rent = a_rent(fake, status=RentChargeStatus.UNPAID)
    assert installments.split_refusal(fresh_rent(rent)) == ""
    a_debit(rent)
    assert installments.split_refusal(fresh_rent(rent)) == installments.BANK_DEBIT_OPEN
    assert installments.BANK_DEBIT_OPEN == "A bank debit is pending on this charge."


# --- The pages -----------------------------------------------------------------------------


def page(rent: RentCharge) -> str:
    return reverse("ledger:charges:record_payment", kwargs={"pk": rent.pk})


def confirm_page(rent: RentCharge) -> str:
    return reverse("ledger:charges:charge_now", kwargs={"pk": rent.pk})


def detail(rent: RentCharge) -> str:
    return reverse("ledger:charges:detail", kwargs={"pk": rent.pk})


def client(role: Role) -> Client:
    return client_for(role)[0]


def debit_post(rent: RentCharge, on: date, **values: str) -> dict[str, str]:
    data = {
        "how": "debit",
        "debit_amount": str(fresh_rent(rent).balance),
        "debit_on": on.isoformat(),
        "note": "",
        "creation_key": str(uuid.uuid4()),
    }
    data.update(values)
    return data


def test_the_add_payment_page_offers_the_debit_card(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    body = client(Role.ADMIN).get(page(rent)).content.decode()
    assert 'name="debit_amount"' in body and 'id="how-debit" checked' in body
    assert service.RENT_DEBIT_PENDING not in body


def test_the_card_is_greyed_with_the_owners_sentence_while_a_debit_is_open(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake)
    a_debit(rent)
    body = client(Role.ADMIN).get(page(rent)).content.decode()
    assert "A debit is already pending." in body and 'id="how-debit" disabled' in body


def test_today_from_add_payment_opens_the_confirmation_and_writes_nothing(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake, "1500.00")
    admin_client = client(Role.ADMIN)
    before = written()
    data = debit_post(rent, today(), note="Agreed on the phone.")
    response = admin_client.post(page(rent), data)
    assert response.status_code == 302
    assert response["Location"] == f"{confirm_page(rent)}?key={data['creation_key']}"
    assert written() == before
    body = admin_client.get(response["Location"]).content.decode()
    assert 'id="charge-now-dialog"' in body and "$1,500.00" in body
    assert f"ending {LAST_FOUR}" in body and 'id="confirm-large-debit"' not in body
    # The GET of the confirmation writes nothing either.
    assert written() == before
    done = admin_client.post(response["Location"], {"key": data["creation_key"]})
    assert done.status_code == 302 and done["Location"] == detail(rent)
    debit = OneOffCharge.objects.get()
    assert (
        debit.note == "Agreed on the phone." and str(debit.charge_now_key) == data["creation_key"]
    )
    # A second submit of the same page writes nothing.
    count = written()
    admin_client.post(response["Location"], {"key": data["creation_key"]})
    assert written() == count


def test_the_confirmation_shows_the_large_debit_box(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("1000.00")):
        body = client(Role.ADMIN).get(confirm_page(rent)).content.decode()
    assert 'id="confirm-large-debit"' in body


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_the_confirmation_is_an_admins(fake: FakeProvider, role: Role) -> None:
    rent = a_rent(fake)
    who = client(role)
    assert who.get(confirm_page(rent)).status_code == 403
    assert who.post(confirm_page(rent), {"key": str(uuid.uuid4())}).status_code == 403
    assert not OneOffCharge.objects.exists()


def test_a_viewer_cannot_open_add_payment(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    viewer = client(Role.VIEWER)
    assert viewer.get(page(rent)).status_code == 403
    assert viewer.post(page(rent), debit_post(rent, today())).status_code == 403
    assert not OneOffCharge.objects.exists()


def test_another_stores_charge_is_404(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    other = a_rent(fake, operator=rent.lease.operator)
    query = f"?store={other.lease.store_id}"
    admin_client = client(Role.ADMIN)
    assert admin_client.get(page(rent) + query).status_code == 404
    assert admin_client.get(confirm_page(rent) + query).status_code == 404
    missing = reverse("ledger:charges:charge_now", kwargs={"pk": uuid.uuid4()})
    assert admin_client.get(missing).status_code == 404


def test_a_later_date_from_add_payment_lands_on_the_rent_charge(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    on = today() + timedelta(days=4)
    response = client(Role.BILLING).post(page(rent), debit_post(rent, on))
    assert response.status_code == 302 and response["Location"] == detail(rent)
    debit = OneOffCharge.objects.get()
    assert (debit.collection, debit.due_date) == (CollectionMethod.AUTOPAY_DUE, on)


@pytest.mark.parametrize("amount", ["1499.00", "1500.01"])
def test_a_debit_of_another_amount_is_refused(fake: FakeProvider, amount: str) -> None:
    rent = a_rent(fake, "1500.00")
    admin_client = client(Role.ADMIN)
    before = written()
    on = today() + timedelta(days=3)
    response = admin_client.post(page(rent), debit_post(rent, on, debit_amount=amount))
    assert response.status_code == 302
    assert not response["Location"].startswith(confirm_page(rent))
    assert written() == before
    body = admin_client.get(page(rent)).content.decode()
    if amount == "1499.00":
        assert "A bank debit takes the whole balance, $1,500.00." in body
    else:
        assert escape(service.DEBIT_OVER_BALANCE) in body


def test_the_rent_page_shows_the_open_debit_beside_its_payments(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    debit = a_debit(rent)
    body = client(Role.VIEWER).get(detail(rent)).content.decode()
    payments = body[body.index('id="payments-card"') :]
    assert 'id="open-debit"' in payments and "Bank debit of Rent Sep 2026" in payments
    one_off = reverse("ledger:one_off_charges:detail", kwargs={"pk": debit.pk})
    assert f'href="{one_off}" id="open-debit-link">Open ›</a>' in payments
    assert "Owed" in payments[: payments.index("Open ›")]


def test_the_debit_page_reads_bank_debit(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    debit = a_debit(rent)
    body = (
        client(Role.VIEWER)
        .get(reverse("ledger:one_off_charges:detail", kwargs={"pk": debit.pk}))
        .content.decode()
    )
    assert '<span class="badge kind-badge" id="charge-kind">Bank debit</span>' in body
    assert '<dd id="category">Bank debit</dd>' in body
    assert "as a bank debit of Rent Sep 2026" in body and "from Bank debit" not in body
    assert "Re-debit" not in body and "a returned payment" not in body


# --- The wording elsewhere -----------------------------------------------------------------


def test_the_kind_label(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    debit = a_debit(rent)
    assert service.kind_label(debit) == "Bank debit"
    returned = a_payment(rent, PaymentStatus.RETURNED)
    assert service.kind_label(a_redebit_of(returned)) == "Re-debit"


def test_the_calendar_names_a_bank_debit(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    debit = a_debit(rent)
    found = calendar.items(debit.due_date, debit.due_date)
    [item] = [entry for entry in found if entry.kind == calendar.KIND_REDEBIT]
    assert item.title.startswith("Bank debit: ") and "$" not in item.title


def test_the_unpaid_balances_and_the_charges_tab_read_bank_debit(fake: FakeProvider) -> None:
    rent = a_rent(fake)
    debit = a_debit(rent)
    operator = rent.lease.operator
    assert operator is not None
    [row] = receivables.open_balances(today(), operator=operator.pk)
    assert row.description == "Bank debit: Bank debit of Rent Sep 2026"
    every = workspace.charges_tab(rent.lease.store, workspace.FILTER_ALL, Role.ADMIN)
    rows = {row.charge_id: row for row in every["charge_rows"]}
    assert rows[rent.pk].status == workspace.CARRIED_BY_BANK_DEBIT == "Carried by bank debit"
    assert rows[debit.pk].kind_label == "Bank debit"


# --- The lease's own autopay (Fable's finding on C2) ---------------------------------------


def an_autopay_rent(fake: FakeProvider, due_on: date, *, paused: bool = False) -> RentCharge:
    """A ``due`` rent charge of a lease whose rent autopay is on (or paused) at the fake,
    the operator's bank method stored."""
    lease = linked_lease(fake)
    assert lease.operator is not None
    customer = ExternalRef.objects.get(
        entity_type="operator", entity_id=lease.operator.pk, external_kind="customer"
    )
    # The stored method list (as the worker's refresh leaves it) names the linked method.
    ProviderSnapshot.objects.create(
        external_ref=customer,
        payload=[
            {
                "external_id": LEASE_BANK,
                "raw_status": "",
                "method": "bank",
                "last_four": LAST_FOUR,
                "bank_type": "checking",
                "nickname": NICKNAME,
                "is_default": True,
            }
        ],
        fetched_at=timezone.now(),
    )
    if paused:
        assert leases.pause_autopay(lease.pk, actor=None).ok
    assert leases.autopay_on(lease) is not paused
    return RentCharge.objects.create(
        lease=lease,
        period_month=due_on.replace(day=1),
        seq=1,
        due_on=due_on,
        amount_due=D("1500.00"),
        status=RentChargeStatus.DUE,
    )


def test_autopay_on_and_due_today_greys_the_card(fake: FakeProvider) -> None:
    rent = an_autopay_rent(fake, today())
    expected = f"The lease's autopay debits this charge on {day_text(today())}."
    assert reason(rent) == expected
    assert service.DEBIT_BY_AUTOPAY.format(date=day_text(today())) == expected
    before = written()
    outcome = rent_debit(rent, today() + timedelta(days=2), actor=billing())
    assert (outcome.ok, outcome.message) == (False, expected)
    assert written() == before


def test_autopay_on_and_due_later_greys_the_card(fake: FakeProvider) -> None:
    rent = an_autopay_rent(fake, today() + timedelta(days=6))
    assert reason(rent).startswith("The lease's autopay debits this charge on ")


def test_autopay_on_and_due_yesterday_still_due_is_offered(fake: FakeProvider) -> None:
    rent = an_autopay_rent(fake, today() - timedelta(days=1))
    assert reason(rent) == ""


def test_autopay_on_and_an_unpaid_charge_is_offered(fake: FakeProvider) -> None:
    rent = an_autopay_rent(fake, today())
    RentCharge.objects.filter(pk=rent.pk).update(status=RentChargeStatus.UNPAID)
    assert reason(rent) == ""


def test_autopay_paused_and_due_today_is_offered(fake: FakeProvider) -> None:
    rent = an_autopay_rent(fake, today(), paused=True)
    assert reason(rent) == ""


def test_the_autopay_reason_sits_after_the_plan_and_before_a_live_debit(
    fake: FakeProvider,
) -> None:
    rent = an_autopay_rent(fake, today())
    a_payment(rent, PaymentStatus.PENDING, "100.00")
    assert reason(rent).startswith("The lease's autopay debits this charge on ")
    a_plan(rent)
    assert reason(rent) == service.DEBIT_ON_PLAN
