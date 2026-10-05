"""Deposit resolution at the end of a lease (docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md
Batch B: D7, D12; §6 test plan).

The service (refunded, withheld, transferred, and every refusal), its effects on the
payments ledger (a deposit refund is money out, never revenue), the lease page action,
the deposit's page and the role gates. Nothing reaches the payment provider.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AuditLog,
    DepositResolution,
    DepositResolutionKind,
    Lease,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    SetupCharge,
    SetupChargeStatus,
)
from ledger.services import deposits as service
from ledger.services import leases, payments_ledger, pnl, refunds
from tests.test_lease_services import _race, make_operator
from tests.test_operator_views import client_for
from tests.test_setup_charges import DAY, a_charge, a_lease, deposit_type, fee, fresh, pay

pytestmark = pytest.mark.django_db

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="row locks and concurrent transactions need PostgreSQL",
)

D = Decimal
ENDED = date(2026, 9, 30)
ON = date(2026, 10, 10)
NOTE = "Lease ended; deposit returned after the walk-through."
CARD = "4111 1111 1111 1111"


# --- Builders ----------------------------------------------------------------------------


def end(lease: Lease, on: date = ENDED) -> Lease:
    """End the lease through the staff path (``leases.end_lease``)."""
    return leases.end_lease(lease.pk, on, "Operator moved out.", actor=None, today=DAY)


def held_deposit(paid: str = "2500.00", price: str = "2500.00", ended: bool = True) -> SetupCharge:
    charge = a_charge(price, deposit_type())
    if D(paid):
        outcome = pay(charge, paid)
        assert outcome.ok, outcome.message
    if ended:
        end(charge.lease)
    return fresh(charge)


def resolve(charge: SetupCharge, kind: str, amount: str, **overrides: Any) -> service.Outcome:
    values: dict[str, Any] = {
        "resolved_on": ON,
        "method": PaymentMethod.WIRE if kind == DepositResolutionKind.REFUNDED else "",
        "note": NOTE,
        "to_lease": None,
        "actor": None,
        "today": DAY,
    }
    values.update(overrides)
    return service.resolve_deposit(charge, kind, D(amount), **values)


def sibling(charge: SetupCharge, **overrides: Any) -> Lease:
    """Another lease of the deposit's operator, not ended."""
    return a_lease(operator=charge.lease.operator, **overrides)


def nothing_written(charge: SetupCharge, payments: int = 1) -> None:
    assert not DepositResolution.objects.exists()
    assert Payment.objects.filter(setup_charge__lease=charge.lease).count() == payments
    assert not AuditLog.objects.filter(action__startswith="setup_charge.deposit_").exists()
    assert SetupCharge.objects.count() == 1


# --- Refunded, withheld, transferred (D7) --------------------------------------------------


def test_a_full_refund_writes_a_deposit_refund_payment_and_keeps_nothing() -> None:
    charge = held_deposit()
    outcome = resolve(charge, DepositResolutionKind.REFUNDED, "2500.00")
    assert (outcome.ok, outcome.message) == (True, "Deposit refunded.")
    resolution = DepositResolution.objects.get()
    assert (resolution.kind, resolution.amount, resolution.withheld_amount) == (
        "refunded",
        D("2500.00"),
        D("0.00"),
    )
    [paid_back] = Payment.objects.filter(kind=PaymentKind.REFUND_DEPOSIT)
    assert (paid_back.setup_charge_id, paid_back.method, paid_back.amount) == (
        charge.pk,
        PaymentMethod.WIRE,
        D("2500.00"),
    )
    assert (paid_back.status, paid_back.settled_on, paid_back.note) == (
        PaymentStatus.SETTLED,
        ON,
        NOTE,
    )
    assert (paid_back.is_refund, paid_back.parent_payment_id) == (False, None)
    # Paying the deposit back does not reopen the charge.
    assert fresh(charge).status == SetupChargeStatus.PAID
    entry = AuditLog.objects.get(action="setup_charge.deposit_refunded")
    assert entry.entity_id == str(charge.pk)
    assert entry.after is not None and entry.after["payment"] == str(paid_back.pk)


def test_a_partial_refund_records_the_rest_as_withheld() -> None:
    charge = held_deposit()
    resolve(charge, DepositResolutionKind.REFUNDED, "2000.00", method=PaymentMethod.ZELLE)
    resolution = DepositResolution.objects.get()
    assert (resolution.amount, resolution.withheld_amount, resolution.method) == (
        D("2000.00"),
        D("500.00"),
        "zelle",
    )


def test_a_refund_is_money_out_and_never_revenue_in_the_payments_ledger() -> None:
    sign = a_charge("1000.00", fee("sign_fee"))
    pay(sign, "1000.00")
    charge = held_deposit()
    before = payments_ledger.totals(
        payments_ledger.ledger_payments(payments_ledger.PaymentFilter())
    )
    assert (before.settled, before.deposits_held) == (D("1000.00"), D("2500.00"))
    resolve(charge, DepositResolutionKind.REFUNDED, "2000.00")
    after = payments_ledger.totals(payments_ledger.ledger_payments(payments_ledger.PaymentFilter()))
    # 2,000 paid back, 500 withheld (revenue from that day): nothing is held, as in
    # the P&L footer.
    assert (after.settled, after.refunded, after.deposits_held) == (
        D("1000.00"),
        D("0.00"),
        D("0.00"),
    )
    footer = pnl.deposits_held(pnl.parse_query({"from": "2026-01", "to": "2026-12"}))
    assert footer == after.deposits_held
    rows = payments_ledger.rows_for(
        payments_ledger.ledger_payments(payments_ledger.PaymentFilter())
    )
    [refund_row] = [row for row in rows if row.payment.kind == PaymentKind.REFUND_DEPOSIT]
    record = payments_ledger.csv_record(refund_row)
    assert record[payments_ledger.CSV_HEADER.index("charge_kind")] == "refund_deposit"
    paid_back = Payment.objects.get(kind=PaymentKind.REFUND_DEPOSIT)
    facts = refunds.payment_facts(paid_back)
    assert facts.refund_refusal == refunds.IS_DEPOSIT_REFUND


def test_the_ledger_page_shows_the_refund_with_its_charge() -> None:
    charge = held_deposit()
    resolve(charge, DepositResolutionKind.REFUNDED, "2500.00")
    client, _ = client_for(Role.VIEWER)
    listing = client.get("/payments/?kind=refund_deposit").content.decode()
    assert f"/setup-charges/{charge.pk}/" in listing


def test_withheld_keeps_the_whole_deposit_and_writes_no_payment() -> None:
    charge = held_deposit()
    outcome = resolve(charge, DepositResolutionKind.WITHHELD, "2500.00")
    assert (outcome.ok, outcome.message) == (True, "Deposit withheld.")
    resolution = DepositResolution.objects.get()
    assert (resolution.withheld_amount, resolution.method, resolution.to_lease_id) == (
        D("2500.00"),
        "",
        None,
    )
    assert Payment.objects.count() == 1  # the deposit's own payment only
    assert AuditLog.objects.filter(action="setup_charge.deposit_withheld").count() == 1


def test_withholding_part_of_a_deposit_is_refused_with_the_way_to_do_it() -> None:
    charge = held_deposit()
    outcome = resolve(charge, DepositResolutionKind.WITHHELD, "500.00")
    assert (outcome.ok, outcome.message, outcome.field) == (False, service.WITHHELD_PART, "amount")
    nothing_written(charge)


def test_a_transfer_creates_a_paid_deposit_on_the_operators_other_lease() -> None:
    charge = held_deposit()
    target = sibling(charge)
    outcome = resolve(charge, DepositResolutionKind.TRANSFERRED, "2000.00", to_lease=target)
    assert (outcome.ok, outcome.message) == (True, "Deposit transferred.")
    received = SetupCharge.objects.get(lease=target)
    assert received.fee_type.code == "security_deposit"
    assert (received.price, received.cost, received.amount_paid, received.status) == (
        D("2000.00"),
        None,
        D("2000.00"),
        SetupChargeStatus.PAID,
    )
    [moved] = Payment.objects.filter(setup_charge=received)
    assert (moved.kind, moved.method, moved.amount, moved.status) == (
        PaymentKind.SETUP,
        PaymentMethod.OTHER,
        D("2000.00"),
        PaymentStatus.SETTLED,
    )
    assert moved.note == f"Deposit transferred from {charge.lease.store.external_label} lease"
    resolution = DepositResolution.objects.get()
    assert (resolution.to_lease_id, resolution.withheld_amount) == (target.pk, D("500.00"))
    assert not Payment.objects.filter(kind=PaymentKind.REFUND_DEPOSIT).exists()
    created = AuditLog.objects.get(action="setup_charge.created", entity_id=str(received.pk))
    assert created.after is not None and created.after["transferred_from"] == str(charge.pk)


def test_a_transfer_then_a_partial_refund_on_the_target_holds_the_rest_once() -> None:
    charge = held_deposit()
    target = sibling(charge)
    assert resolve(charge, DepositResolutionKind.TRANSFERRED, "2500.00", to_lease=target).ok
    end(target, ON)
    received = SetupCharge.objects.get(lease=target)
    assert resolve(received, DepositResolutionKind.REFUNDED, "1000.00").ok

    def held(**selected: Any) -> Decimal:
        rows = payments_ledger.ledger_payments(payments_ledger.PaymentFilter(**selected))
        return payments_ledger.totals(rows).deposits_held

    # 2,500 in on each lease, 2,500 out by the transfer, 1,000 paid back and the 1,500
    # rest withheld (revenue, not held): nothing is held, never 5,000 less the refund.
    assert held() == D("0.00")
    # The transfer leaves the first lease's store and lands on the target's.
    assert held(store=charge.lease.store_id) == D("0.00")
    assert held(store=target.store_id) == D("0.00")
    # The P&L footer applies the same rule: the ledger and the footer agree.
    assert pnl.deposits_held(pnl.parse_query({"from": "2026-01", "to": "2026-12"})) == held()


def test_a_transfer_works_even_when_the_deposit_type_is_inactive() -> None:
    charge = held_deposit()
    fee("security_deposit", name="Security deposit", refundable=True, active=False)
    target = sibling(charge)
    assert resolve(charge, DepositResolutionKind.TRANSFERRED, "2500.00", to_lease=target).ok


# --- Refusals (§6) ------------------------------------------------------------------------


def test_a_non_refundable_charge_is_refused() -> None:
    charge = a_charge("1000.00", fee("sign_fee"))
    pay(charge, "1000.00")
    end(charge.lease)
    outcome = resolve(charge, DepositResolutionKind.REFUNDED, "1000.00")
    assert (outcome.ok, outcome.message) == (False, service.NOT_REFUNDABLE)
    nothing_written(charge)


def test_a_lease_not_ended_is_refused() -> None:
    charge = held_deposit(ended=False)
    outcome = resolve(charge, DepositResolutionKind.REFUNDED, "2500.00")
    assert (outcome.ok, outcome.message) == (False, service.LEASE_NOT_ENDED)
    nothing_written(charge)


@pytest.mark.parametrize(
    ("paid", "amount"),
    [("2500.00", "2500.01"), ("1000.00", "2500.00"), ("0", "0.01")],
    ids=["a cent above", "partly paid", "nothing paid"],
)
def test_an_amount_above_what_was_paid_is_refused(paid: str, amount: str) -> None:
    charge = held_deposit(paid=paid)
    for kind in DepositResolutionKind.values:
        target = sibling(charge) if kind == DepositResolutionKind.TRANSFERRED else None
        outcome = resolve(charge, kind, amount, to_lease=target)
        assert (outcome.ok, outcome.field) == (False, "amount"), kind
        assert outcome.message == service.ABOVE_PAID.format(paid=D(paid).quantize(D("0.01")))
    nothing_written(charge, payments=1 if D(paid) else 0)


def test_a_returned_payment_does_not_count_as_paid() -> None:
    charge = held_deposit()
    Payment.objects.filter(setup_charge=charge).update(status=PaymentStatus.RETURNED)
    outcome = resolve(charge, DepositResolutionKind.REFUNDED, "2500.00")
    assert outcome.message == service.ABOVE_PAID.format(paid="0.00")


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"amount": "0"}, "amount", service.AMOUNT_NOT_POSITIVE),
        ({"amount": "-1.00"}, "amount", service.AMOUNT_NOT_POSITIVE),
        ({"amount": "1.001"}, "amount", service.NOT_CENTS),
        ({"kind": "kept"}, "kind", service.KIND_INVALID),
        ({"resolved_on": date(2026, 10, 16)}, "resolved_on", service.DATE_FUTURE),
        ({"note": ""}, "note", service.NOTE_REQUIRED),
        ({"note": " ​ \n"}, "note", service.NOTE_REQUIRED),
        (
            {"note": f"Paid back to {CARD}"},
            "note",
            "This text may not contain card or account numbers.",
        ),
        ({"method": ""}, "method", service.METHOD_REQUIRED),
        ({"method": "ach"}, "method", service.METHOD_REQUIRED),
        ({"kind": "withheld", "method": "wire"}, "method", service.METHOD_NOT_REFUND),
        ({"kind": "transferred", "method": ""}, "to_lease", service.TARGET_REQUIRED),
    ],
    ids=[
        "zero",
        "negative",
        "three decimals",
        "unknown kind",
        "future date",
        "empty note",
        "invisible note",
        "card number",
        "no method",
        "ach",
        "method on withheld",
        "no target",
    ],
)
def test_bad_input_is_refused_with_a_sentence_and_writes_nothing(
    overrides: dict[str, Any], field: str, message: str
) -> None:
    charge = held_deposit()
    kind = overrides.pop("kind", DepositResolutionKind.REFUNDED)
    amount = overrides.pop("amount", "100.00")
    outcome = resolve(charge, kind, amount, **overrides)
    assert (outcome.ok, outcome.field, outcome.message) == (False, field, message)
    nothing_written(charge)


def test_a_target_lease_on_a_refund_is_refused() -> None:
    charge = held_deposit()
    outcome = resolve(charge, DepositResolutionKind.REFUNDED, "100.00", to_lease=sibling(charge))
    assert (outcome.ok, outcome.message) == (False, service.TARGET_NOT_TRANSFER)
    nothing_written(charge)


def test_a_transfer_to_another_operators_lease_is_refused() -> None:
    charge = held_deposit()
    stranger = a_lease(operator=make_operator(email="other@example.com", customer=None))
    outcome = resolve(charge, DepositResolutionKind.TRANSFERRED, "100.00", to_lease=stranger)
    assert (outcome.ok, outcome.message) == (False, service.TARGET_OTHER_OPERATOR)
    nothing_written(charge)


def test_a_transfer_to_an_ended_lease_or_to_its_own_lease_is_refused() -> None:
    charge = held_deposit()
    ended = end(sibling(charge))
    for target in (ended, charge.lease):
        outcome = resolve(charge, DepositResolutionKind.TRANSFERRED, "100.00", to_lease=target)
        assert (outcome.ok, outcome.message) == (False, service.TARGET_ENDED)
    nothing_written(charge)


def test_a_transfer_to_a_lease_with_a_deposit_already_is_refused() -> None:
    charge = held_deposit()
    target = sibling(charge)
    a_charge_on = SetupCharge.objects.create(
        lease=target, fee_type=deposit_type(), price=D("100.00"), due_date=DAY
    )
    outcome = resolve(charge, DepositResolutionKind.TRANSFERRED, "100.00", to_lease=target)
    assert (outcome.ok, outcome.message) == (False, service.TARGET_HAS_DEPOSIT)
    assert SetupCharge.objects.filter(lease=target).get() == a_charge_on
    assert not DepositResolution.objects.exists()


def test_a_deposit_is_resolved_once() -> None:
    charge = held_deposit()
    assert resolve(charge, DepositResolutionKind.REFUNDED, "1000.00").ok
    again = resolve(charge, DepositResolutionKind.WITHHELD, "1500.00")
    assert (again.ok, again.message) == (False, service.ALREADY_RESOLVED)
    assert DepositResolution.objects.count() == 1
    assert Payment.objects.filter(kind=PaymentKind.REFUND_DEPOSIT).count() == 1


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_resolutions_make_one() -> None:
    charge = held_deposit()
    results = _race(
        lambda: resolve(charge, DepositResolutionKind.REFUNDED, "2500.00"),
        lambda: resolve(charge, DepositResolutionKind.REFUNDED, "2500.00"),
    )
    assert sorted(result.ok for result in results) == [False, True]
    assert DepositResolution.objects.count() == 1
    assert Payment.objects.filter(kind=PaymentKind.REFUND_DEPOSIT).count() == 1


# --- Screens and roles (D7, D12) ---------------------------------------------------------


def resolve_path(charge: SetupCharge) -> str:
    return f"/leases/{charge.lease_id}/deposits/{charge.pk}/resolve/"


def form(**overrides: str) -> dict[str, str]:
    values = {
        "kind": "refunded",
        "amount": "2500.00",
        "resolved_on": "2026-10-01",
        "method": "wire",
        "to_lease": "",
        "note": NOTE,
    }
    return {**values, **overrides}


def test_the_route_is_admin_only() -> None:
    rule = registry.rule_for("ledger:leases:resolve_deposit")
    assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_billing_and_viewer_cannot_resolve_a_deposit(role: Role) -> None:
    charge = held_deposit()
    client, _ = client_for(role)
    lease_page = client.get(f"/leases/{charge.lease_id}/").content.decode()
    assert "Resolve deposit" not in lease_page and "resolve-deposit" not in lease_page
    assert client.get(resolve_path(charge)).status_code == 403
    assert client.post(resolve_path(charge), form()).status_code == 403
    assert not DepositResolution.objects.exists()


def test_the_lease_page_offers_the_action_only_once_ended_and_for_deposits() -> None:
    charge = held_deposit(ended=False)
    sign = a_charge("1000.00", fee("sign_fee"), operator=charge.lease.operator)
    SetupCharge.objects.filter(pk=sign.pk).update(lease=charge.lease)
    client, _ = client_for(Role.ADMIN)
    lease_page = f"/leases/{charge.lease_id}/"
    assert resolve_path(charge) not in client.get(lease_page).content.decode()
    end(charge.lease)
    shown = client.get(lease_page).content.decode()
    assert resolve_path(charge) in shown
    assert f"/deposits/{sign.pk}/resolve/" not in shown


def test_an_admin_resolves_through_the_page_and_the_deposit_page_shows_it() -> None:
    charge = held_deposit()
    client, user = client_for(Role.ADMIN)
    page = client.get(resolve_path(charge)).content.decode()
    assert 'id="deposit-paid">$2500.00<' in page
    response = client.post(
        resolve_path(charge), form(amount="2000.00", note="Kept 500 for repairs.")
    )
    assert response["Location"] == f"/setup-charges/{charge.pk}/"
    resolution = DepositResolution.objects.get()
    assert (resolution.created_by, resolution.withheld_amount) == (user, D("500.00"))
    detail = client.get(f"/setup-charges/{charge.pk}/").content.decode()
    assert 'id="deposit-resolution"' in detail and "Refunded on 2026-10-01" in detail
    assert '<dd id="resolution-withheld">$500.00</dd>' in detail
    assert "Kept 500 for repairs." in detail
    lease_page = client.get(f"/leases/{charge.lease_id}/").content.decode()
    assert "Refunded $2000.00" in lease_page and resolve_path(charge) not in lease_page


def test_an_admin_transfers_to_a_lease_chosen_from_the_operators_open_leases() -> None:
    charge = held_deposit()
    target = sibling(charge)
    stranger = a_lease(operator=make_operator(email="stranger@example.com", customer=None))
    client, _ = client_for(Role.ADMIN)
    page = client.get(resolve_path(charge)).content.decode()
    assert f'value="{target.pk}"' in page and f'value="{stranger.pk}"' not in page
    client.post(resolve_path(charge), form(kind="transferred", method="", to_lease=str(target.pk)))
    assert SetupCharge.objects.get(lease=target).status == SetupChargeStatus.PAID
    # A posted lease that is not offered is still refused by the service.
    other = held_deposit()
    client.post(resolve_path(other), form(kind="transferred", method="", to_lease=str(stranger.pk)))
    assert service.TARGET_OTHER_OPERATOR in client.get(resolve_path(other)).content.decode()
    assert DepositResolution.objects.count() == 1


def test_a_refusal_is_held_on_the_form_and_a_card_number_is_never_held() -> None:
    charge = held_deposit()
    client, _ = client_for(Role.ADMIN)
    response = client.post(resolve_path(charge), form(amount="2600.00"))
    assert response["Location"] == resolve_path(charge)
    assert (
        "more than was paid on the deposit ($2500.00)"
        in client.get(resolve_path(charge)).content.decode()
    )
    client.post(resolve_path(charge), form(note=f"Sent to {CARD}"))
    page = client.get(resolve_path(charge)).content.decode()
    assert "may not contain card or account numbers" in page
    # The whole number and its spaced form, never a fragment.
    assert CARD not in page and CARD.replace(" ", "") not in page
    assert not DepositResolution.objects.exists()


def test_a_charge_of_another_lease_in_the_url_is_404() -> None:
    charge = held_deposit()
    other = held_deposit()
    client, _ = client_for(Role.ADMIN)
    assert client.get(f"/leases/{other.lease_id}/deposits/{charge.pk}/resolve/").status_code == 404
    assert (
        client.get(f"/leases/{charge.lease_id}/deposits/{uuid.uuid4()}/resolve/").status_code == 404
    )
    assert client.get(f"/leases/{charge.lease_id}/deposits/nope/resolve/").status_code == 404
