"""PLAN-012f-c Batch B (docs/qa/PLAN-012f-c-staging-review-fixes.md §3 D7–D11, §6 B): the
greyed bank-debit card's two lines, the Add payment badge, the Reference help and the help
topic "Payments", the held form's guard, the ``phone`` filter and its sites, and staff names
on the "Recorded by" / "by {user}" cells, and the partial-payment notice (D12).

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`; nothing opens a socket.
D12: the payment page's partial-payment notice, apart from the review reasons.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.html import escape

from accounts.display import staff_name
from accounts.roles import Role
from finance import help as help_pages
from finance.templatetags.people import staff
from finance.templatetags.phones import phone, phone_text
from ledger.forms import ADD_PAYMENT_REFERENCE_HELP, REFERENCE_HELP, AddPaymentForm
from ledger.models import (
    AuditLog,
    CompanySettings,
    Operator,
    Payment,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
)
from ledger.services import detail_cards, refunds, rent_run
from ledger.services import one_off_charges as service
from ledger.validators import contains_card_number, validate_no_payment_data
from ledger.views.operators import HELD_FORM_KEY
from payments import offline_marking
from payments.fake import FakeProvider
from tests.receivables_helpers import a_lease, a_rent_charge, a_store
from tests.receivables_helpers import an_operator as a_plain_operator
from tests.test_add_payment_page import element, page_of, tiles
from tests.test_bank_debit import an_operator_with_bank, enabled, fake, invoiced, today
from tests.test_manual_payment_marking import invoiced as invoiced_rent
from tests.test_manual_payment_marking import pay
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_refunds_voids import ach
from tests.test_refunds_voids import void as processor_void
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import make_world

pytestmark = pytest.mark.django_db

__all__ = ["enabled", "fake"]  # the fixtures of tests.test_bank_debit, used here

D = Decimal
PROVIDER = "PayCo"
#: A routing number that passes the ABA checksum, beside an account number: the forms'
#: guard refuses it; the card-number detector alone does not see it.
ROUTING_AND_ACCOUNT = "routing 021000021 account 123456789012"


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


def rent_page(charge_pk: object) -> str:
    return reverse("ledger:charges:record_payment", kwargs={"pk": charge_pk})


def named_user(username: str, first: str = "", last: str = "", email: str = "") -> User:
    user = client_for(Role.BILLING, username)[1]
    user.first_name, user.last_name = first, last
    user.email = email
    user.save()
    return user


# --- D7: the greyed card's reasons --------------------------------------------------------


def test_no_bank_and_the_flag_off_give_the_owners_two_lines(fake: FakeProvider) -> None:
    name_provider()
    operator = an_operator(fake, bank=None)
    Operator.objects.filter(pk=operator.pk).update(display_name="Fatih Co")
    charge = invoiced(fake, operator=Operator.objects.get(pk=operator.pk))
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        options = service.bank_debit_options(charge)
    assert options.reason == "No bank account on file — send Fatih Co the PayCo link to add one"
    assert options.reason_more == "Bank debits are also off at PayCo."
    assert not options.allowed


@pytest.mark.parametrize(
    ("bank", "flag", "reason"),
    [
        (False, True, service.DEBIT_NO_METHOD),
        (True, False, service.DEBIT_OFF),
        (True, True, ""),
    ],
)
def test_one_reason_or_none_keeps_one_line(
    fake: FakeProvider, bank: bool, flag: bool, reason: object
) -> None:
    operator = an_operator_with_bank(fake) if bank else an_operator(fake, bank=None)
    charge = invoiced(fake, operator=operator)
    with override_settings(STAX_PAY_INVOICE_ENABLED=flag):
        options = service.bank_debit_options(charge)
    assert options.reason == str(reason)
    assert options.reason_more == ""


def test_an_earlier_reason_wins_and_has_no_second_line(fake: FakeProvider) -> None:
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
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        options = service.bank_debit_options(charge)
    assert (options.reason, options.reason_more) == (service.DEBIT_PAID, "")


def test_a_rent_charge_gets_the_two_lines_too() -> None:
    name_provider()
    lease = a_lease(a_store("1991 Istanbul", 1991), a_plain_operator("Rent Ops"))
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.UNPAID, "2000.00")
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        both = service.rent_debit_options(charge)
    assert both.reason == "No bank account on file — send Rent Ops the PayCo link to add one"
    assert both.reason_more == "Bank debits are also off at PayCo."
    with override_settings(STAX_PAY_INVOICE_ENABLED=True):
        one = service.rent_debit_options(charge)
    assert (one.reason, one.reason_more) == (str(service.DEBIT_NO_METHOD), "")


def test_a_rent_charge_with_a_bank_and_the_flag_off_keeps_one_line(fake: FakeProvider) -> None:
    lease = a_lease(a_store(), an_operator_with_bank(fake))
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.UNPAID, "2000.00")
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        options = service.rent_debit_options(charge)
    assert (options.reason, options.reason_more) == (str(service.DEBIT_OFF), "")


def test_the_page_prints_the_second_line_muted_under_the_first(fake: FakeProvider) -> None:
    name_provider()
    operator = an_operator(fake, bank=None)
    Operator.objects.filter(pk=operator.pk).update(display_name="Fatih <Co>")
    charge = invoiced(fake, operator=Operator.objects.get(pk=operator.pk))
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        card = tiles(admin_get(page_of(charge)))["debit"]
    first = (
        '<span class="collect-note unavailable-reason" id="debit-reason">No bank account on '
        "file — send Fatih &lt;Co&gt; the PayCo link to add one</span>"
    )
    second = (
        '<span class="collect-note" id="debit-reason-more">'
        "Bank debits are also off at PayCo.</span>"
    )
    assert first + second in card
    assert "Fatih <Co>" not in card


def test_one_reason_on_the_page_has_no_second_line(fake: FakeProvider) -> None:
    charge = invoiced(fake, operator=an_operator(fake, bank=None))
    card = tiles(admin_get(page_of(charge)))["debit"]
    assert escape(str(service.DEBIT_NO_METHOD)) in card
    assert "debit-reason-more" not in card


def test_the_rent_page_prints_both_lines() -> None:
    name_provider()
    lease = a_lease(a_store("1991 Istanbul", 1991), a_plain_operator("Rent Ops"))
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.UNPAID, "2000.00")
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        card = tiles(admin_get(rent_page(charge.pk)))["debit"]
    assert "send Rent Ops the PayCo link to add one</span>" in card
    assert 'id="debit-reason-more">Bank debits are also off at PayCo.</span>' in card


def test_the_second_line_is_dropped_without_a_first() -> None:
    form = AddPaymentForm(debit_reason="", debit_reason_more="stray", balance=D("10.00"))
    debit = next(option for option in form.options() if option["value"] == "debit")
    assert (debit["reason"], debit["reason_more"]) == ("", "")


# --- D8: the Add payment badge --------------------------------------------------------------


def status_span(body: str) -> str:
    return element(body, '<span id="charge-status">', "h1")


def test_the_one_off_badge_reads_the_dot_the_label_and_the_amount(fake: FakeProvider) -> None:
    body = admin_get(page_of(invoiced(fake, "2100.00")))
    badge = status_span(body)
    assert 'aria-hidden="true">●</span> Owed · $2,100.00</span>' in badge
    assert "$2,100.00 due" not in body
    # The muted line keeps "due {date}".
    assert " · due " in element(body, '<p class="store-line" id="add-payment-line">', "p")


def test_the_rent_badge_reads_the_dot_the_label_and_the_amount() -> None:
    lease = a_lease(a_store("1991 Istanbul", 1991), a_plain_operator("Fatih Co"))
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.DUE, "2100.00")
    body = admin_get(rent_page(charge.pk))
    assert 'aria-hidden="true">●</span> Due · $2,100.00</span>' in status_span(body)
    assert "$2,100.00 due" not in body
    assert "Fatih Co · due " in body


# --- D10: the Reference help and the topic "Payments" -----------------------------------------


def test_the_reference_help_on_add_payment_is_one_short_sentence(fake: FakeProvider) -> None:
    card = tiles(admin_get(page_of(invoiced(fake))))["record"]
    assert f'<span class="help muted">{ADD_PAYMENT_REFERENCE_HELP}</span>' in card
    assert ADD_PAYMENT_REFERENCE_HELP == "Never an account number."
    assert REFERENCE_HELP not in card and "Zelle confirmation" not in card


def test_the_payments_topic_follows_how_an_invoice_is_collected() -> None:
    slugs = [topic.slug for topic in help_pages.TOPICS]
    assert slugs[slugs.index("collecting-an-invoice") + 1] == "payments"
    found = next(topic for topic in help_pages.TOPICS if topic.slug == "payments")
    assert (found.title, found.updated) == ("Payments", date(2026, 10, 27))


def test_the_payments_topic_renders_with_the_moved_sentence_and_the_rules() -> None:
    name_provider()
    html = body_of(client_for(Role.VIEWER)[0], "/help/payments/")
    article = element(html, '<article id="help-topic"', "article")
    assert '<h2 class="help-title" id="help-title">Payments</h2>' in article
    assert 'href="/help/payments/" id="help-nav-payments" aria-current="page"' in html
    assert REFERENCE_HELP in article
    for heading in (
        "Reference",
        "A payment received outside PayCo",
        "A partial payment",
        "Voiding a payment recorded here",
        "Refunds and voids of a PayCo payment",
    ):
        assert f">{heading}</h3>" in article, heading
    for words in (
        "marked paid in PayCo",
        "the PayCo invoice is not marked paid, and it stays open until the rest is paid",
        "billing role or above and a reason",
        "stays, marked Voided, with who voided it, when and why",
        "Unmark it in the PayCo dashboard",
        "by an admin",
    ):
        assert words in article, words
    assert "Stax" not in article


# --- The held form's guard (Batch A's review) --------------------------------------------------


def test_a_held_value_the_guard_refuses_is_never_kept_in_the_session(fake: FakeProvider) -> None:
    assert not contains_card_number(ROUTING_AND_ACCOUNT)
    with pytest.raises(Exception, match="card or account numbers"):
        validate_no_payment_data(ROUTING_AND_ACCOUNT)
    charge = invoiced(fake)
    client = client_for(Role.BILLING)[0]
    data = {
        "how": "record",
        "method": PaymentMethod.ZELLE.value,
        "amount": "40.00",
        "paid_on": today().isoformat(),
        "reference": "ZL-123",
        "note": f"Paid; {ROUTING_AND_ACCOUNT}",
        "creation_key": str(uuid.uuid4()),
    }
    response = client.post(page_of(charge), data)
    assert response.status_code == 302
    held = client.session[HELD_FORM_KEY]
    assert held["data"]["note"] == ""
    assert held["data"]["reference"] == "ZL-123"
    assert "021000021" not in str(dict(client.session.items()))
    assert any("card or account numbers" in text for text in held["errors"]["note"])
    assert not Payment.objects.filter(one_off_charge=charge).exists()


# --- D9: the phone filter -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("2125550100", "(212) 555-0100"),
        ("212-555-0100", "(212) 555-0100"),
        ("(212) 555.0100", "(212) 555-0100"),
        ("12125550100", "(212) 555-0100"),
        ("1 212 555 0100", "(212) 555-0100"),
        ("+12125550100", "(212) 555-0100"),
        ("+1 (212) 555-0100", "(212) 555-0100"),
        ("+447911123456", "+44 7911123456"),
        ("+44 20 7946 0958", "+44 20 7946 0958"),
        ("+7 912 345-67-89", "+7 912 345-67-89"),
        ("+353861234567", "+353 861234567"),
        ("+1 555 0100", "+1 555 0100"),
        ("21255501001", "21255501001"),
        ("555-0100", "555-0100"),
        ("212-555-0100 ext 5", "212-555-0100 ext 5"),
        ("CALL-ME-NOW", "CALL-ME-NOW"),
        ("+", "+"),
        ("+1234567890123456", "+1234567890123456"),
        ("٢١٢٥٥٥٠١٠٠", "٢١٢٥٥٥٠١٠٠"),
        ("  2125550100  ", "(212) 555-0100"),
        ("", ""),
        (None, ""),
    ],
)
def test_the_phone_filter(stored: str | None, shown: str) -> None:
    assert phone_text(stored) == shown
    assert phone(stored) == shown


def test_the_phone_filter_output_is_escaped_on_a_page() -> None:
    html = render_to_string(
        "ledger/payments/_add_payment_summary.html",
        {"operator": Operator(display_name="X", phone="<b>1</b>")},
    )
    assert "&lt;b&gt;1&lt;/b&gt;" in html and "<b>1</b>" not in html


def test_the_operator_page_shows_both_phones_formatted_and_stores_them_as_typed() -> None:
    operator = a_plain_operator("Phone Ops")
    Operator.objects.filter(pk=operator.pk).update(phone="212-555-0100", sms_phone="+447911123456")
    client = client_for(Role.BILLING)[0]
    html = body_of(client, reverse("ledger:operators:detail", kwargs={"pk": operator.pk}))
    line = element(html, '<p class="store-line operator-line" id="operator-line">', "p")
    assert line.startswith('<p class="store-line operator-line" id="operator-line">(212) 555-0100')
    assert "<dt>Phone</dt><dd>(212) 555-0100</dd>" in html
    assert '<dd id="sms-phone">+44 7911123456</dd>' in html
    assert '<dd id="side-phone">(212) 555-0100</dd>' in html
    assert "212-555-0100<" not in html
    stored = Operator.objects.get(pk=operator.pk)
    assert (stored.phone, stored.sms_phone) == ("212-555-0100", "+447911123456")
    edit = body_of(client, reverse("ledger:operators:edit", kwargs={"pk": operator.pk}))
    assert 'value="212-555-0100"' in edit


def test_the_operator_page_shows_a_dash_for_no_phone() -> None:
    operator = a_plain_operator("No Phone Ops")
    html = body_of(
        client_for(Role.VIEWER)[0], reverse("ledger:operators:detail", kwargs={"pk": operator.pk})
    )
    assert "<dt>Phone</dt><dd>—</dd>" in html and '<dd id="sms-phone">—</dd>' in html


def test_the_add_payment_operator_card_formats_the_phone(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    Operator.objects.filter(pk=operator.pk).update(phone="+1 212 555 0100")
    body = admin_get(page_of(invoiced(fake, operator=Operator.objects.get(pk=operator.pk))))
    assert '<span id="operator-phone">(212) 555-0100</span>' in body


# --- D11: staff names ---------------------------------------------------------------------------


def test_staff_name_prefers_the_full_name_then_the_username_never_the_email() -> None:
    assert staff_name(User(username="fd", first_name="Fatih", last_name="Duzenli")) == (
        "Fatih Duzenli"
    )
    assert staff_name(User(username="fd", first_name="Fatih")) == "Fatih"
    assert staff_name(User(username="fd", last_name="Duzenli")) == "Duzenli"
    assert staff_name(User(username="fd", first_name="  ", last_name=" ")) == "fd"
    assert staff_name(User(username="fd", email="fatih@example.com")) == "fd"


def test_the_staff_filter_puts_the_email_in_the_title_and_escapes_both() -> None:
    user = User(username="fd", first_name="<Fatih>", last_name="D", email='a"b@example.com')
    assert staff(user) == '<span title="a&quot;b@example.com">&lt;Fatih&gt; D</span>'
    assert staff(User(username="fd")) == "<span>fd</span>"
    assert staff(None) == "" and staff("someone") == ""


def a_recorded_payment(actor: User) -> Payment:
    lease = a_lease(a_store(), a_plain_operator("Staff Ops"))
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.DUE, "1500.00")
    outcome = rent_run.record_manual_payment(
        charge.pk,
        method=PaymentMethod.WIRE,
        amount=D("500.00"),
        paid_on=date(2026, 10, 15),
        note="Wire received.",
        actor=actor,
        today=date(2026, 10, 15),
    )
    assert outcome.ok and outcome.payment is not None, outcome.message
    return outcome.payment


def test_the_payment_page_and_the_charge_page_show_the_staff_name() -> None:
    user = named_user("fduzenli", "Fatih", "Duzenli", "fatih@example.com")
    payment = a_recorded_payment(user)
    shown = '<span title="fatih@example.com">Fatih Duzenli</span>'
    html = admin_get(reverse("ledger:payments:detail", kwargs={"pk": payment.pk}))
    assert f"<dt>Recorded by</dt><dd>{shown}</dd>" in html
    assert payment.rent_charge is not None
    charge_html = admin_get(reverse("ledger:charges:detail", kwargs={"pk": payment.rent_charge.pk}))
    assert f'<td data-role="meta" data-label="Recorded by">{shown}</td>' in charge_html
    assert "fduzenli" not in re.sub(r"<[^>]+>", " ", charge_html)


def test_a_user_without_a_name_shows_the_username_with_the_email_title() -> None:
    user = named_user("billing-desk", email="desk@example.com")
    payment = a_recorded_payment(user)
    html = admin_get(reverse("ledger:payments:detail", kwargs={"pk": payment.pk}))
    assert '<dt>Recorded by</dt><dd><span title="desk@example.com">billing-desk</span></dd>' in html
    assert ">desk@example.com<" not in html


def test_a_payment_without_a_recorder_shows_a_dash() -> None:
    lease = a_lease(a_store(), a_plain_operator("Dash Ops"))
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.DUE, "1500.00")
    outcome = rent_run.record_manual_payment(
        charge.pk,
        method=PaymentMethod.WIRE,
        amount=D("500.00"),
        paid_on=date(2026, 10, 15),
        note="Wire received.",
        actor=None,
        today=date(2026, 10, 15),
    )
    assert outcome.payment is not None
    html = admin_get(reverse("ledger:payments:detail", kwargs={"pk": outcome.payment.pk}))
    assert "<dt>Recorded by</dt><dd>—</dd>" in html


def test_the_activity_card_names_the_actor_and_leaves_the_system_unnamed() -> None:
    user = named_user("act-user", "Ada", "Lovelace", "ada@example.com")
    payment = a_recorded_payment(user)
    assert payment.rent_charge is not None
    row = AuditLog.objects.filter(entity_id=str(payment.rent_charge.pk)).latest("id")
    AuditLog.objects.create(
        actor=None,
        action="rent_charge.refreshed",
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        before={},
        after={},
    )
    activity = detail_cards.activity([(row.entity_type, row.entity_id)])
    assert activity.rows[0].user is None
    assert any(item.user == user for item in activity.rows)
    html = render_to_string("ledger/_detail_activity.html", {"activity": activity})
    assert ' by <span title="ada@example.com">Ada Lovelace</span>' in html
    assert html.count(" by <span") == sum(1 for item in activity.rows if item.user is not None)


# --- D12: the partial-payment notice ---------------------------------------------------------

NOTICE = "Partial payment — the PayCo invoice stays open until the rest is paid."


def payment_page(payment: Payment) -> str:
    return admin_get(reverse("ledger:payments:detail", kwargs={"pk": payment.pk}))


def charge_card(html: str) -> str:
    return element(html, '<section class="card" id="charge-card">', "section")


def test_a_partial_manual_payment_shows_the_info_notice_not_the_strong_line(
    fake: FakeProvider,
) -> None:
    name_provider()
    charge = invoiced_rent(fake)
    outcome = pay(charge, "500.00")
    assert outcome.ok and outcome.payment is not None
    card = charge_card(payment_page(outcome.payment))
    assert (
        '<p class="toast info is-inline" id="partial-notice"><span class="toast-icon"><svg' in card
    )
    assert f'<span class="toast-text">{NOTICE}</span></p>' in card
    assert "part of the amount" not in card and "<strong>" not in card
    assert "toast-dismiss" not in card
    # The charge's own page keeps its review line as before.
    charge_html = admin_get(reverse("ledger:charges:detail", kwargs={"pk": charge.pk}))
    assert "Not marked paid in PayCo: part of the amount." in charge_html


def test_the_fallback_is_never_shown_for_a_charge_flagged_only_for_the_partial(
    fake: FakeProvider,
) -> None:
    charge = invoiced_rent(fake)
    assert pay(charge, "500.00").ok
    charge.refresh_from_db()
    assert rent_run.partial_marking(charge)
    assert rent_run.charge_review_message(charge, with_partial=False) == ""
    assert str(rent_run.SECOND_INVOICE) not in rent_run.charge_review_message(
        charge, with_partial=False
    )
    # A charge flagged with no stored reason still reads the fallback, as before.
    charge.review_message = ""
    assert rent_run.charge_review_message(charge, with_partial=False) == str(
        rent_run.SECOND_INVOICE
    )
    [payment] = Payment.objects.filter(rent_charge=charge)
    card = charge_card(payment_page(payment))
    assert str(rent_run.SECOND_INVOICE) not in card and 'id="partial-notice"' in card


def test_a_full_manual_payment_shows_no_notice(fake: FakeProvider) -> None:
    charge = invoiced_rent(fake)
    outcome = pay(charge, "1500.00")
    assert outcome.ok and outcome.payment is not None
    assert 'id="partial-notice"' not in payment_page(outcome.payment)


def test_a_paid_charge_whose_invoice_was_never_marked_keeps_the_strong_line(
    fake: FakeProvider,
) -> None:
    charge = invoiced_rent(fake)
    assert pay(charge, "500.00").ok
    second = pay(charge, "1000.00", method=PaymentMethod.ZELLE)
    assert second.payment is not None
    charge.refresh_from_db()
    assert charge.balance == 0 and rent_run.partial_marking(charge)
    card = charge_card(payment_page(second.payment))
    assert 'id="partial-notice"' not in card
    assert "<strong>Not marked paid in" in card and "part of the amount" in card


def test_a_marking_failure_keeps_the_strong_line_and_shows_no_notice(fake: FakeProvider) -> None:
    charge = invoiced_rent(fake)
    outcome = pay(charge, "500.00")
    assert outcome.payment is not None
    type(charge).objects.filter(pk=charge.pk).update(
        needs_review=True,
        review_message=offline_marking.MARK_FAILED.format(failure="network"),
    )
    card = charge_card(payment_page(outcome.payment))
    assert 'id="partial-notice"' not in card
    assert "<p><strong>Not marked paid in" in card


def test_a_partial_payment_on_a_one_off_charge_shows_the_notice(fake: FakeProvider) -> None:
    name_provider()
    charge = invoiced(fake, "100.00")
    outcome = service.record_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=D("40.00"),
        paid_on=today(),
        note="Part paid by Zelle.",
        actor=client_for(Role.BILLING)[1],
    )
    assert outcome.ok and outcome.payment is not None, outcome.message
    charge.refresh_from_db()
    assert charge.review_message == offline_marking.PARTIAL_AMOUNT
    assert service.review_message(charge, with_partial=False) == ""
    card = charge_card(payment_page(outcome.payment))
    assert f'<span class="toast-text">{NOTICE}</span>' in card and "<strong>" not in card


def test_the_inline_notice_never_fades() -> None:
    css = help_pages.__file__.rsplit("/", 1)[0] + "/static/finance/app.css"
    with open(css, encoding="utf-8") as handle:
        text = handle.read()
    fade = text.index(".toast:not(.warning):not(.error) { animation: toast-fade")
    rule = text.index(".toast.info.is-inline { animation: none; }")
    assert rule > fade


# --- A-2 (deferred MINOR): a voided payment has nothing refundable -------------------------


def test_a_voided_manual_payment_has_nothing_refundable_and_its_tile_says_so() -> None:
    payment = a_recorded_payment(named_user("void-desk"))
    assert refunds.payment_facts(payment).refundable == D("500.00")
    outcome = refunds.void_manual_payment(
        payment.pk, reason="Recorded twice.", actor=client_for(Role.BILLING)[1]
    )
    assert outcome.ok, outcome.message
    payment.refresh_from_db()
    facts = refunds.payment_facts(payment)
    assert (facts.refundable, facts.refunded) == (D("0.00"), D("0.00"))
    assert facts.refund_refusal and facts.void_refusal
    html = payment_page(payment)
    tiles_html = element(html, '<section class="kpi-row detail-tiles"', "section")
    assert (
        '<span class="kpi-value">$0.00</span><span class="kpi-note">nothing refundable</span>'
        in (tiles_html)
    )
    assert '<span class="kpi-note">refundable</span>' not in tiles_html
    assert '<span class="kpi-note">pending and settled refunds</span>' in tiles_html


def test_a_voided_processor_payment_has_nothing_refundable_and_keeps_its_refusals(
    fake: FakeProvider,
) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    before = refunds.payment_facts(payment)
    assert before.refundable == D("1500.00")
    assert processor_void(payment).ok
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.VOIDED
    after = refunds.payment_facts(payment)
    assert after.refundable == D("0.00")
    assert (after.reference, after.refunded) == (before.reference, before.refunded)
    assert after.refund_refusal == refunds.REFUND_NOT_SETTLED
    assert after.void_refusal == refunds.VOID_NOT_OPEN.format(status="voided")
    tiles_html = element(payment_page(payment), '<section class="kpi-row detail-tiles"', "section")
    assert '<span class="kpi-note">nothing refundable</span>' in tiles_html
