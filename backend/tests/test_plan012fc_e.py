"""PLAN-012f-c slice E (docs/qa/PLAN-012f-c-staging-review-fixes.md §11): the owner's
checkpoint decisions of 2026-10-27.

- E1: the Needs review item of a voided payment whose processor invoice stays marked paid
  reads "Unmark invoice {number} in {provider} by hand; this payment was voided".
- E2: the greyed bank-debit card of Add payment, no bank method on file, reads "No bank
  account on file for {operator}." with an "Operator page" link after it, and "Bank debits
  are also off at {provider}." under it when the flag is off as well; the dashboard
  instruction is in the Payments help topic.
- E3: the page of a payment recorded outside the processor shows Amount and Applied to and
  the "Paid outside" line; processor payments and refunds keep their tiles.

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`; nothing opens a socket.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.html import escape

from accounts.roles import Role
from ledger.models import (
    CompanySettings,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import attention, installments, refunds, rent_run
from ledger.services import one_off_charges as service
from ledger.views.payments import _applied_tile
from payments.fake import FakeProvider
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_insurance_charge
from tests.receivables_helpers import an_operator as a_plain_operator
from tests.test_add_payment_page import element, page_of, tiles
from tests.test_bank_debit import an_operator_with_bank, enabled, fake, invoiced, today
from tests.test_manual_payment_marking import invoiced as invoiced_rent
from tests.test_manual_payment_marking import local_invoice, pay
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_refunds_voids import ach
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_void_manual_payment import marked_items
from tests.test_webhook_handlers import make_world

pytestmark = pytest.mark.django_db

__all__ = ["enabled", "fake"]  # the fixtures of tests.test_bank_debit, used here

D = Decimal
PROVIDER = "PayCo"
OCT = date(2026, 10, 1)


def name_provider(name: str = PROVIDER) -> None:
    row = CompanySettings.current()
    row.provider_name = name
    row.save()


def body_of(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return str(response.content.decode())


def admin_get(path: str) -> str:
    return body_of(client_for(Role.ADMIN)[0], path)


def operator_page(operator: Operator) -> str:
    return reverse("ledger:operators:detail", kwargs={"pk": operator.pk})


def link_to(operator: Operator) -> str:
    return (
        f'<a class="collect-link" id="debit-reason-link" href="{operator_page(operator)}">'
        "Operator page</a>"
    )


def named(operator: Operator, name: str) -> Operator:
    Operator.objects.filter(pk=operator.pk).update(display_name=name)
    return Operator.objects.get(pk=operator.pk)


def rent_page(charge_pk: object) -> str:
    return reverse("ledger:charges:record_payment", kwargs={"pk": charge_pk})


def payment_page(payment: Payment) -> str:
    return admin_get(reverse("ledger:payments:detail", kwargs={"pk": payment.pk}))


def tile_row(html: str) -> str:
    return element(html, '<section class="kpi-row detail-tiles"', "section")


def tile_labels(html: str) -> list[str]:
    return re.findall(r'<span class="kpi-label">(.*?)</span>', tile_row(html))


def a_rent_charge_of(name: str = "Rent Ops", due: str = "1500.00") -> RentCharge:
    lease = a_lease(a_store("1991 Istanbul", 1991), a_plain_operator(name))
    return a_rent_charge(lease, OCT, RentChargeStatus.DUE, due)


def a_zelle_payment(amount: str = "500.00", due: str = "1500.00") -> Payment:
    charge = a_rent_charge_of(due=due)
    outcome = rent_run.record_manual_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=D(amount),
        paid_on=date(2026, 10, 15),
        note="Zelle received.",
        actor=client_for(Role.BILLING)[1],
        today=date(2026, 10, 15),
    )
    assert outcome.ok and outcome.payment is not None, outcome.message
    return outcome.payment


# --- E1: the Needs review sentence ------------------------------------------------------------


def test_the_needs_review_item_reads_the_owners_one_line(fake: FakeProvider) -> None:
    name_provider()
    charge = invoiced_rent(fake)
    assert pay(charge, "1500.00").ok
    payment = Payment.objects.get()
    number = local_invoice(charge).invoice_number
    outcome = refunds.void_manual_payment(
        payment.pk, reason="Recorded twice.", actor=client_for(Role.BILLING)[1]
    )
    assert outcome.ok, outcome.message
    [item] = marked_items()
    assert item.text == f"Unmark invoice {number} in PayCo by hand; this payment was voided"
    # The kind, its priority and its link are unchanged.
    assert item.kind == attention.INVOICE_MARKED_PAID_VOID
    assert item.severity == attention.HIGH
    assert item.url == reverse("ledger:charges:detail", kwargs={"pk": charge.pk})


def test_the_sentence_names_the_processor_from_the_settings() -> None:
    name_provider("Other Pay")
    text = attention.MARKED_PAID_VOID_TEXT.format(number="INV-1")
    assert text == "Unmark invoice INV-1 in Other Pay by hand; this payment was voided"
    assert "dashboard" not in text and "—" not in text


# --- E2: the greyed card's sentence and link -----------------------------------------------


def test_no_bank_with_the_flag_on_reads_the_sentence_and_links_the_operator(
    fake: FakeProvider,
) -> None:
    operator = named(an_operator(fake, bank=None), "Fatih Co")
    charge = invoiced(fake, operator=operator)
    options = service.bank_debit_options(charge)
    assert options.reason == "No bank account on file for Fatih Co."
    assert options.reason_more == ""
    assert options.reason_url == operator_page(operator)
    card = tiles(admin_get(page_of(charge)))["debit"]
    first = (
        '<span class="collect-note unavailable-reason" id="debit-reason">'
        "No bank account on file for Fatih Co.</span>"
    )
    assert first + link_to(operator) + "</span>" in card
    assert "debit-reason-more" not in card
    assert "dashboard" not in card and "Update from" not in card


def test_no_bank_with_the_flag_off_adds_the_second_line_after_the_link(
    fake: FakeProvider,
) -> None:
    name_provider()
    operator = named(an_operator(fake, bank=None), "Fatih Co")
    charge = invoiced(fake, operator=operator)
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        options = service.bank_debit_options(charge)
        card = tiles(admin_get(page_of(charge)))["debit"]
    assert (options.reason, options.reason_more, options.reason_url) == (
        "No bank account on file for Fatih Co.",
        "Bank debits are also off at PayCo.",
        operator_page(operator),
    )
    second = (
        '<span class="collect-note" id="debit-reason-more">'
        "Bank debits are also off at PayCo.</span>"
    )
    assert "No bank account on file for Fatih Co.</span>" + link_to(operator) + second in card


@pytest.mark.parametrize("flag", [True, False])
def test_a_bank_on_file_has_no_link(fake: FakeProvider, flag: bool) -> None:
    name_provider()
    charge = invoiced(fake, operator=an_operator_with_bank(fake))
    with override_settings(STAX_PAY_INVOICE_ENABLED=flag):
        options = service.bank_debit_options(charge)
        card = tiles(admin_get(page_of(charge)))["debit"]
    assert options.reason_url == ""
    assert options.reason == ("" if flag else "Bank debits are off at PayCo.")
    assert "debit-reason-link" not in card and "Operator page" not in card


def test_an_earlier_reason_has_no_link(fake: FakeProvider) -> None:
    charge = invoiced(fake, "60.00", operator=an_operator(fake, bank=None))
    outcome = service.record_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=D("60.00"),
        paid_on=today(),
        note="Paid in full by Zelle.",
        actor=client_for(Role.BILLING)[1],
    )
    assert outcome.ok, outcome.message
    options = service.bank_debit_options(charge)
    assert (options.reason, options.reason_url) == (service.DEBIT_PAID, "")


@pytest.mark.parametrize("flag", [True, False])
def test_a_rent_charge_reads_the_same_sentence_and_link(flag: bool) -> None:
    name_provider()
    charge = a_rent_charge_of("Rent Ops")
    operator = charge.lease.operator
    assert operator is not None
    with override_settings(STAX_PAY_INVOICE_ENABLED=flag):
        options = service.rent_debit_options(charge)
        card = tiles(admin_get(rent_page(charge.pk)))["debit"]
    assert options.reason == "No bank account on file for Rent Ops."
    assert options.reason_more == ("" if flag else "Bank debits are also off at PayCo.")
    assert options.reason_url == operator_page(operator)
    assert link_to(operator) in card
    assert ("debit-reason-more" in card) is not flag


def test_without_an_operator_the_sentence_says_the_operator_and_has_no_link() -> None:
    name_provider()
    assert service._setup_reasons(True, None, None) == (
        "No bank account on file for the operator.",
        "",
        "",
    )
    assert service._setup_reasons(False, None, None) == (
        "No bank account on file for the operator.",
        "Bank debits are also off at PayCo.",
        "",
    )


def test_a_hostile_operator_name_is_escaped_beside_the_link(fake: FakeProvider) -> None:
    operator = named(an_operator(fake, bank=None), '<script>alert("x")</script>')
    charge = invoiced(fake, operator=operator)
    card = tiles(admin_get(page_of(charge)))["debit"]
    assert "<script>" not in card
    assert escape('No bank account on file for <script>alert("x")</script>.') in card
    assert link_to(operator) in card


def test_the_link_is_on_the_debit_card_only(fake: FakeProvider) -> None:
    charge = invoiced(fake, operator=an_operator(fake, bank=None))
    cards = tiles(admin_get(page_of(charge)))
    assert "debit-reason-link" in cards["debit"]
    assert "debit-reason-link" not in cards["record"]


def test_the_old_send_sentence_is_gone_and_the_builder_keeps_its_own() -> None:
    assert not hasattr(service, "DEBIT_NO_METHOD_SEND")
    # PLAN-013a C3: How to collect greys its tiles with the Add payment card's sentence too; the
    # dashboard instruction lives on the Payments help topic and no constant carries it.
    assert not hasattr(service, "DEBIT_NO_METHOD")
    name_provider()
    assert service.DEBIT_NO_ACCOUNT == "No bank account on file for {operator}."


def test_the_payments_help_topic_says_how_to_add_a_bank_account() -> None:
    name_provider()
    html = body_of(
        client_for(Role.VIEWER)[0], reverse("finance:help_topic", kwargs={"slug": "payments"})
    )
    assert '<h3 id="help-payments-bank-debit">Bank debits</h3>' in html
    assert (
        '<p id="help-bank-debit">When the operator has no bank account on file, Add payment '
        "greys \"Charge the operator's bank account\" and links the operator's "
        "page: add one in the PayCo dashboard, then Update from PayCo.</p>"
    ) in html


# --- E3: the tiles of a payment recorded outside the processor ------------------------------


def test_a_zelle_payment_shows_amount_and_applied_to_and_the_line() -> None:
    name_provider()
    payment = a_zelle_payment("500.00")
    html = payment_page(payment)
    row = tile_row(html)
    assert tile_labels(html) == ["Amount", "Applied to"]
    assert (
        '<div id="tile-applied"><div class="kpi"><span class="kpi-label">Applied to</span>'
        '<span class="kpi-value">$500.00</span><span class="kpi-note">Rent Oct 2026</span>'
        "</div></div>"
    ) in row
    assert 'id="tile-refunded"' not in row and 'id="tile-remaining"' not in row
    line = (
        '<p class="muted" id="paid-outside">Paid outside PayCo: void it to undo, it cannot be '
        "refunded here.</p>"
    )
    assert html.index(line) > html.index(row)


def test_a_manual_payment_on_a_one_off_charge_names_the_charge(fake: FakeProvider) -> None:
    charge = invoiced(fake, "60.00")
    outcome = service.record_payment(
        charge.pk,
        method=PaymentMethod.CHECK,
        amount=D("40.00"),
        paid_on=today(),
        note="Check received.",
        actor=client_for(Role.BILLING)[1],
    )
    assert outcome.ok, outcome.message
    payment = Payment.objects.get(one_off_charge=charge)
    row = tile_row(payment_page(payment))
    label = installments.parent_screen_label(charge)
    assert label
    assert (
        f'<span class="kpi-value">$40.00</span><span class="kpi-note">{escape(label)}</span>' in row
    )


def test_an_ach_payment_keeps_amount_refunded_remaining(fake: FakeProvider) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    html = payment_page(payment)
    assert tile_labels(html) == ["Amount", "Refunded", "Remaining"]
    assert 'id="tile-applied"' not in html and 'id="paid-outside"' not in html


def test_a_voided_manual_payment_keeps_the_two_tiles_and_the_line() -> None:
    name_provider()
    payment = a_zelle_payment("500.00")
    outcome = refunds.void_manual_payment(
        payment.pk, reason="Recorded twice.", actor=client_for(Role.BILLING)[1]
    )
    assert outcome.ok, outcome.message
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.VOIDED
    html = payment_page(payment)
    assert tile_labels(html) == ["Amount", "Applied to"]
    row = tile_row(html)
    assert (
        '<div id="tile-amount"><div class="kpi"><span class="kpi-label">Amount</span>'
        '<span class="kpi-value">$500.00</span>'
    ) in row
    # The charge owes the voided amount again: the payment applies nothing to it.
    assert (
        '<div id="tile-applied"><div class="kpi"><span class="kpi-label">Applied to</span>'
        '<span class="kpi-value">$0.00</span><span class="kpi-note">Rent Oct 2026 · voided</span>'
        "</div></div>"
    ) in row
    assert _applied_tile(payment) == {"label": "Rent Oct 2026 · voided", "amount": D("0.00")}
    assert 'id="paid-outside">Paid outside PayCo: void it to undo' in html


def test_applied_is_the_whole_amount_of_a_partial_payment() -> None:
    payment = a_zelle_payment("500.00", due="1500.00")
    tile = _applied_tile(payment)
    assert tile == {"label": "Rent Oct 2026", "amount": D("500.00")}


def test_refunds_money_paid_back_and_payments_on_no_charge_keep_todays_tiles() -> None:
    charge = a_rent_charge_of()
    lease = charge.lease
    zelle = {"method": PaymentMethod.ZELLE, "amount": D("10.00")}
    refund = Payment(rent_charge=charge, kind=PaymentKind.RENT, is_refund=True, **zelle)
    deposit_back = Payment(rent_charge=charge, kind=PaymentKind.REFUND_DEPOSIT, **zelle)
    credit_back = Payment(rent_charge=charge, kind=PaymentKind.CREDIT_REFUND, **zelle)
    insurance = Payment(
        insurance_charge=an_insurance_charge(lease, 2026, OCT),
        kind=PaymentKind.INSURANCE,
        **zelle,
    )
    ach_payment = Payment(
        rent_charge=charge, kind=PaymentKind.RENT, method=PaymentMethod.ACH, amount=D("10.00")
    )
    for payment in (refund, deposit_back, credit_back, insurance, ach_payment):
        assert _applied_tile(payment) is None, payment.kind


def test_a_hostile_charge_label_is_escaped_in_the_tile(fake: FakeProvider) -> None:
    charge = invoiced(fake, "60.00")
    type(charge).objects.filter(pk=charge.pk).update(description="<b>Damage</b>")
    outcome = service.record_payment(
        charge.pk,
        method=PaymentMethod.CASH,
        amount=D("60.00"),
        paid_on=today(),
        note="Cash received.",
        actor=client_for(Role.BILLING)[1],
    )
    assert outcome.ok, outcome.message
    payment = Payment.objects.get(one_off_charge=charge)
    row = tile_row(payment_page(payment))
    assert "<b>Damage</b>" not in row
    assert escape("<b>Damage</b>") in row
