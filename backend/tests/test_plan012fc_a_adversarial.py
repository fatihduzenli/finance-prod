"""Fable's adversarial cases for PLAN-012f-c Batch A, the void of a payment recorded outside
the processor (plan §6 A, the cases left to Fable): two voids at the same moment; a void of
one row of a split payment; a card number and a bank pattern in the reason; a hostile return
address and a foreign store on the dialog; what a GET and a HEAD leave; the reason's bounds
and its escaping on the page; two payments voided one after the other."""

from __future__ import annotations

import threading
import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection, connections
from django.urls import reverse
from django.utils.html import escape

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    OneOffCharge,
    Payment,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import refunds
from payments.fake import FakeProvider
from tests.receivables_helpers import a_one_off_charge, a_rent_charge
from tests.test_operator_views import client_for
from tests.test_payment_allocation import a_world, form, operator_of
from tests.test_void_manual_payment import (
    REASON,
    billing_user,
    detail_path,
    dialog_path,
    manual,
    rent_charge,
    void,
    written,
)

pytestmark = pytest.mark.django_db

D = Decimal

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="row locks: PostgreSQL only"
)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def figures(charge: RentCharge) -> tuple[Decimal, Decimal, str]:
    charge.refresh_from_db()
    return charge.amount_paid, charge.balance, charge.status


def voided_rows(payment: Payment) -> int:
    return AuditLog.objects.filter(
        action=refunds.VOIDED_MANUAL_ACTION, entity_id=str(payment.pk)
    ).count()


# --- Two voids at the same moment ----------------------------------------------------------


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_voids_at_the_same_moment_void_once_and_audit_once(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    actor = billing_user()
    barrier = threading.Barrier(2)
    outcomes: list[refunds.Outcome] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            barrier.wait(timeout=10)
            outcomes.append(
                refunds.void_manual_payment(
                    payment.pk, reason=REASON, actor=actor, creation_key=uuid.uuid4()
                )
            )
        except BaseException as error:
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert sorted(outcome.ok for outcome in outcomes) == [False, True]
    [loser] = [outcome for outcome in outcomes if not outcome.ok]
    assert loser.message == refunds.ALREADY_VOIDED
    assert voided_rows(payment) == 1
    assert figures(charge) == (D("0.00"), D("1500.00"), RentChargeStatus.UNPAID)


# --- Two payments, voided one after the other ------------------------------------------------


def test_two_payments_voided_one_after_the_other_step_the_charge_down(
    fake: FakeProvider,
) -> None:
    charge = rent_charge("1500.00")
    first = manual(charge, "1000.00")
    second = manual(charge, "500.00")
    assert figures(charge) == (D("1500.00"), D("0.00"), RentChargeStatus.PAID)
    assert void(first).ok
    assert figures(charge) == (D("500.00"), D("1000.00"), RentChargeStatus.PARTIAL)
    second.refresh_from_db()
    assert second.status == PaymentStatus.SETTLED and second.void_reason == ""
    assert void(second).ok
    assert figures(charge) == (D("0.00"), D("1500.00"), RentChargeStatus.UNPAID)
    assert Payment.objects.filter(status=PaymentStatus.VOIDED).count() == 2


# --- One row of a split payment ----------------------------------------------------------------


def test_voiding_one_row_of_a_split_payment_leaves_its_siblings_and_their_charges(
    fake: FakeProvider,
) -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    other = a_one_off_charge(operator_of(lease), "250.00", date(2026, 9, 20), lease=lease)
    data = form("1250.00", {rent.pk: "1000.00", other.pk: "250.00"})
    client, _ = client_for(Role.BILLING)
    response = client.post(
        reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}), data
    )
    assert response.status_code == 302
    rent_row = Payment.objects.get(rent_charge=rent)
    other_row = Payment.objects.get(one_off_charge=other)
    assert rent_row.allocation_group == other_row.allocation_group
    assert void(rent_row).ok
    rent.refresh_from_db()
    assert (rent.amount_paid, rent.status) == (D("0.00"), RentChargeStatus.UNPAID)
    other_row.refresh_from_db()
    assert other_row.status == PaymentStatus.SETTLED
    assert OneOffCharge.objects.get(pk=other.pk).status == "paid"
    # The sibling row is still offered a void of its own.
    assert refunds.manual_void_refusal(other_row) == ""


# --- The reason ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "Paid by card 4111 1111 1111 1111 instead",
        "Routing 021000021 account 000123456789 was wrong",
    ],
)
def test_a_reason_with_payment_data_is_refused_by_the_form_and_the_service(
    fake: FakeProvider, reason: str
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    before = written()
    refused = void(payment, reason=reason)
    assert (refused.ok, refused.field) == (False, "reason")
    assert written() == before
    client, _ = client_for(Role.BILLING)
    response = client.post(
        dialog_path(payment), {"creation_key": str(uuid.uuid4()), "reason": reason}
    )
    assert response.status_code == 302 and response["Location"].startswith(dialog_path(payment))
    body = client.get(response["Location"]).content.decode()
    # The digits as typed, with and without their spaces (a hex id may hold "4111").
    for digits in ("4111 1111 1111 1111", "4111111111111111", "000123456789"):
        assert digits not in body, digits
    assert written() == before
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.SETTLED


def test_the_reason_bounds_two_hundred_in_two_hundred_and_one_out_blank_out(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    before = written()
    for bad in ("x" * 201, "   ", "​​"):
        refused = void(payment, reason=bad)
        assert not refused.ok and refused.field == "reason", bad
    assert written() == before
    ok = void(payment, reason="y" * 200)
    assert ok.ok
    payment.refresh_from_db()
    assert len(payment.void_reason) == 200


def test_the_reason_is_escaped_on_the_payment_page_and_in_the_held_dialog(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    hostile = '<img src=x onerror="alert(1)"> "quoted" & <b>bold</b>'
    assert void(payment, reason=hostile).ok
    client, _ = client_for(Role.VIEWER)
    body = client.get(detail_path(payment)).content.decode()
    assert "<img src=x" not in body and escape(hostile) in body


# --- The dialog's parameters -----------------------------------------------------------------


def test_a_hostile_return_address_never_reaches_the_keep_link(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    client, _ = client_for(Role.BILLING)
    for query in (
        "?return_url=https://evil.example/",
        "?next=javascript:alert(1)",
        "?return_url=//evil.example",
    ):
        response = client.get(dialog_path(payment) + query)
        assert response.status_code == 200
        body = response.content.decode()
        assert (
            'id="void-manual-keep" href="' + detail_path(payment) + '"'
            in body.replace('class="btn" ', "")
            or f'href="{detail_path(payment)}" id="void-manual-keep"' in body
        )
        assert "evil.example" not in body and "javascript:" not in body


def test_another_stores_query_is_404_on_get_and_post_with_nothing_written(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    other = rent_charge()
    client, _ = client_for(Role.BILLING)
    before = written()
    url = f"{dialog_path(payment)}?store={other.lease.store_id}"
    assert client.get(url).status_code == 404
    assert (
        client.post(url, {"creation_key": str(uuid.uuid4()), "reason": REASON}).status_code == 404
    )
    assert written() == before


def test_get_and_head_write_nothing_and_leave_the_session_alone(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    client, _ = client_for(Role.BILLING)

    def session() -> dict[str, object]:
        return {k: v for k, v in client.session.items() if k != "accounts_last_seen_at"}

    before = written()
    session_before = session()
    assert client.get(dialog_path(payment)).status_code == 200
    assert client.head(dialog_path(payment)).status_code == 200
    assert written() == before and session() == session_before


def test_a_viewer_and_a_signed_out_visitor_get_nothing(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    before = written()
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get(dialog_path(payment)).status_code == 403
    assert viewer.post(dialog_path(payment), {"reason": REASON}).status_code == 403
    from django.test import Client

    anonymous = Client()
    response = anonymous.post(dialog_path(payment), {"reason": REASON})
    assert response.status_code in (302, 401, 403)
    assert written() == before


# --- The method guard ---------------------------------------------------------------------------


def test_a_cash_and_a_check_payment_can_be_voided_an_ach_one_cannot_here(
    fake: FakeProvider,
) -> None:
    charge = rent_charge("1500.00")
    cash = manual(charge, "500.00", method=PaymentMethod.CASH)
    check = manual(charge, "500.00", method=PaymentMethod.CHECK)
    assert refunds.manual_void_refusal(cash) == "" and refunds.manual_void_refusal(check) == ""
    ach = Payment.objects.create(
        rent_charge=charge,
        kind=cash.kind,
        method=PaymentMethod.ACH,
        amount=D("500.00"),
        status=PaymentStatus.SETTLED,
        initiated_on=date(2026, 10, 15),
    )
    assert refunds.manual_void_refusal(ach) == str(refunds.MANUAL_IS_PROCESSOR)
    before = written()
    refused = void(ach)
    assert not refused.ok and written() == before
