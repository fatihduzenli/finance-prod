"""PLAN-015e: a returned bank debit that was kept as a credit reverses the credit, and staff and
the operator are told (docs/qa/15/PLAN-015e-credit-return.md §3 to §5).

Task 1: the held row split off a payment names it (``split_from``), the new CHECKs, and ledger
migration 0077 both ways (the data step linking the rows split before it, the seeded templates,
the refused reverse).
"""

from __future__ import annotations

import uuid
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
    MessageTemplate,
    OutboundMessage,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services import start_date
from tests.test_plan015d_change_start import an_active_lease, apply, november, pay

pytestmark = pytest.mark.django_db

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
