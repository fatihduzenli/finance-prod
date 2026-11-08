"""PLAN-015e: a returned bank debit that was kept as a credit reverses the credit, and staff and
the operator are told (docs/qa/15/PLAN-015e-credit-return.md §3 to §5).

Task 1: the held row split off a payment names it (``split_from``), the new CHECKs, and ledger
migration 0077 both ways (the data step linking the rows split before it, the seeded templates,
the refused reverse). Task 2: the return reaches the credit through real webhook deliveries to a
:class:`~payments.fake.FakeProvider` (whole and split rows, unapplied, partly and fully applied,
refunded at the lease end, replays, a row split before ledger 0077), the P&L's credits held.
Task 3: the NSF step of such a return (the credit's lease, one fee, no re-debit, no template F,
the transaction's total).
"""

from __future__ import annotations

import functools
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from ledger.models import (
    AuditLog,
    LeaseCredit,
    MessageRule,
    MessageTemplate,
    OneOffCategory,
    OneOffCharge,
    OutboundMessage,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import credits, leases, nsf, receivables, rent_run, returns, start_date
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind
from payments.webhooks.handlers import transaction as transaction_handler
from tests import test_returns_nsf as nsf_tests
from tests.test_lease_services import make_lease
from tests.test_plan015d_change_start import an_active_lease, apply, november, pay
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_returns_nsf import NOTICE, Clock, a_world
from tests.test_webhook_handlers import (
    TXN,
    World,
    deliver,
    payment_for,
    provider_txn,
    replay_all,
    settle,
    state,
)

pytestmark = pytest.mark.django_db

fake = nsf_tests.fake
#: The patched ``timezone.now`` (autouse): Friday Nov 6, 2026, the return's notice.
clock = nsf_tests.clock

D = Decimal
NOV_5 = date(2026, 11, 5)
BEFORE = ("ledger", "0076_one_off_charge_dated")
AFTER = ("ledger", "0077_credit_return")
SEEDED = ("credit_returned", "credit_returned_operator")


@pytest.fixture
def nov_5(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: NOV_5)
    return NOV_5


def split_credit(method: str = PaymentMethod.ZELLE) -> tuple[Payment, Payment, LeaseCredit]:
    """November's $3,000.00 paid, then the start moved from Nov 1 to Nov 4 and the $300.00 of
    Nov 1–3 kept as a credit: the payment, the held row split off it and the credit."""
    lease = an_active_lease()
    payment = pay(november(lease), "3000.00", method)
    change = apply(lease, date(2026, 11, 4), choice="credit")
    assert change.outcome == start_date.OUTCOME_CREDIT
    credit = LeaseCredit.objects.get(lease=lease)
    held = Payment.objects.get(credit=credit)
    return Payment.objects.get(pk=payment.pk), held, credit


# --- split_from -----------------------------------------------------------------------------


def test_a_held_row_split_off_a_payment_names_it(nov_5: date) -> None:
    payment, held, credit = split_credit()
    assert held.pk != payment.pk
    assert (held.split_from_id, held.credit_id, held.amount) == (payment.pk, credit.pk, D("300"))
    assert payment.split_from_id is None
    assert list(payment.split_rows.all()) == [held]


def test_a_payment_held_whole_names_no_payment_it_was_split_from(nov_5: date) -> None:
    lease = an_active_lease()
    charge = november(lease)
    payment = pay(charge, "300.00", PaymentMethod.ACH)
    with transaction.atomic():
        held = start_date._hold(
            lease, charge, payment, D("300.00"), None, NOV_5, "Kept as a credit."
        )
    assert held.pk == payment.pk
    held = Payment.objects.get(pk=held.pk)
    assert (held.credit_id is not None, held.split_from_id, held.rent_charge_id) == (
        True,
        None,
        None,
    )


def test_only_a_held_row_names_a_payment_it_was_split_from(nov_5: date) -> None:
    payment, held, _ = split_credit()
    # A payment on a charge (not held as a credit) may not name one.
    with pytest.raises(IntegrityError), transaction.atomic():
        Payment.objects.filter(pk=payment.pk).update(split_from=held)


def test_a_reversed_credit_holds_nothing(nov_5: date) -> None:
    _, _, credit = split_credit()
    with pytest.raises(IntegrityError), transaction.atomic():
        LeaseCredit.objects.filter(pk=credit.pk).update(reversed_at=timezone.now())
    LeaseCredit.objects.filter(pk=credit.pk).update(reversed_at=timezone.now(), remaining=0)
    assert LeaseCredit.objects.get(pk=credit.pk).remaining == D("0.00")


def test_the_two_templates_are_seeded_with_their_own_placeholders() -> None:
    from ledger import merge

    for code in SEEDED:
        template = MessageTemplate.objects.filter(code=code).first()
        if template is None:  # a flush by an earlier transactional test removed it
            continue
        assert template.active and template.channel == "email"
        assert template.internal is (code == "credit_returned")
        assert not template.merge_field_errors()
        assert merge.fields_for(code) != merge.FIELDS


# --- Ledger migration 0077 both ways --------------------------------------------------------


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


@pytest.mark.django_db(transaction=True)
def test_0077_links_the_rows_split_before_it_and_reverses_without_its_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: NOV_5)
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    payment, held, credit = split_credit()
    # A payment held whole: its audit row names itself as the held payment.
    lease = an_active_lease()
    whole = pay(november(lease), "300.00", PaymentMethod.ACH)
    with transaction.atomic():
        start_date._hold(lease, november(lease), whole, D("300.00"), None, NOV_5, "Kept.")
    try:
        old = _migrate(BEFORE)
        old_payment = old.get_model("ledger", "Payment")
        assert "split_from" not in {field.name for field in old_payment._meta.fields}
        assert (
            not old.get_model("ledger", "MessageTemplate").objects.filter(code__in=SEEDED).exists()
        )
        new = _migrate(AFTER)
        rows = new.get_model("ledger", "Payment").objects
        assert rows.get(pk=held.pk).split_from_id == payment.pk
        assert rows.get(pk=whole.pk).split_from_id is None
        assert rows.get(pk=payment.pk).split_from_id is None
        templates = new.get_model("ledger", "MessageTemplate").objects
        assert set(templates.filter(code__in=SEEDED).values_list("code", flat=True)) == set(SEEDED)
        # Only the split row is linked.
        assert rows.filter(split_from__isnull=False).count() == 1
        # The reverse is refused while a reversed credit exists, and nothing changes.
        credits = new.get_model("ledger", "LeaseCredit").objects
        credits.filter(pk=credit.pk).update(reversed_at=timezone.now(), remaining=0)
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(BEFORE)
        assert rows.get(pk=held.pk).split_from_id == payment.pk
        credits.filter(pk=credit.pk).update(reversed_at=None, remaining=D("300.00"))
        # ... and while a lease is flagged for a reversed credit.
        leases = new.get_model("ledger", "Lease").objects
        leases.filter(pk=lease.pk).update(needs_review=True, review_reason="credit_returned")
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(BEFORE)
        leases.filter(pk=lease.pk).update(needs_review=False, review_reason="")
        back = _migrate(BEFORE)
        assert (
            not back.get_model("ledger", "MessageTemplate").objects.filter(code__in=SEEDED).exists()
        )
        assert back.get_model("ledger", "LeaseCredit").objects.get(pk=credit.pk).remaining == D(
            "300.00"
        )
    finally:
        _migrate(*leaves)


@pytest.mark.django_db(transaction=True)
def test_0077_reverse_is_refused_while_a_message_about_a_reversed_credit_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: NOV_5)
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    _, _, credit = split_credit()
    template, _ = MessageTemplate.objects.get_or_create(
        code="credit_returned",
        defaults={"internal": True, "subject": "Credit reversed", "body": "Reversed."},
    )
    lease = credit.lease
    assert lease.operator is not None
    OutboundMessage.objects.create(
        operator=lease.operator,
        lease=lease,
        template=template,
        kind="credit_returned",
        rule="credit_returned",
        charge_entity_type="lease_credit",
        charge_entity_id=credit.pk,
        to_address="managers@example.com",
        rendered_subject="Credit reversed",
        rendered_body="Reversed.",
    )
    try:
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(BEFORE)
        assert MessageTemplate.objects.filter(code="credit_returned").exists()
    finally:
        _migrate(*leaves)


def test_a_message_about_a_credit_must_be_of_its_kind_and_rule(nov_5: date) -> None:
    _, _, credit = split_credit()
    template, _ = MessageTemplate.objects.get_or_create(
        code="credit_returned",
        defaults={"internal": True, "subject": "Credit reversed", "body": "Reversed."},
    )
    row = {
        "operator": credit.lease.operator,
        "lease": credit.lease,
        "template": template,
        "to_address": "managers@example.com",
        "rendered_subject": "Credit reversed",
        "rendered_body": "Reversed.",
        "charge_entity_id": credit.pk,
    }
    for bad in (
        {"kind": "ladder", "rule": "credit_returned", "charge_entity_type": "lease_credit"},
        {"kind": "credit_returned", "rule": "F", "charge_entity_type": "lease_credit"},
        {"kind": "credit_returned", "rule": "credit_returned", "charge_entity_type": "payment"},
        {"kind": "ladder", "rule": "F", "charge_entity_type": "lease_credit"},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            OutboundMessage.objects.create(**row, **bad)
    for rule in SEEDED:
        OutboundMessage.objects.create(
            **row, kind="credit_returned", rule=rule, charge_entity_type="lease_credit"
        )
    assert OutboundMessage.objects.filter(charge_entity_id=credit.pk).count() == 2


def test_the_held_audit_row_names_the_split_row(nov_5: date) -> None:
    payment, held, _ = split_credit()
    audit = AuditLog.objects.get(action=start_date.HELD_ACTION, entity_id=str(payment.pk))
    assert audit.after["held_payment"] == str(held.pk)
    assert held.kind == PaymentKind.RENT and held.status == PaymentStatus.SETTLED
    assert uuid.UUID(audit.after["credit"]) == held.credit_id


# --- Task 2: the return reaches the credit (through real webhook deliveries) ----------------
#
# The world of tests/test_returns_nsf.py: an October rent charge of $1,500.00 on an invoice,
# paid by a bank debit (txn-0001) delivered through the webhook and settled; staff then kept
# the debit (or part of it) as a credit, which may have paid later charges. The bank returns
# the debit days later (R01).


def a_debit(fake: FakeProvider) -> tuple[World, Payment]:
    """The world and its settled bank debit."""
    world = a_world(fake)
    provider_txn(fake)
    assert deliver("create_transaction", id=TXN) == "processed"
    settle(fake)
    assert deliver("update_transaction_settled", id=TXN) == "processed"
    payment = payment_for()
    assert payment.status == PaymentStatus.SETTLED and payment.rent_charge_id == world.charge.pk
    return world, payment


def kept_whole(fake: FakeProvider) -> tuple[World, Payment, LeaseCredit]:
    """The whole debit kept as a credit (its month void, as Change start date leaves it)."""
    world, payment = a_debit(fake)
    with transaction.atomic():
        held = start_date._hold(
            world.lease, world.charge, payment, D("1500.00"), None, NOTICE, "Kept as a credit."
        )
    RentCharge.objects.filter(pk=world.charge.pk).update(
        status=RentChargeStatus.VOID, amount_paid=D("0.00")
    )
    assert held.pk == payment.pk
    held = Payment.objects.get(pk=held.pk)
    assert held.credit is not None
    return world, held, held.credit


def kept_part(fake: FakeProvider, part: str = "300.00") -> tuple[World, Payment, Payment]:
    """``part`` of the debit kept as a credit, split off it (the charge now owes the rest)."""
    world, payment = a_debit(fake)
    with transaction.atomic():
        held = start_date._hold(
            world.lease, world.charge, payment, D(part), None, NOTICE, "Kept as a credit."
        )
        charge = RentCharge.objects.select_for_update().get(pk=world.charge.pk)
        charge.amount_due = D("1500.00") - D(part)
        charge.save(update_fields=["amount_due"])
        rent_run.recompute_charge(charge)
    assert held.pk != payment.pk and held.split_from_id == payment.pk
    return world, Payment.objects.get(pk=payment.pk), held


def a_rent(world: World, month: date, amount: str) -> RentCharge:
    return RentCharge.objects.create(
        lease=world.lease,
        period_month=month,
        seq=1,
        due_on=month,
        amount_due=D(amount),
        status=RentChargeStatus.DUE,
    )


def apply_credit(world: World, charge: RentCharge, amount: str) -> Payment:
    """Apply ``amount`` of the lease's credit to ``charge``, as the store page's form does."""
    with transaction.atomic():
        used = credits.apply(
            credits.locked(world.lease, NOTICE),
            functools.partial(rent_run.record_manual_payment, defer_mark=True),
            charge.pk,
            D(amount),
            key=lambda credit: uuid.uuid4(),
            paid_on=NOTICE,
            reference="",
            note="Credit applied.",
            allocation_group=uuid.uuid4(),
            actor=None,
            today=NOTICE,
        )
    assert isinstance(used, list), used
    payment = used[0].outcome.payment
    assert payment is not None
    return payment


def bank_returns(fake: FakeProvider, code: str = "R01") -> str:
    fake.transactions[TXN] = replace(
        fake.transactions[TXN], returned=True, return_code=code, return_reason="Bank text"
    )
    return deliver("update_transaction_settled", id=TXN)


def reversal_rows(credit: LeaseCredit) -> list[AuditLog]:
    return list(AuditLog.objects.filter(action=credits.REVERSED_ACTION, entity_id=str(credit.pk)))


#: The provider calls a return may make: reads only (docs/CLAUDE.md Rule 2).
READS = {"get_transaction", "get_invoice"}


def writes(fake: FakeProvider) -> list[str]:
    return [name for name, _ in fake.calls if name not in READS]


def test_an_unapplied_credit_kept_whole_is_reversed_to_zero(fake: FakeProvider) -> None:
    world, held, credit = kept_whole(fake)
    assert credits.balance(world.lease) == D("1500.00")
    before = len(fake.calls)
    assert bank_returns(fake) == "processed"
    held = Payment.objects.get(pk=held.pk)
    assert (held.status, held.return_code) == (PaymentStatus.RETURNED, "R01")
    credit = LeaseCredit.objects.get(pk=credit.pk)
    assert (credit.remaining, credit.reversed_reason) == (D("0.00"), "R01: Bank text")
    assert credit.reversed_at is not None
    assert credits.balance(world.lease) == D("0.00")
    [row] = reversal_rows(credit)
    assert row.after["unapplied"] == "1500.00" and row.after["applications"] == []
    assert row.after["payment"] == str(held.pk) and row.after["reversed_after_refund"] is False
    assert row.before["remaining"] == "1500.00"
    assert not AuditLog.objects.filter(action=transaction_handler.UNPLACED_ACTION).exists()
    # The void month stays void: the credit held nothing owed.
    assert RentCharge.objects.get(pk=world.charge.pk).status == RentChargeStatus.VOID
    assert writes(fake)[before:] == [] and not writes(fake)


def test_a_split_return_reopens_the_charge_and_reverses_the_split_rows_credit(
    fake: FakeProvider,
) -> None:
    world, payment, held = kept_part(fake)
    assert RentCharge.objects.get(pk=world.charge.pk).status == RentChargeStatus.PAID
    assert bank_returns(fake) == "processed"
    payment = Payment.objects.get(pk=payment.pk)
    held = Payment.objects.get(pk=held.pk)
    assert payment.status == held.status == PaymentStatus.RETURNED
    assert (held.return_code, held.return_reason, held.returned_at) == (
        payment.return_code,
        payment.return_reason,
        payment.returned_at,
    )
    # One return to act on: the transaction's own row; the split row is resolved with it.
    assert payment.resolved_at is None
    assert held.resolved_at is not None
    assert held.resolution_note == transaction_handler.SPLIT_RESOLVED
    charge = RentCharge.objects.get(pk=world.charge.pk)
    assert (charge.status, charge.amount_paid, charge.needs_review) == (
        RentChargeStatus.UNPAID,
        D("0.00"),
        True,
    )
    assert held.credit is not None
    credit = LeaseCredit.objects.get(pk=held.credit.pk)
    assert (credit.remaining, credit.reversed_at is not None) == (D("0.00"), True)
    split_audit = AuditLog.objects.get(
        action="payment.status_changed", entity_id=str(held.pk), after__split_from=str(payment.pk)
    )
    assert split_audit.after["status"] == PaymentStatus.RETURNED
    assert [row.pk for row in returns.open_returns()] == [payment.pk]
    assert not writes(fake)


def test_a_partly_applied_credit_returns_its_application_and_the_charge_is_owed_again(
    fake: FakeProvider,
) -> None:
    world, held, credit = kept_whole(fake)
    november = a_rent(world, date(2026, 11, 1), "1000.00")
    application = apply_credit(world, november, "1000.00")
    assert RentCharge.objects.get(pk=november.pk).status == RentChargeStatus.PAID
    assert LeaseCredit.objects.get(pk=credit.pk).remaining == D("500.00")
    assert bank_returns(fake) == "processed"
    application = Payment.objects.get(pk=application.pk)
    assert (application.status, application.return_code) == (PaymentStatus.RETURNED, "R01")
    assert application.resolution_note == credits.APPLICATION_RESOLVED
    november = RentCharge.objects.get(pk=november.pk)
    assert (november.status, november.amount_paid, november.balance, november.needs_review) == (
        RentChargeStatus.UNPAID,
        D("0.00"),
        D("1000.00"),
        True,
    )
    assert any(
        row.charge_id == november.pk and row.balance == D("1000.00")
        for row in receivables.open_balances(NOTICE)
    )
    [row] = reversal_rows(credit)
    assert row.after["unapplied"] == "500.00"
    assert row.after["applications"] == [{"payment": str(application.pk), "amount": "1000.00"}]
    assert AuditLog.objects.filter(
        action="rent_charge.ach_returned", entity_id=str(november.pk)
    ).exists()
    # Only the bank debit's own return waits in the returns queue.
    assert [payment.pk for payment in returns.open_returns()] == [held.pk]


def test_a_credit_applied_to_two_charges_in_two_months_owes_both_again(
    fake: FakeProvider,
) -> None:
    world, _, credit = kept_whole(fake)
    november = a_rent(world, date(2026, 11, 1), "900.00")
    december = a_rent(world, date(2026, 12, 1), "600.00")
    first = apply_credit(world, november, "900.00")
    second = apply_credit(world, december, "600.00")
    assert LeaseCredit.objects.get(pk=credit.pk).remaining == D("0.00")
    assert bank_returns(fake) == "processed"
    for charge, amount in ((november, "900.00"), (december, "600.00")):
        row = RentCharge.objects.get(pk=charge.pk)
        assert (row.status, row.balance, row.needs_review) == (
            RentChargeStatus.UNPAID,
            D(amount),
            True,
        )
    assert {payment.status for payment in Payment.objects.filter(pk__in=[first.pk, second.pk])} == {
        PaymentStatus.RETURNED
    }
    [reversal] = reversal_rows(credit)
    assert reversal.after["unapplied"] == "0.00" and len(reversal.after["applications"]) == 2
    assert credits.balance(world.lease) == D("0.00")


def test_a_credit_refunded_at_the_lease_end_is_only_marked_reversed(fake: FakeProvider) -> None:
    world, _, credit = kept_whole(fake)
    november = a_rent(world, date(2026, 11, 1), "1000.00")
    application = apply_credit(world, november, "1000.00")
    with transaction.atomic():
        [refund] = credits.refund_all(
            world.lease,
            method=PaymentMethod.CHECK,
            on=NOTICE,
            reference="CHK-1",
            note="Refunded at the lease end.",
            actor=None,
        )
    assert bank_returns(fake) == "processed"
    credit = LeaseCredit.objects.get(pk=credit.pk)
    assert (credit.remaining, credit.reversed_at is not None) == (D("0.00"), True)
    [row] = reversal_rows(credit)
    assert row.after["reversed_after_refund"] is True and row.after["applications"] == []
    # The money went out: nothing else changes; staff decide.
    assert Payment.objects.get(pk=refund.pk).status == PaymentStatus.SETTLED
    assert Payment.objects.get(pk=application.pk).status == PaymentStatus.SETTLED
    assert RentCharge.objects.get(pk=november.pk).status == RentChargeStatus.PAID
    assert credits.balance(world.lease) == D("0.00")
    assert credits.held(NOTICE) == D("0.00")


def test_a_replayed_return_reverses_nothing_again(fake: FakeProvider) -> None:
    world, _, credit = kept_whole(fake)
    november = a_rent(world, date(2026, 11, 1), "1000.00")
    apply_credit(world, november, "1000.00")
    assert bank_returns(fake) == "processed"
    first = state()
    replay_all()
    assert bank_returns(fake) == "processed"
    replay_all()
    assert state() == first
    assert len(reversal_rows(credit)) == 1
    assert not writes(fake)


def test_a_split_row_not_linked_at_the_return_is_returned_by_a_later_event(
    fake: FakeProvider,
) -> None:
    """The debit returned before ledger 0077 linked its split row (the row unlinked at the
    return); the migration's data step links it; the next event returns it too."""
    world, payment, held = kept_part(fake)
    Payment.objects.filter(pk=held.pk).update(split_from=None)
    assert bank_returns(fake) == "processed"
    assert Payment.objects.get(pk=held.pk).status == PaymentStatus.SETTLED
    assert held.credit_id is not None
    assert LeaseCredit.objects.get(pk=held.credit_id).reversed_at is None
    Payment.objects.filter(pk=held.pk).update(split_from=payment)
    replay_all()
    held = Payment.objects.get(pk=held.pk)
    assert held.status == PaymentStatus.RETURNED and held.credit is not None
    credit = LeaseCredit.objects.get(pk=held.credit.pk)
    assert (credit.remaining, credit.reversed_at is not None) == (D("0.00"), True)
    assert len(reversal_rows(credit)) == 1
    replay_all()
    assert len(reversal_rows(credit)) == 1


def test_a_whole_credit_returned_before_this_plan_is_reversed_by_a_later_event(
    fake: FakeProvider,
) -> None:
    world, held, credit = kept_whole(fake)
    # As before PLAN-015e: the row returned, the credit left as it was.
    Payment.objects.filter(pk=held.pk).update(
        status=PaymentStatus.RETURNED, returned_at=timezone.now(), return_code="R01"
    )
    fake.transactions[TXN] = replace(
        fake.transactions[TXN], returned=True, return_code="R01", return_reason="Bank text"
    )
    assert LeaseCredit.objects.get(pk=credit.pk).reversed_at is None
    replay_all()
    assert LeaseCredit.objects.get(pk=credit.pk).reversed_at is not None
    assert len(reversal_rows(credit)) == 1


def test_a_returned_payment_with_neither_a_charge_nor_a_credit_is_audited_unplaced(
    fake: FakeProvider,
) -> None:
    a_world(fake)
    provider_txn(fake, invoice_external_id="")
    assert deliver("create_transaction", id=TXN) == "processed"
    payment = payment_for()
    assert (payment.rent_charge_id, payment.credit_id) == (None, None)
    assert bank_returns(fake) == "processed"
    [row] = AuditLog.objects.filter(
        action=transaction_handler.UNPLACED_ACTION, entity_id=str(payment.pk)
    )
    assert row.after["status"] == PaymentStatus.RETURNED and row.after["return_code"] == "R01"


def test_the_pnl_credits_held_count_a_reversed_credit_until_its_reversal(
    fake: FakeProvider, clock: Clock
) -> None:
    world, _, credit = kept_whole(fake)
    november = a_rent(world, date(2026, 11, 1), "1000.00")
    apply_credit(world, november, "1000.00")
    assert credits.held(NOTICE) == D("500.00")
    clock.at(date(2026, 11, 10))
    assert bank_returns(fake) == "processed"
    # Before the reversal's day the credit was held, less what it had paid; from then, none.
    assert credits.held(date(2026, 11, 9)) == D("500.00")
    assert credits.held(date(2026, 11, 10)) == D("0.00")
    assert credits.held(date(2026, 12, 31)) == D("0.00")


def test_a_non_ach_credit_has_no_return_path(nov_5: date) -> None:
    """A credit kept from a Zelle payment: no transaction names it, so nothing returns it;
    the reversal refuses a payment that is not returned."""
    _, held, credit = split_credit(PaymentMethod.ZELLE)
    assert not ExternalRef.objects.filter(entity_id=held.pk).exists()
    assert credits.reverse_from_return(held, lambda row: None) is None
    assert LeaseCredit.objects.get(pk=credit.pk).reversed_at is None


# --- Task 3: the NSF step of a debit kept as a credit ---------------------------------------


def return_audit(payment: Payment) -> AuditLog:
    return AuditLog.objects.get(action=nsf.RETURN_ACTION, entity_id=str(payment.pk))


def test_a_debit_kept_whole_gets_its_fee_on_the_credits_lease_and_no_redebit_or_f(
    fake: FakeProvider,
) -> None:
    world, held, credit = kept_whole(fake)
    # The operator's earliest active lease is another one: never the guess.
    other = make_lease(operator=world.operator, started_on=date(2025, 1, 1))
    assert leases.first_active_lease(world.operator) == other
    assert bank_returns(fake, "R01") == "processed"
    [fee] = OneOffCharge.objects.filter(source_payment=held, category=OneOffCategory.NSF_FEE)
    assert fee.lease_id == credit.lease_id == world.lease.pk
    assert fee.description == (
        f"{OneOffCategory.NSF_FEE.label}: returned debit of a payment kept as a credit"
    )
    # R01 is retried on a charge; a credit's return is not.
    assert not OneOffCharge.objects.filter(category=OneOffCategory.REDEBIT).exists()
    assert not OutboundMessage.objects.filter(rule=MessageRule.F).exists()
    audit = return_audit(held)
    assert audit.after["retry"] == nsf.NO_RETRY_CREDIT
    assert audit.after["lease"] == str(world.lease.pk)
    assert audit.after["credit_reversed"] == [str(credit.pk)]
    assert audit.after["transaction_total"] == "1500.00"
    assert audit.after["fee_charge"] == str(fee.pk) and audit.after["message"] is None
    assert not Job.objects.filter(kind=JobKind.SCHEDULE_CREATE).exists()
    assert not writes(fake)
    # The returns queue says why there is no retry, and shows the credit's store.
    state = nsf.queue_states([Payment.objects.get(pk=held.pk)])[held.pk]
    assert state.text == nsf.STATE_CREDIT and state.fee == fee.pk
    row = returns.row(Payment.objects.get(pk=held.pk))
    assert row.store is not None and row.store.pk == world.lease.store_id


def test_a_split_debit_counts_its_whole_transaction_and_is_not_redebited(
    fake: FakeProvider,
) -> None:
    world, payment, held = kept_part(fake)
    assert bank_returns(fake, "R09") == "processed"
    audit = return_audit(payment)
    assert audit.after["transaction_total"] == "1500.00"
    assert audit.after["retry"] == nsf.NO_RETRY_CREDIT
    assert audit.after["credit_reversed"] == [str(held.credit_id)]
    [fee] = OneOffCharge.objects.filter(category=OneOffCategory.NSF_FEE)
    assert (fee.source_payment_id, fee.lease_id) == (payment.pk, world.lease.pk)
    # One fee for the one bank debit: none for the split row.
    assert not OneOffCharge.objects.filter(source_payment=held).exists()
    assert not OneOffCharge.objects.filter(category=OneOffCategory.REDEBIT).exists()
    assert not OutboundMessage.objects.filter(rule=MessageRule.F).exists()
    replay_all()
    assert OneOffCharge.objects.filter(category=OneOffCategory.NSF_FEE).count() == 1


def test_a_debit_not_kept_as_a_credit_is_still_redebited(fake: FakeProvider) -> None:
    _, payment = a_debit(fake)
    assert bank_returns(fake, "R01") == "processed"
    audit = return_audit(payment)
    assert audit.after["retry"] == nsf.RETRY_AUTO
    assert audit.after["credit_reversed"] == [] and audit.after["transaction_total"] == "1500.00"
    assert OneOffCharge.objects.filter(category=OneOffCategory.REDEBIT).count() == 1
