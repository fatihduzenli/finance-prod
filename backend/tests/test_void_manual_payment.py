"""The void of a payment recorded outside the processor (docs/qa/PLAN-012f-c §3 D2 to D6,
§6 A): the service, the attention item for an invoice left marked paid, the dialog's
sentences, the dialog over the payment page and its route, the header actions and DETAILS,
the processor void's stamp, and migration 0057's guard lists.

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`; a manual void never
calls it. Nothing opens a socket. Fable's adversarial cases (two voids racing on
PostgreSQL, a void beside a pending refund, a card number in the reason, a hostile
``return_url``, a multi-line invoice's figures) are not here.
"""

from __future__ import annotations

import importlib
import re
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from accounts import registry
from accounts.roles import Role
from ledger.forms import VoidManualForm
from ledger.models import (
    AuditLog,
    InstallmentPlan,
    Lease,
    LeaseCredit,
    OneOffChargeStatus,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import attention, one_off_charges, refunds, rent_run
from ledger.validators import PAYMENT_DATA_MESSAGE, validate_no_payment_data
from ledger.wording import provider_name
from payments import invoice_sync
from payments.fake import FakeProvider
from payments.models import Job
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_invoice,
    an_operator,
)
from tests.test_manual_payment_marking import INVOICE_REF, invoiced, local_invoice, pay
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

D = Decimal
DAY = date(2026, 10, 15)
NOTE = "Wire received from the operator's bank."
REASON = "Recorded twice by mistake; the wire was counted on the other store."


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def billing_user() -> User:
    return client_for(Role.BILLING)[1]


def rent_charge(due: str = "1500.00") -> RentCharge:
    lease = a_lease(a_store(), an_operator("Acme Stores"))
    return a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.DUE, due)


def manual(charge: RentCharge, amount: str, **overrides: Any) -> Payment:
    values: dict[str, Any] = {
        "method": PaymentMethod.WIRE,
        "amount": D(amount),
        "paid_on": DAY,
        "note": NOTE,
        "actor": None,
        "today": DAY,
    }
    values.update(overrides)
    outcome = rent_run.record_manual_payment(charge.pk, **values)
    assert outcome.ok and outcome.payment is not None, outcome.message
    return outcome.payment


def void(payment: Payment, **overrides: Any) -> refunds.Outcome:
    values: dict[str, Any] = {
        "reason": REASON,
        "actor": billing_user(),
        "creation_key": uuid.uuid4(),
    }
    values.update(overrides)
    return refunds.void_manual_payment(payment.pk, **values)


def figures(charge: RentCharge) -> tuple[Decimal, Decimal, str]:
    charge.refresh_from_db()
    return charge.amount_paid, charge.balance, charge.status


def written() -> tuple[int, list[tuple[str, str]], int]:
    """What a refused void must leave as it was: audit rows, payments' states, jobs."""
    states = list(Payment.objects.order_by("id").values_list("status", "void_reason"))
    return AuditLog.objects.count(), states, Job.objects.count()


def dialog_path(payment: Payment) -> str:
    return reverse("ledger:payments:void_manual", kwargs={"pk": payment.pk})


def detail_path(payment: Payment) -> str:
    return reverse("ledger:payments:detail", kwargs={"pk": payment.pk})


# --- The service: figures, status, audit, replay -------------------------------------------


def test_a_void_restores_the_balance_paid_then_partly_paid_then_unpaid(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    first = manual(charge, "1000.00")
    second = manual(charge, "500.00", method=PaymentMethod.ZELLE)
    assert figures(charge) == (D("1500.00"), D("0.00"), RentChargeStatus.PAID)

    assert void(first).ok
    assert figures(charge) == (D("500.00"), D("1000.00"), RentChargeStatus.PARTIAL)
    outcome = void(second)
    assert outcome.ok, outcome.message
    assert figures(charge) == (D("0.00"), D("1500.00"), RentChargeStatus.UNPAID)
    assert outcome.message == refunds.VOIDED_MANUAL.format(amount="$500.00")
    assert fake.calls == []


def test_the_voided_payment_keeps_its_row_marked_void_with_who_when_and_why(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00", method=PaymentMethod.CHECK, reference="1042")
    actor = billing_user()
    assert void(payment, actor=actor, reason=f"  {REASON}\n").ok
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.VOIDED
    assert (payment.void_reason, payment.voided_by) == (REASON, actor)
    assert payment.voided_at is not None
    assert (payment.amount, payment.method, payment.note) == (
        D("1500.00"),
        PaymentMethod.CHECK,
        NOTE,
    )
    assert Payment.objects.filter(pk=payment.pk).exists()


def test_the_audit_row_names_the_actor_the_reason_and_the_charge_before_and_after(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    actor = billing_user()
    key = uuid.uuid4()
    assert void(payment, actor=actor, creation_key=key).ok
    row = AuditLog.objects.get(action=refunds.VOIDED_MANUAL_ACTION)
    assert (row.actor, row.entity_type, row.entity_id) == (actor, "payment", str(payment.pk))
    assert row.before is not None and row.after is not None
    assert (row.before["status"], row.after["status"]) == ("settled", "voided")
    assert row.before["charge"]["status"] == RentChargeStatus.PAID
    assert row.after["charge"]["status"] == RentChargeStatus.UNPAID
    assert (row.after["reason"], row.after["creation_key"]) == (REASON, str(key))
    assert "provider_marking" not in row.after


def test_a_resubmitted_void_finds_its_key_and_writes_nothing_again(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    key = uuid.uuid4()
    assert void(payment, creation_key=key).ok
    before = written()
    again = void(payment, creation_key=key)
    assert (again.ok, again.replayed, again.message) == (True, True, refunds.ALREADY_SUBMITTED_VOID)
    assert written() == before
    assert AuditLog.objects.filter(action=refunds.VOIDED_MANUAL_ACTION).count() == 1


def test_a_voided_payment_cannot_be_voided_twice(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    assert void(payment).ok
    before = written()
    outcome = void(payment)
    assert (outcome.ok, outcome.message) == (False, "This payment is already voided.")
    assert written() == before


def test_a_void_on_a_one_off_charge_reopens_it(fake: FakeProvider) -> None:
    lease = a_lease(a_store(), an_operator("Acme Stores"))
    assert lease.operator is not None
    charge = a_one_off_charge(lease.operator, "300.00", DAY, lease=lease)
    outcome = one_off_charges.record_payment(
        charge.pk,
        method=PaymentMethod.CASH,
        amount=D("300.00"),
        paid_on=DAY,
        note="Cash at the counter.",
        actor=None,
        today=DAY,
    )
    assert outcome.ok and outcome.payment is not None
    charge.refresh_from_db()
    assert charge.status == OneOffChargeStatus.PAID
    assert void(outcome.payment).ok
    charge.refresh_from_db()
    assert (charge.amount_paid, charge.status) == (D("0.00"), OneOffChargeStatus.OWED)


# --- Refusals: nothing written ----------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.VIEWER, None])
def test_below_billing_the_service_refuses_and_writes_nothing(
    fake: FakeProvider, role: Role | None
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    before = written()
    outcome = void(payment, actor=client_for(role)[1])
    assert (outcome.ok, outcome.message) == (False, refunds.VOID_NOT_ALLOWED)
    assert void(payment, actor=None).message == refunds.VOID_NOT_ALLOWED
    assert written() == before


@pytest.mark.parametrize("reason", ["", "   ", "\n\t", "x" * 201])
def test_the_reason_is_required_and_at_most_200_characters(fake: FakeProvider, reason: str) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    before = written()
    outcome = void(payment, reason=reason)
    assert (outcome.ok, outcome.field) == (False, "reason")
    expected = refunds.REASON_TOO_LONG if reason.strip() else refunds.REASON_REQUIRED
    assert outcome.message == expected
    assert written() == before
    assert void(payment, reason="x" * 200).ok


def test_the_reason_is_guarded_text_in_the_form_the_model_and_the_migration() -> None:
    assert validate_no_payment_data in VoidManualForm.base_fields["reason"].validators
    field = Payment._meta.get_field("void_reason")
    assert validate_no_payment_data in field.validators
    assert (field.max_length, field.blank, field.default) == (200, True, "")
    migration = importlib.import_module("ledger.migrations.0057_payment_void_fields")
    previous = importlib.import_module("ledger.migrations.0009_payment_returns_resolution")
    assert migration.GUARDED_COLUMNS == {
        "payments": (*previous.GUARDED_COLUMNS["payments"], "void_reason")
    }


def test_0057_is_in_both_guard_lists() -> None:
    from tests.test_card_number_guard import GUARD_MODULES as CARD_MODULES
    from tests.test_migrations_roundtrip import GUARD_MODULES as ROUND_TRIP_MODULES

    name = "ledger.migrations.0057_payment_void_fields"
    assert name in CARD_MODULES and name in ROUND_TRIP_MODULES


def test_a_processor_payment_is_refused_on_this_route(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = a_payment(charge, "1500.00", DAY)
    before = written()
    outcome = void(payment)
    assert (outcome.ok, outcome.message) == (False, refunds.MANUAL_IS_PROCESSOR)
    assert written() == before
    assert fake.calls == []


def test_a_refund_and_a_deposit_refund_are_refused(fake: FakeProvider) -> None:
    charge = rent_charge()
    parent = a_payment(charge, "1500.00", DAY)
    refund = Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D("100.00"),
        status=PaymentStatus.SETTLED,
        is_refund=True,
        parent_payment=parent,
        settled_on=DAY,
    )
    lease = charge.lease
    assert lease.operator is not None
    deposit = a_one_off_charge(lease.operator, "500.00", DAY, lease=lease)
    deposit_refund = Payment.objects.create(
        one_off_charge=deposit,
        kind=PaymentKind.REFUND_DEPOSIT,
        method=PaymentMethod.WIRE,
        amount=D("500.00"),
        status=PaymentStatus.SETTLED,
        settled_on=DAY,
        note="Deposit paid back by wire.",
    )
    before = written()
    assert void(refund).message == refunds.IS_REFUND
    assert void(deposit_refund).message == refunds.IS_DEPOSIT_REFUND
    assert written() == before


def test_a_credit_applied_a_credit_refund_and_an_insurance_payment_are_refused(
    fake: FakeProvider,
) -> None:
    """Outside D2's charges: a credit's remaining and an insurance charge are not recomputed
    by this void, so it refuses them (method ``credit``, kind ``credit_refund``, no rent or
    one-off charge)."""
    charge = rent_charge()
    lease = charge.lease
    credit = LeaseCredit.objects.create(
        lease=lease,
        amount=D("200.00"),
        remaining=D("0.00"),
        method="wire",
        received_on=DAY,
        note="Left over from the October wire.",
        allocation_group=uuid.uuid4(),
    )
    applied = Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.CREDIT,
        credit=credit,
        amount=D("200.00"),
        status=PaymentStatus.SETTLED,
        settled_on=DAY,
        note="Credit applied.",
    )
    credit_refund = Payment.objects.create(
        kind=PaymentKind.CREDIT_REFUND,
        method=PaymentMethod.WIRE,
        credit=credit,
        amount=D("10.00"),
        status=PaymentStatus.SETTLED,
        settled_on=DAY,
        note="Credit paid back.",
    )
    insurance = an_insurance_charge(lease, 2026, DAY, "600.00")
    on_insurance = Payment.objects.create(
        insurance_charge=insurance,
        kind=PaymentKind.INSURANCE,
        policy_year=2026,
        method=PaymentMethod.ZELLE,
        amount=D("600.00"),
        status=PaymentStatus.SETTLED,
        settled_on=DAY,
        note="Zelle for the premium.",
    )
    before = written()
    assert void(applied).message == refunds.MANUAL_IS_CREDIT
    assert void(credit_refund).message == refunds.MANUAL_CREDIT_REFUND
    assert void(on_insurance).message == refunds.MANUAL_NO_CHARGE
    assert written() == before
    for payment in (applied, credit_refund, on_insurance):
        assert refunds.manual_void_refusal(payment)


@pytest.mark.parametrize("status", [PaymentStatus.PENDING, PaymentStatus.RETURNED])
def test_a_pending_or_returned_manual_payment_is_refused(fake: FakeProvider, status: str) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    Payment.objects.filter(pk=payment.pk).update(status=status)
    before = written()
    outcome = void(payment)
    assert outcome.message == refunds.MANUAL_NOT_SETTLED.format(status=status)
    assert written() == before


# --- D3: the invoice marked paid by this payment ----------------------------------------------


def marked_items() -> list[attention.Item]:
    found = attention.attention(DAY, limit=None)
    return [item for item in found.items if item.kind == attention.INVOICE_MARKED_PAID_VOID]


def test_a_marked_invoice_is_named_and_flagged_until_the_refresh_shows_it_open(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake)
    assert pay(charge, "1500.00").ok
    payment = Payment.objects.get()
    stored = local_invoice(charge)
    assert stored.provider_status == invoice_sync.PROVIDER_PAID
    number = stored.invoice_number
    sentence = refunds.VOID_MANUAL_MARKED.format(number=number)
    assert sentence in refunds.void_manual_facts(payment)
    assert marked_items() == []
    fake.calls.clear()

    outcome = void(payment)

    assert outcome.ok and fake.calls == []
    assert outcome.message.endswith(refunds.VOIDED_MANUAL_UNMARK.format(number=number))
    row = AuditLog.objects.get(action=refunds.VOIDED_MANUAL_ACTION)
    assert row.after is not None
    assert row.after["provider_marking"] == "to unmark"
    assert (row.after["marked_invoice"], row.after["invoice_number"]) == (str(stored.pk), number)
    [item] = marked_items()
    assert item.severity == attention.HIGH
    assert item.text == attention.MARKED_PAID_VOID_TEXT.format(number=number)
    # PLAN-012f-c §11 E1: the owner's one line.
    unmark = f"Unmark invoice {number} in {provider_name()} by hand; this payment was voided"
    assert item.text == unmark
    assert item.url == reverse("ledger:charges:detail", kwargs={"pk": charge.pk})
    assert item.amount == D("1500.00")
    assert [
        i.kind
        for i in attention.attention(DAY, operator=charge.lease.operator_id).items
        if i.kind == item.kind
    ] == [item.kind]
    stored.refresh_from_db()
    assert stored.provider_status == invoice_sync.PROVIDER_PAID

    # Still paid on a refresh: still listed. Open again: gone by itself.
    invoice_sync.record_provider_state(stored.pk, fake.invoices[INVOICE_REF])
    assert len(marked_items()) == 1
    invoice_sync.record_provider_state(
        stored.pk, replace(fake.invoices[INVOICE_REF], raw_status="SENT")
    )
    assert marked_items() == []


def test_without_a_successful_marking_there_is_no_item_and_no_sentence(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake)
    assert pay(charge, "1000.00").ok  # partial: never marked
    payment = Payment.objects.get()
    assert refunds.marked_invoice(payment) is None
    assert len(refunds.void_manual_facts(payment)) == 1
    assert void(payment).ok
    row = AuditLog.objects.get(action=refunds.VOIDED_MANUAL_ACTION)
    assert row.after is not None and "provider_marking" not in row.after
    assert marked_items() == []


# --- D4: the dialog's sentences -------------------------------------------------------------


def a_plan(charge: RentCharge, created_at: datetime) -> InstallmentPlan:
    lease: Lease = charge.lease
    assert lease.operator is not None
    plan = InstallmentPlan.objects.create(
        rent_charge=charge,
        operator=lease.operator,
        lease=lease,
        count=2,
        first_due=date(2026, 11, 1),
        cadence="monthly",
        collection="manual",
        status="cancelled",
        cancelled_at=created_at,
        note="Stopped.",
        confirmed_at=created_at,
    )
    InstallmentPlan.objects.filter(pk=plan.pk).update(created_at=created_at)
    return plan


def test_the_consequence_sentence_names_the_amount_and_the_charge(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    assert refunds.void_manual_facts(payment) == [
        "Voiding returns $1,500.00 to Rent Oct 2026; the record stays, marked void."
    ]


def test_the_installment_sentence_only_for_a_plan_created_after_the_payment(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    plan_sentence = refunds.VOID_MANUAL_PLAN.format(amount="$1,500.00")
    a_plan(charge, datetime(2026, 10, 1, 12, tzinfo=UTC))
    assert plan_sentence not in refunds.void_manual_facts(payment)
    InstallmentPlan.objects.all().delete()
    a_plan(charge, datetime(2026, 10, 20, 12, tzinfo=UTC))
    assert refunds.void_manual_facts(payment)[-1] == plan_sentence
    assert plan_sentence == (
        "The installment plan was built on the remaining balance; voiding adds $1,500.00 back "
        "to the charge, not to the plan."
    )


def test_the_installment_sentence_falls_back_to_the_initiated_day(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    Payment.objects.filter(pk=payment.pk).update(settled_on=None, initiated_on=date(2026, 10, 25))
    payment.refresh_from_db()
    a_plan(charge, datetime(2026, 10, 20, 12, tzinfo=UTC))
    assert len(refunds.void_manual_facts(payment)) == 1


# --- The processor void stamps who and when too -------------------------------------------------


def test_the_processor_void_stamps_who_and_when(fake: FakeProvider) -> None:
    from tests.test_refunds_voids import ach
    from tests.test_refunds_voids import void as processor_void
    from tests.test_webhook_handlers import make_world

    make_world(fake)
    payment = ach(fake, "1500.00")
    actor = client_for(Role.ADMIN)[1]
    assert processor_void(payment, actor=actor).ok
    payment.refresh_from_db()
    assert (payment.status, payment.voided_by, payment.void_reason) == (
        PaymentStatus.VOIDED,
        actor,
        "",
    )
    assert payment.voided_at is not None
    row = AuditLog.objects.get(action=refunds.VOIDED_ACTION)
    assert row.after is not None and "voided_at" not in row.after


# --- The route, the dialog, the header and DETAILS -----------------------------------------------


def test_the_route_is_billing_post_in_the_registry() -> None:
    rule = registry.rule_for("ledger:payments:void_manual")
    assert rule is not None and rule.min_role == Role.BILLING
    assert rule.methods == frozenset({"POST"})
    assert dialog_path_for_pk("x") == "/payments/x/void-manual/"


def dialog_path_for_pk(pk: str) -> str:
    return reverse("ledger:payments:void_manual", kwargs={"pk": pk})


def test_billing_voids_through_the_dialog_and_lands_on_the_payment_page(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    client: Client = client_for(Role.BILLING)[0]
    page = client.get(dialog_path(payment)).content.decode()
    key = re.search(r'name="creation_key" value="([^"]+)"', page)
    assert key is not None
    response = client.post(
        dialog_path(payment), {"creation_key": key.group(1), "reason": REASON}, follow=True
    )
    assert response.redirect_chain == [(detail_path(payment), 302)]
    body = response.content.decode()
    assert "Payment voided: $1,500.00 is owed on the charge again." in body
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.VOIDED
    assert figures(charge)[2] == RentChargeStatus.UNPAID


def test_an_admin_may_void_too(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    client = client_for(Role.ADMIN)[0]
    response = client.post(dialog_path(payment), {"creation_key": uuid.uuid4(), "reason": REASON})
    assert response.status_code == 302
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.VOIDED


def test_a_viewer_gets_403_on_get_and_post_and_nothing_is_written(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    client = client_for(Role.VIEWER)[0]
    before = written()
    assert client.get(dialog_path(payment)).status_code == 403
    response = client.post(dialog_path(payment), {"creation_key": uuid.uuid4(), "reason": REASON})
    assert response.status_code == 403
    assert written() == before


def test_the_dialogs_get_and_keep_write_nothing(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    client = client_for(Role.BILLING)[0]
    before = written()
    body = client.get(dialog_path(payment)).content.decode()
    assert client.head(dialog_path(payment)).status_code == 200
    keep = re.search(
        r'<a class="btn" href="([^"]+)" id="void-manual-keep">Keep the payment</a>', body
    )
    assert keep is not None and keep.group(1) == detail_path(payment)
    assert client.get(keep.group(1)).status_code == 200
    assert written() == before


def test_a_missing_reason_comes_back_on_the_dialog_with_nothing_written(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    client = client_for(Role.BILLING)[0]
    before = written()
    response = client.post(dialog_path(payment), {"creation_key": uuid.uuid4(), "reason": "  "})
    assert response.status_code == 302 and response["Location"] == dialog_path(payment)
    body = client.get(dialog_path(payment)).content.decode()
    assert "A reason is required: say why the payment is voided." in body
    assert written() == before


def test_a_reason_refused_for_payment_data_never_comes_back_but_its_error_does(
    fake: FakeProvider,
) -> None:
    """Rule 1 (the B3-1 class): the held dialog keeps the error, not the value."""
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    client = client_for(Role.BILLING)[0]
    routing, account = "021000021", "000123456789"
    reason = f"Routing {routing} account {account} was wrong"
    before = written()
    response = client.post(dialog_path(payment), {"creation_key": uuid.uuid4(), "reason": reason})
    assert response.status_code == 302 and response["Location"] == dialog_path(payment)
    body = client.get(dialog_path(payment)).content.decode()
    assert PAYMENT_DATA_MESSAGE in body
    assert not has_token(body, routing) and not has_token(body, account)
    assert re.search(r'<textarea name="reason"[^>]*>\s*</textarea>', body)
    assert written() == before


def test_a_reason_refused_for_its_length_keeps_its_value(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    client = client_for(Role.BILLING)[0]
    reason = "Recorded twice " * 20
    client.post(dialog_path(payment), {"creation_key": uuid.uuid4(), "reason": reason})
    body = client.get(dialog_path(payment)).content.decode()
    assert "Recorded twice Recorded twice" in body


def test_a_refused_void_is_flashed_on_the_payment_page(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = a_payment(charge, "1500.00", DAY)
    client = client_for(Role.BILLING)[0]
    before = written()
    response = client.post(
        dialog_path(payment), {"creation_key": uuid.uuid4(), "reason": REASON}, follow=True
    )
    assert response.redirect_chain == [(detail_path(payment), 302)]
    assert str(refunds.MANUAL_IS_PROCESSOR) in response.content.decode()
    assert written() == before


def test_another_stores_query_is_a_404(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    client = client_for(Role.BILLING)[0]
    other = a_store()
    assert client.get(f"{dialog_path(payment)}?store={other.pk}").status_code == 404
    response = client.post(
        f"{dialog_path(payment)}?store={other.pk}",
        {"creation_key": uuid.uuid4(), "reason": REASON},
    )
    assert response.status_code == 404
    own = charge.lease.store_id
    assert client.get(f"{dialog_path(payment)}?store={own}").status_code == 200
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.SETTLED


def foot(body: str) -> str:
    [found] = re.findall(r'<div class="dialog-foot">(.*?)</div>', body, re.S)
    return str(found)


def test_the_dialog_is_over_the_payment_page_with_one_red_primary(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    body = client_for(Role.BILLING)[0].get(dialog_path(payment)).content.decode()
    assert '<dialog class="dialog dialog-sm" id="void-manual-dialog"' in body
    assert f'data-return-url="{detail_path(payment)}"' in body
    assert 'id="payment-details"' in body  # the payment page behind
    assert "Voiding returns $1,500.00 to Rent Oct 2026; the record stays, marked void." in body
    assert 'name="reason"' in body
    buttons = foot(body)
    assert len(re.findall(r'class="btn btn-(?:primary|danger)"', buttons)) == 1
    assert (
        '<button class="btn btn-danger" type="submit" id="void-manual-submit">Void payment</button>'
        in buttons
    )


def test_the_dialog_of_a_voided_payment_shows_the_refusal_and_keep_alone(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    assert void(payment).ok
    body = client_for(Role.BILLING)[0].get(dialog_path(payment)).content.decode()
    assert "This payment is already voided." in body
    assert 'type="submit"' not in foot(body)


def test_the_header_offers_void_payment_to_billing_on_a_manual_settled_payment(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    item = (
        f'<a class="menu-danger" href="{dialog_path(payment)}" id="void-manual-payment">'
        "Void payment</a>"
    )
    assert item in client_for(Role.BILLING)[0].get(detail_path(payment)).content.decode()
    assert item in client_for(Role.ADMIN)[0].get(detail_path(payment)).content.decode()
    viewer = client_for(Role.VIEWER)[0].get(detail_path(payment)).content.decode()
    assert 'id="void-manual-payment"' not in viewer and 'id="payment-actions"' not in viewer


def test_a_processor_payment_keeps_refund_and_void_for_the_admin_only(fake: FakeProvider) -> None:
    charge = rent_charge()
    payment = a_payment(charge, "1500.00", DAY)
    billing = client_for(Role.BILLING)[0].get(detail_path(payment)).content.decode()
    assert 'id="void-manual-payment"' not in billing and 'id="void-payment"' not in billing


def test_a_voided_payment_has_no_actions_and_shows_who_when_and_the_reason(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    actor = billing_user()
    assert void(payment, actor=actor).ok
    for role in (Role.BILLING, Role.ADMIN):
        body = client_for(role)[0].get(detail_path(payment)).content.decode()
        assert 'id="payment-actions"' not in body and 'id="more-card"' not in body
        assert has_token(body, "Voided")
        voided = re.search(r'<dd id="summary-voided">(.*?)</dd>', body)
        # PLAN-012f-c D11: the staff name with the e-mail as its title.
        shown = f'<span title="{actor.email}">{actor.username}</span>'
        assert voided is not None and voided.group(1).endswith(f"by {shown}")
        assert f'<dd id="summary-void-reason" style="white-space:pre-wrap;">{REASON}</dd>' in body


def test_the_charge_page_shows_the_voided_payment_as_a_voided_processor_payment(
    fake: FakeProvider,
) -> None:
    charge = rent_charge()
    payment = manual(charge, "1500.00")
    assert void(payment).ok
    body = (
        client_for(Role.VIEWER)[0]
        .get(reverse("ledger:charges:detail", kwargs={"pk": charge.pk}))
        .content.decode()
    )
    row = body[body.index(detail_path(payment)) :]
    row = row[: row.index("</tr>")]
    assert "Voided" in row


def test_the_processor_void_dialogs_button_is_red(fake: FakeProvider) -> None:
    from tests.test_refunds_voids import ach
    from tests.test_webhook_handlers import make_world

    make_world(fake)
    payment = ach(fake, "1500.00")
    body = (
        client_for(Role.ADMIN)[0]
        .get(reverse("ledger:payments:void", kwargs={"pk": payment.pk}))
        .content.decode()
    )
    assert 'class="btn btn-danger" type="submit" id="confirm-void-button"' in body
    refund = (
        client_for(Role.ADMIN)[0]
        .get(reverse("ledger:payments:refund", kwargs={"pk": payment.pk}))
        .content.decode()
    )
    assert 'class="btn btn-primary" type="submit" id="confirm-refund-button"' in refund


def test_an_invoice_helper_for_the_attention_list(fake: FakeProvider) -> None:
    """:func:`marked_paid_void` (used by tests/test_attention.py's every-kind build) makes
    one item."""
    lease = a_lease(a_store(), an_operator("Acme Stores"))
    marked_paid_void(lease, 9001)
    assert len(marked_items()) == 1


def marked_paid_void(lease: Lease, number: int) -> Payment:
    """A manual payment voided while the invoice it marked paid still reads paid (D3),
    written as the service writes it."""
    from payments.sync_writer import provider_write

    assert lease.operator is not None
    charge = a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, "1500.00")
    with provider_write():
        invoice = an_invoice(
            lease.operator, number, "1500.00", rent_charge=charge, provider_status="PAID"
        )
    payment = Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.WIRE,
        amount=D("1500.00"),
        status=PaymentStatus.VOIDED,
        settled_on=date(2026, 9, 5),
        note=NOTE,
        void_reason=REASON,
    )
    AuditLog.objects.create(
        action=refunds.VOIDED_MANUAL_ACTION,
        entity_type="payment",
        entity_id=str(payment.pk),
        before={},
        after={
            "provider_marking": refunds.PROVIDER_MARKING_TO_UNMARK,
            "marked_invoice": str(invoice.pk),
            "invoice_number": invoice.invoice_number,
        },
    )
    return payment
