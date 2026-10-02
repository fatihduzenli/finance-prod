"""Refunds, voids and the payment page (docs/qa/PLAN-009-payments-ledger.md D6 to D8;
§4 matrix rows 4, 5, 6, 8, 9, 10).

ACH payments are made the way production makes them: a provider transaction in a
:class:`~payments.fake.FakeProvider`, delivered as webhook events through the real
receiver storage and processor. The admin's refund and void then go through
:mod:`ledger.services.refunds` or the views. No test reaches the network.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
)
from ledger.services import refunds
from payments.errors import ProviderRejected, ProviderUnavailable
from payments.fake import FakeProvider
from payments.models import ExternalRef
from payments.provider import TransactionRecord
from tests.job_helpers import worker_between_requests  # noqa: F401
from tests.test_lease_services import _race
from tests.test_operator_sync import requires_postgresql
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import (
    INVOICE,
    RENT,
    SETTLED_AT,
    TXN,
    World,
    deliver,
    make_world,
    payment_for,
    provider_txn,
    replay_all,
    settle,
)

# PLAN-010 D3: views queue provider work; the worker is run after every request here.
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("worker_between_requests")]

D = Decimal
SECOND_TXN = "txn-0002"


class SettlingFake(FakeProvider):
    """A provider whose refunds read back settled at once."""

    def refund_transaction(self, external_id: str, amount: Decimal) -> TransactionRecord:
        record = super().refund_transaction(external_id, amount)
        self.transactions[record.external_id] = replace(record, settled_at=SETTLED_AT)
        return record


class AnonymousRefundFake(FakeProvider):
    """A provider whose answer to a refund names the original transaction, not the new
    one: the refund's ID is unknown until its event arrives."""

    def refund_transaction(self, external_id: str, amount: Decimal) -> TransactionRecord:
        super().refund_transaction(external_id, amount)
        return self.transactions[external_id]


def _install(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> FakeProvider:
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    return _install(monkeypatch, FakeProvider())


@pytest.fixture
def settling(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    return _install(monkeypatch, SettlingFake())


# --- Helpers ---------------------------------------------------------------------------


def ach(
    fake: FakeProvider, amount: str, external_id: str = TXN, *, settled: bool = True
) -> Payment:
    """An ACH payment of the world's charge, made by the provider's events."""
    provider_txn(fake, external_id, total=D(amount))
    assert deliver("create_transaction", id=external_id) == "processed"
    if settled:
        settle(fake, external_id)
        assert deliver("update_transaction_settled", id=external_id) == "processed"
    return payment_for(external_id)


def admin_user() -> Any:
    return client_for(Role.ADMIN)[1]


def calls(fake: FakeProvider, name: str) -> list[tuple[object, ...]]:
    return [args for method, args in fake.calls if method == name]


def refund(payment: Payment, amount: str | Decimal, **overrides: Any) -> refunds.Outcome:
    values: dict[str, Any] = {
        "amount": D(amount) if isinstance(amount, str) else amount,
        "confirmed": True,
        "actor": overrides.pop("actor", None),
        "creation_key": uuid.uuid4(),
    }
    values.update(overrides)
    return refunds.refund_payment(payment.pk, **values)


def void(payment: Payment, **overrides: Any) -> refunds.Outcome:
    values: dict[str, Any] = {"confirmed": True, "actor": None, "creation_key": uuid.uuid4()}
    values.update(overrides)
    return refunds.void_payment(payment.pk, **values)


def refund_rows(payment: Payment) -> list[Payment]:
    return list(Payment.objects.filter(parent_payment=payment).order_by("created_at"))


def charge_state(world: World) -> tuple[Decimal, str]:
    world.charge.refresh_from_db()
    return world.charge.amount_paid, world.charge.status


def audit_rows(action: str) -> list[AuditLog]:
    return list(AuditLog.objects.filter(action=action).order_by("id"))


def refund_path(payment: Payment) -> str:
    return f"/payments/{payment.pk}/refund/"


def void_path(payment: Payment) -> str:
    return f"/payments/{payment.pk}/void/"


def detail_path(payment: Payment) -> str:
    return f"/payments/{payment.pk}/"


# --- Row 4: full ACH settlement, then a partial refund -----------------------------------


def test_row4_a_full_ach_payment_partly_refunded_reopens_the_charge_as_partial(
    settling: FakeProvider,
) -> None:
    world = make_world(settling)
    payment = ach(settling, "1500.00")
    assert charge_state(world) == (RENT, RentChargeStatus.PAID)
    settling.invoices[INVOICE] = replace(settling.invoices[INVOICE], raw_status="PARTIALLY_PAID")
    actor = admin_user()
    settling.calls.clear()

    outcome = refund(payment, "500.00", actor=actor)

    assert outcome.ok and not outcome.incomplete, outcome.message
    [row] = refund_rows(payment)
    assert (row.is_refund, row.amount, row.status) == (True, D("500.00"), PaymentStatus.SETTLED)
    assert (row.method, row.kind, row.rent_charge_id, row.invoice_id) == (
        PaymentMethod.ACH,
        PaymentKind.RENT,
        world.charge.pk,
        world.invoice.pk,
    )
    refund_id = refunds.transaction_reference(row)
    assert refund_id and refund_id != TXN
    assert charge_state(world) == (D("1000.00"), RentChargeStatus.PARTIAL)
    world.invoice.refresh_from_db()
    assert world.invoice.provider_status == "PARTIALLY_PAID"
    assert [name for name, _ in settling.calls] == [
        "refund_transaction",
        "get_transaction",
        "get_invoice",
    ]
    assert calls(settling, "refund_transaction") == [(TXN, D("500.00"))]
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.SETTLED
    [entry] = audit_rows(refunds.REFUNDED_ACTION)
    assert entry.actor == actor and entry.entity_id == str(payment.pk)
    assert entry.after is not None
    assert (entry.after["amount"], entry.after["refundable"]) == ("500.00", "1000.00")
    assert entry.after["refund_payment"] == str(row.pk)
    assert entry.after["charge"]["status"] == RentChargeStatus.PARTIAL
    assert TXN not in json.dumps(entry.after) and refund_id not in json.dumps(entry.after)
    assert entry.after["provider_transaction"] == "********"


def test_row4_a_refund_equal_to_the_remaining_amount_is_allowed(settling: FakeProvider) -> None:
    world = make_world(settling)
    payment = ach(settling, "1500.00")
    assert refund(payment, "1499.99").ok
    assert refund(payment, "0.01").ok
    assert charge_state(world) == (D("0.00"), RentChargeStatus.UNPAID)
    assert refunds.payment_facts(payment).refund_refusal == refunds.NOTHING_REFUNDABLE


# --- Row 5: refund of a partial ACH payment ----------------------------------------------


def test_row5_refunds_of_a_partial_ach_payment(settling: FakeProvider) -> None:
    world = make_world(settling)
    payment = ach(settling, "600.00")
    assert charge_state(world) == (D("600.00"), RentChargeStatus.PARTIAL)

    assert refund(payment, "200.00").ok
    assert charge_state(world) == (D("400.00"), RentChargeStatus.PARTIAL)
    assert refunds.payment_facts(payment).refundable == D("400.00")

    assert refund(payment, "400.00").ok
    assert charge_state(world) == (D("0.00"), RentChargeStatus.UNPAID)
    assert [row.amount for row in refund_rows(payment)] == [D("200.00"), D("400.00")]

    settling.calls.clear()
    outcome = refund(payment, "0.01")
    assert (outcome.ok, outcome.message) == (False, refunds.NOTHING_REFUNDABLE)
    assert settling.calls == []


def test_a_pending_refund_leaves_the_charge_paid_until_it_settles(fake: FakeProvider) -> None:
    world = make_world(fake)
    payment = ach(fake, "1500.00")
    outcome = refund(payment, "500.00")
    assert outcome.ok and refunds.REFUND_PENDING.strip() in outcome.message
    [row] = refund_rows(payment)
    assert row.status == PaymentStatus.PENDING
    assert charge_state(world) == (RENT, RentChargeStatus.PAID)
    # A pending refund already counts against what is left to refund.
    assert refunds.payment_facts(payment).refundable == D("1000.00")
    fake.calls.clear()
    assert not refund(payment, "1000.01").ok
    assert fake.calls == []


# --- Row 6: void of a pending ACH payment ------------------------------------------------


def test_row6_void_of_a_pending_ach_payment(fake: FakeProvider) -> None:
    world = make_world(fake)
    payment = ach(fake, "1500.00", settled=False)
    assert charge_state(world) == (D("0.00"), RentChargeStatus.PENDING)
    fake.invoices[INVOICE] = replace(fake.invoices[INVOICE], raw_status="VOIDED-SENT")
    actor = admin_user()
    fake.calls.clear()

    outcome = void(payment, actor=actor)

    assert outcome.ok and not outcome.incomplete, outcome.message
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.VOIDED
    assert Payment.objects.count() == 1  # no new row for a void
    assert charge_state(world) == (D("0.00"), RentChargeStatus.UNPAID)
    world.invoice.refresh_from_db()
    assert world.invoice.provider_status == "VOIDED-SENT"
    assert [name for name, _ in fake.calls] == [
        "void_transaction",
        "get_transaction",
        "get_invoice",
    ]
    [entry] = audit_rows(refunds.VOIDED_ACTION)
    assert entry.actor == actor
    assert entry.after is not None and entry.after["status"] == PaymentStatus.VOIDED
    assert entry.after["provider_shows_voided"] is True
    assert TXN not in json.dumps(entry.after)


def test_void_of_a_settled_ach_payment_reopens_the_charge(fake: FakeProvider) -> None:
    world = make_world(fake)
    payment = ach(fake, "1500.00")
    assert void(payment).ok
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.VOIDED
    assert charge_state(world) == (D("0.00"), RentChargeStatus.UNPAID)


def test_a_voided_payment_is_not_voided_or_refunded_again(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    assert void(payment).ok
    fake.calls.clear()
    assert void(payment).message == refunds.VOID_NOT_OPEN.format(status="voided")
    assert refund(payment, "1.00").message == refunds.REFUND_NOT_SETTLED
    assert fake.calls == []


def test_the_voided_event_after_an_admin_void_changes_nothing(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    assert void(payment).ok
    before = list(Payment.objects.values())
    assert deliver("update_transaction", id=TXN) == "processed"
    assert list(Payment.objects.values()) == before


def test_a_payment_with_refunds_is_not_voided(settling: FakeProvider) -> None:
    make_world(settling)
    payment = ach(settling, "1500.00")
    assert refund(payment, "100.00").ok
    settling.calls.clear()
    assert void(payment).message == refunds.VOID_HAS_REFUNDS
    assert settling.calls == []


def test_a_returned_payment_is_neither_refunded_nor_voided(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    Payment.objects.filter(pk=payment.pk).update(status=PaymentStatus.RETURNED)
    fake.calls.clear()
    assert refund(payment, "1.00").message == refunds.REFUND_NOT_SETTLED
    assert void(payment).message == refunds.VOID_NOT_OPEN.format(status="returned")
    assert fake.calls == []


# --- Row 8: pay half, refund the half, pay in full ---------------------------------------


def test_row8_pay_half_refund_the_half_pay_in_full(settling: FakeProvider) -> None:
    world = make_world(settling)
    half = ach(settling, "750.00")
    assert charge_state(world) == (D("750.00"), RentChargeStatus.PARTIAL)
    assert refund(half, "750.00").ok
    assert charge_state(world) == (D("0.00"), RentChargeStatus.UNPAID)
    full = ach(settling, "1500.00", SECOND_TXN)

    assert charge_state(world) == (RENT, RentChargeStatus.PAID)
    world.charge.refresh_from_db()
    assert world.charge.balance == D("0.00")
    rows = sorted(
        Payment.objects.filter(rent_charge=world.charge).values_list(
            "is_refund", "amount", "status"
        )
    )
    assert rows == [
        (False, D("750.00"), PaymentStatus.SETTLED),
        (False, D("1500.00"), PaymentStatus.SETTLED),
        (True, D("750.00"), PaymentStatus.SETTLED),
    ]
    assert full.status == PaymentStatus.SETTLED


# --- Row 9: over-refund and bad amounts refused before any call -------------------------


@pytest.mark.parametrize(
    "amount",
    [
        D("1500.01"),
        D("1.001"),
        D("-1.00"),
        D("0"),
        D("0.00"),
        D("0.009"),
        D("NaN"),
        D("Infinity"),
    ],
)
def test_row9_a_bad_or_excessive_refund_amount_is_refused_before_any_call(
    fake: FakeProvider, amount: Decimal
) -> None:
    world = make_world(fake)
    payment = ach(fake, "1500.00")
    fake.calls.clear()
    audit_before = AuditLog.objects.count()
    outcome = refund(payment, amount)
    assert not outcome.ok and outcome.field == "amount"
    assert fake.calls == []
    assert refund_rows(payment) == []
    assert AuditLog.objects.count() == audit_before
    assert charge_state(world) == (RENT, RentChargeStatus.PAID)


def test_row9_a_float_amount_is_refused(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    fake.calls.clear()
    outcome = refund(payment, 10.0)  # type: ignore[arg-type]
    assert not outcome.ok and fake.calls == []


def test_row9_over_refund_after_an_earlier_refund(settling: FakeProvider) -> None:
    make_world(settling)
    payment = ach(settling, "1500.00")
    assert refund(payment, "1000.00").ok
    settling.calls.clear()
    outcome = refund(payment, "500.01")
    assert outcome.message == refunds.AMOUNT_OVER.format(refundable=D("500.00"))
    assert settling.calls == []


@pytest.mark.parametrize("amount", ["1500.01", "1.001", "-1.00", "0", "abc", ""])
def test_row9_the_page_refuses_a_bad_amount_before_any_call(
    fake: FakeProvider, amount: str
) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    fake.calls.clear()
    client, _ = client_for(Role.ADMIN)
    response = client.post(
        refund_path(payment),
        {"creation_key": str(uuid.uuid4()), "amount": amount, "confirm": "on"},
    )
    assert response.status_code == 302 and response["Location"] == refund_path(payment)
    assert fake.calls == []
    assert refund_rows(payment) == []
    page = client.get(refund_path(payment)).content.decode()
    assert "field-error" in page


# --- Confirmation ------------------------------------------------------------------------


@pytest.mark.parametrize("ticked", [None, "", "0", "off", "false", "yes", "1", "ON"])
def test_without_the_exact_confirmation_nothing_is_sent(
    fake: FakeProvider, ticked: str | None
) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    fake.calls.clear()
    client, _ = client_for(Role.ADMIN)
    data = {"creation_key": str(uuid.uuid4()), "amount": "10.00"}
    if ticked is not None:
        data["confirm"] = ticked
    client.post(refund_path(payment), data)
    void_data = {"creation_key": str(uuid.uuid4())}
    if ticked is not None:
        void_data["confirm"] = ticked
    client.post(void_path(payment), void_data)
    assert fake.calls == []
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.SETTLED and refund_rows(payment) == []


@pytest.mark.parametrize("ticked", ["on", "true"])
def test_the_exact_confirmation_values_are_accepted(fake: FakeProvider, ticked: str) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    client, _ = client_for(Role.ADMIN)
    response = client.post(
        refund_path(payment),
        {"creation_key": str(uuid.uuid4()), "amount": "10.00", "confirm": ticked},
    )
    assert response.status_code == 302 and response["Location"] == detail_path(payment)
    assert len(calls(fake, "refund_transaction")) == 1


def test_the_service_refuses_without_confirmation(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    fake.calls.clear()
    assert refund(payment, "1.00", confirmed=False).field == "confirm"
    assert void(payment, confirmed=False).field == "confirm"
    assert fake.calls == []


# --- Row 10: billing and viewer ----------------------------------------------------------


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_row10_billing_and_viewer_get_403_on_every_refund_and_void_route(
    fake: FakeProvider, role: Role
) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00", settled=False)
    settle(fake)
    deliver("update_transaction_settled", id=TXN)
    fake.calls.clear()
    client, _ = client_for(role)
    for path in (refund_path(payment), void_path(payment)):
        assert client.get(path).status_code == 403, path
        response = client.post(
            path, {"creation_key": str(uuid.uuid4()), "amount": "10.00", "confirm": "on"}
        )
        assert response.status_code == 403, path
    assert fake.calls == []
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.SETTLED and refund_rows(payment) == []
    page = client.get(detail_path(payment))
    assert page.status_code == 200
    html = page.content.decode()
    assert 'id="refund-payment"' not in html and 'id="void-payment"' not in html
    assert "/refund/" not in html and "/void/" not in html
    assert not AuditLog.objects.filter(
        action__in=(refunds.REFUND_FAILED_ACTION, refunds.VOID_FAILED_ACTION)
    ).exists()


def test_a_user_without_a_role_gets_403_on_the_payment_page(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    client, _ = client_for(None)
    for path in (detail_path(payment), refund_path(payment), void_path(payment)):
        assert client.get(path).status_code == 403, path


def test_the_admin_sees_the_buttons_only_where_they_apply(settling: FakeProvider) -> None:
    make_world(settling)
    payment = ach(settling, "1500.00")
    client, _ = client_for(Role.ADMIN)
    html = client.get(detail_path(payment)).content.decode()
    assert 'id="refund-payment"' in html and 'id="void-payment"' in html
    assert refund(payment, "100.00").ok
    html = client.get(detail_path(payment)).content.decode()
    assert 'id="refund-payment"' in html and 'id="void-payment"' not in html
    [row] = refund_rows(payment)
    html = client.get(detail_path(row)).content.decode()
    assert 'id="refund-payment"' not in html and 'id="void-payment"' not in html


def test_a_manual_payment_has_no_refund_or_void(fake: FakeProvider) -> None:
    world = make_world(fake)
    wire = Payment.objects.create(
        rent_charge=world.charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.WIRE,
        amount=D("100.00"),
        status=PaymentStatus.SETTLED,
        note="Wire received",
    )
    fake.calls.clear()
    assert refund(wire, "10.00").message == refunds.NOT_ACH
    assert void(wire).message == refunds.NOT_ACH
    assert fake.calls == []
    client, _ = client_for(Role.ADMIN)
    html = client.get(detail_path(wire)).content.decode()
    assert 'id="refund-payment"' not in html and 'id="void-payment"' not in html
    page = client.get(refund_path(wire)).content.decode()
    assert refunds.NOT_ACH.split(".")[0] in page and 'id="refund-form"' not in page


def test_an_ach_payment_without_a_reference_has_no_refund(fake: FakeProvider) -> None:
    world = make_world(fake)
    orphan = Payment.objects.create(
        rent_charge=world.charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D("100.00"),
        status=PaymentStatus.SETTLED,
    )
    assert refund(orphan, "10.00").message == refunds.NO_REFERENCE
    assert void(orphan).message == refunds.NO_REFERENCE
    assert fake.calls == []


# --- Idempotency -------------------------------------------------------------------------


def test_a_replayed_confirmation_refunds_once(settling: FakeProvider) -> None:
    make_world(settling)
    payment = ach(settling, "1500.00")
    key = uuid.uuid4()
    assert refund(payment, "100.00", creation_key=key).ok
    again = refund(payment, "100.00", creation_key=key)
    assert again.ok and again.replayed and again.message == refunds.ALREADY_SUBMITTED
    assert len(calls(settling, "refund_transaction")) == 1
    assert len(refund_rows(payment)) == 1


def test_a_replayed_page_post_refunds_once(settling: FakeProvider) -> None:
    make_world(settling)
    payment = ach(settling, "1500.00")
    client, _ = client_for(Role.ADMIN)
    data = {"creation_key": str(uuid.uuid4()), "amount": "100.00", "confirm": "on"}
    first = client.post(refund_path(payment), data)
    second = client.post(refund_path(payment), data)
    assert first["Location"] == second["Location"] == detail_path(payment)
    assert len(calls(settling, "refund_transaction")) == 1
    assert len(refund_rows(payment)) == 1


def test_a_replayed_void_confirmation_voids_once(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    key = uuid.uuid4()
    assert void(payment, creation_key=key).ok
    assert void(payment, creation_key=key).replayed
    assert len(calls(fake, "void_transaction")) == 1


def test_a_replayed_key_after_a_failure_is_not_sent_again(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    key = uuid.uuid4()
    fake.fail_next("refund_transaction", ProviderUnavailable(503))
    assert not refund(payment, "100.00", creation_key=key).ok
    assert refund(payment, "100.00", creation_key=key).replayed
    assert len(calls(fake, "refund_transaction")) == 1


# --- The webhook handler and the admin action agree --------------------------------------


def test_the_refund_event_settles_the_admin_refund_row(fake: FakeProvider) -> None:
    world = make_world(fake)
    payment = ach(fake, "1500.00")
    assert refund(payment, "500.00").ok
    [row] = refund_rows(payment)
    refund_id = refunds.transaction_reference(row)
    assert deliver("create_transaction", id=refund_id) == "processed"
    settle(fake, refund_id)
    assert deliver("update_transaction_settled", id=refund_id) == "processed"
    replay_all()
    [row] = refund_rows(payment)
    assert row.status == PaymentStatus.SETTLED
    assert charge_state(world) == (D("1000.00"), RentChargeStatus.PARTIAL)


def test_a_refund_whose_id_is_unknown_gets_it_from_its_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _install(monkeypatch, AnonymousRefundFake())
    world = make_world(provider)
    payment = ach(provider, "1500.00")
    outcome = refund(payment, "500.00")
    assert outcome.ok
    [row] = refund_rows(payment)
    assert (row.status, refunds.transaction_reference(row)) == (PaymentStatus.PENDING, "")
    assert charge_state(world) == (RENT, RentChargeStatus.PAID)

    provider_txn(
        provider,
        "txn-refund-9",
        total=D("500.00"),
        kind="refund",
        parent_external_id=TXN,
        settled_at=SETTLED_AT,
    )
    assert deliver("create_transaction", id="txn-refund-9") == "processed"
    assert deliver("update_transaction_settled", id="txn-refund-9") == "processed"
    replay_all()

    [row] = refund_rows(payment)
    assert refunds.transaction_reference(row) == "txn-refund-9"
    assert row.status == PaymentStatus.SETTLED
    assert charge_state(world) == (D("1000.00"), RentChargeStatus.PARTIAL)
    assert audit_rows("payment.provider_id_recorded")


def test_an_event_of_another_amount_does_not_take_the_unknown_refund_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _install(monkeypatch, AnonymousRefundFake())
    make_world(provider)
    payment = ach(provider, "1500.00")
    assert refund(payment, "500.00").ok
    provider_txn(provider, "txn-refund-7", total=D("20.00"), kind="refund", parent_external_id=TXN)
    deliver("create_transaction", id="txn-refund-7")
    rows = refund_rows(payment)
    assert sorted(row.amount for row in rows) == [D("20.00"), D("500.00")]
    unknown = next(row for row in rows if row.amount == D("500.00"))
    assert refunds.transaction_reference(unknown) == ""


# --- Provider failures -------------------------------------------------------------------


def test_a_refused_refund_changes_nothing_but_the_audit_row(fake: FakeProvider) -> None:
    world = make_world(fake)
    payment = ach(fake, "1500.00")
    fake.fail_next("refund_transaction", ProviderRejected(422, error_fields=("total",)))
    actor = admin_user()
    outcome = refund(payment, "100.00", actor=actor)
    assert not outcome.ok
    assert "ProviderRejected" in outcome.message and "total" in outcome.message
    assert refund_rows(payment) == []
    assert charge_state(world) == (RENT, RentChargeStatus.PAID)
    assert [name for name, _ in fake.calls if name != "get_invoice"][-1] == "refund_transaction"
    [entry] = audit_rows(refunds.REFUND_FAILED_ACTION)
    assert entry.actor == actor and entry.after is not None
    assert (entry.after["failure"], entry.after["fields"]) == ("ProviderRejected", "total")


def test_a_refused_void_changes_nothing_but_the_audit_row(fake: FakeProvider) -> None:
    world = make_world(fake)
    payment = ach(fake, "1500.00", settled=False)
    fake.fail_next("void_transaction", ProviderRejected(422, error_fields=("id",)))
    outcome = void(payment)
    assert not outcome.ok and "ProviderRejected" in outcome.message
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PENDING
    assert charge_state(world) == (D("0.00"), RentChargeStatus.PENDING)
    [entry] = audit_rows(refunds.VOID_FAILED_ACTION)
    assert entry.after is not None and entry.after["fields"] == "id"


def test_an_unreachable_provider_says_the_refund_may_have_been_made(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    fake.fail_next("refund_transaction", ProviderUnavailable(None))
    outcome = refund(payment, "100.00")
    assert not outcome.ok and "may have been made" in outcome.message


def test_the_page_shows_the_failure_class_and_fields(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    fake.fail_next("refund_transaction", ProviderRejected(422, error_fields=("total",)))
    client, _ = client_for(Role.ADMIN)
    response = client.post(
        refund_path(payment),
        {"creation_key": str(uuid.uuid4()), "amount": "100.00", "confirm": "on"},
        follow=True,
    )
    html = response.content.decode()
    assert "ProviderRejected" in html and "fields: total" in html
    # PLAN-010 D3: the outcome is shown on the payment page, with the refund button
    # leading to a fresh form to try again.
    assert response.redirect_chain == [(f"/payments/{payment.pk}/", 302)]
    assert 'id="refund-payment"' in html


def test_a_failed_refetch_keeps_the_refund_and_says_so(settling: FakeProvider) -> None:
    world = make_world(settling)
    payment = ach(settling, "1500.00")
    settling.fail_next("get_transaction", ProviderUnavailable(503))
    outcome = refund(payment, "500.00")
    assert outcome.ok and outcome.incomplete and "could not be read back" in outcome.message
    [row] = refund_rows(payment)
    # The provider's answer named the refund: the row keeps its ID, pending until its event.
    assert refunds.transaction_reference(row) != ""
    assert row.status == PaymentStatus.PENDING
    assert charge_state(world) == (RENT, RentChargeStatus.PAID)


def test_a_failed_refetch_after_a_void_still_voids(fake: FakeProvider) -> None:
    world = make_world(fake)
    payment = ach(fake, "1500.00", settled=False)
    fake.fail_next("get_transaction", ProviderUnavailable(503))
    outcome = void(payment)
    assert outcome.ok and outcome.incomplete
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.VOIDED
    assert charge_state(world) == (D("0.00"), RentChargeStatus.UNPAID)


# --- The pages ---------------------------------------------------------------------------


def test_the_refund_page_shows_what_the_admin_confirms(settling: FakeProvider) -> None:
    world = make_world(settling)
    payment = ach(settling, "1500.00")
    assert refund(payment, "200.00").ok
    client, _ = client_for(Role.ADMIN)
    html = client.get(refund_path(payment)).content.decode()
    for text in (
        world.operator.display_name,
        world.lease.store.external_label,
        world.invoice.invoice_number,
        "debit 1",
        'id="summary-amount">$1500.00',
        'id="summary-refunded">$200.00',
        'id="summary-refundable">$1300.00',
        'value="1300.00"',
        refunds.REFUND_SENTENCE,
        refunds.REFUND_CONFIRMATION,
        'name="confirm" value="true"',
        "csrfmiddlewaretoken",
    ):
        assert text in html, text
    assert TXN not in html


def test_the_void_page_shows_what_the_admin_confirms(fake: FakeProvider) -> None:
    world = make_world(fake)
    payment = ach(fake, "1500.00", settled=False)
    client, _ = client_for(Role.ADMIN)
    html = client.get(void_path(payment)).content.decode()
    for text in (
        world.operator.display_name,
        world.invoice.invoice_number,
        refunds.VOID_SENTENCE,
        refunds.void_confirmation(payment),
    ):
        assert text in html, text
    assert 'name="amount"' not in html


def test_the_payment_page_shows_the_chain(settling: FakeProvider) -> None:
    world = make_world(settling)
    payment = ach(settling, "1500.00")
    assert refund(payment, "300.00", actor=admin_user()).ok
    [row] = refund_rows(payment)
    client, _ = client_for(Role.VIEWER)
    html = client.get(detail_path(payment)).content.decode()
    for text in (
        detail_path(row),
        "&minus;$300.00",
        world.invoice.invoice_number,
        'id="invoice-provider-status"',
        'id="charge-due">$1500.00',
        'id="charge-paid">$1200.00',
        'id="charge-balance">$300.00',
        refunds.REFUNDED_ACTION,
        "payment.created_from_provider",
    ):
        assert text in html, text
    child = client.get(detail_path(row)).content.decode()
    assert detail_path(payment) in child and 'id="parent-payment"' in child


def test_the_payment_page_refuses_a_bad_id_and_any_post() -> None:
    client, _ = client_for(Role.ADMIN)
    assert client.get("/payments/not-a-uuid/").status_code == 404
    assert client.get(f"/payments/{uuid.uuid4()}/").status_code == 404
    assert client.get(f"/payments/{uuid.uuid4()}/refund/").status_code == 404
    assert client.post(f"/payments/{uuid.uuid4()}/").status_code == 403


def test_the_payments_ledger_links_each_row_to_its_page(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    client, _ = client_for(Role.VIEWER)
    assert detail_path(payment) in client.get("/payments/").content.decode()


def test_refunds_and_voids_never_unmask_an_identifier_in_audit_rows(
    settling: FakeProvider,
) -> None:
    make_world(settling)
    payment = ach(settling, "1500.00")
    assert refund(payment, "100.00").ok
    settling.fail_next("void_transaction", ProviderRejected(422))
    refund_ids = set(ExternalRef.objects.values_list("external_id", flat=True))
    void(payment)
    for entry in AuditLog.objects.filter(action__startswith="payment."):
        text = json.dumps([entry.before, entry.after])
        for external_id in refund_ids:
            assert external_id not in text, (entry.action, external_id)


# --- Two admins at once (PostgreSQL) -----------------------------------------------------


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_admins_confirming_at_once_make_one_refund(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowFake(SettlingFake):
        def refund_transaction(self, external_id: str, amount: Decimal) -> TransactionRecord:
            time.sleep(0.3)
            return super().refund_transaction(external_id, amount)

    provider = _install(monkeypatch, SlowFake())
    world = make_world(provider)
    payment = ach(provider, "1500.00")
    outcomes = _race(lambda: refund(payment, "1500.00"), lambda: refund(payment, "1500.00"))
    assert sorted(outcome.ok for outcome in outcomes) == [False, True]
    refused = next(outcome for outcome in outcomes if not outcome.ok)
    assert refused.message == refunds.NOTHING_REFUNDABLE
    assert len(calls(provider, "refund_transaction")) == 1
    assert len(refund_rows(payment)) == 1
    assert charge_state(world) == (D("0.00"), RentChargeStatus.UNPAID)


def test_refund_and_void_are_admin_routes_for_post_only() -> None:
    for name in ("ledger:payments:refund", "ledger:payments:void"):
        rule = registry.rule_for(name)
        assert rule is not None and rule.min_role == Role.ADMIN, name
        assert rule.methods == frozenset({"POST"}), name
        assert name not in registry.READ_ONLY_ROUTE_NAMES
    assert "ledger:payments:detail" in registry.READ_ONLY_ROUTE_NAMES
