"""The Add payment page (docs/qa/PLAN-012f-b Batch C, slice C-2, D14; DESIGN-SPEC §4 Add
payment): one template for the rent and the one-off route, the "How is this being paid?"
card with its two radio cards on the builder's tiles, the Note card, the sticky SUMMARY with
its tiles and the one primary whose words follow the card, the OPERATOR card. A bank debit
today opens Charge now's confirmation, whose POST debits once; a later date schedules. The
payment provider is a :class:`~payments.fake.FakeProvider`; nothing reaches a network.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.html import escape

from accounts.roles import Role
from ledger.forms import (
    DEBIT_NEEDS_ADMIN,
    NOTE_OPTIONAL_LABEL,
    NOTE_REQUIRED_LABEL,
    REFERENCE_HELP,
    RENT_DEBIT_REASON,
)
from ledger.models import AuditLog, CollectionMethod, OneOffCharge, Payment, PaymentMethod
from ledger.services import one_off_charges as service
from payments.fake import FakeProvider
from payments.models import Job, JobKind
from tests.receivables_helpers import a_lease, a_rent_charge, a_store
from tests.receivables_helpers import an_operator as a_plain_operator
from tests.test_bank_debit import enabled, fake, invoiced, setup_charge, today
from tests.test_charge_now import LAST_FOUR, NICKNAME
from tests.test_one_off_charges import an_operator, created, fresh, item
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

__all__ = ["enabled", "fake"]  # the fixtures of tests.test_bank_debit, used here

D = Decimal
APP_CSS = Path(__file__).resolve().parents[1] / "finance" / "static" / "finance" / "app.css"


def client(role: Role) -> Client:
    return client_for(role)[0]


def page_of(charge: OneOffCharge) -> str:
    return reverse("ledger:one_off_charges:payment", kwargs={"pk": charge.pk})


def get(role: Role, path: str) -> str:
    response = client(role).get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def element(body: str, start: str, tag: str) -> str:
    """The element opened by ``start`` up to its matching close tag (no nesting of ``tag``
    inside, which holds for the elements read here)."""
    index = body.index(start)
    return body[index : body.index(f"</{tag}>", index) + len(f"</{tag}>")]


def tiles(body: str) -> dict[str, str]:
    """The two radio cards' markup, the bank debit's first."""
    card = element(body, '<fieldset class="card form-card collect-card how-card"', "fieldset")
    debit, record = re.split(r'<div class="collect-tile[ "]', card)[1:]
    return {"debit": debit, "record": record}


def summary(body: str) -> str:
    return element(body, '<aside class="form-summary', "aside")


def words(body: str, how: str) -> str:
    """The primary's span for ``how``, with its attributes."""
    found = re.search(rf'<span class="how-words" data-shown-when="how-{how}"[^>]*>', body)
    assert found is not None, how
    return found.group(0)


def written() -> tuple[int, int, int]:
    return (AuditLog.objects.count(), Job.objects.count(), Payment.objects.count())


def record_post(**values: str) -> dict[str, str]:
    data = {
        "how": "record",
        "method": PaymentMethod.ZELLE.value,
        "amount": "40.00",
        "paid_on": today().isoformat(),
        "reference": "ZL-123",
        "note": "Paid by Zelle.",
        "creation_key": str(uuid.uuid4()),
    }
    data.update(values)
    return data


def debit_post(charge: OneOffCharge, on: date, **values: str) -> dict[str, str]:
    data = {
        "how": "debit",
        "debit_amount": str(service.open_balance(charge)),
        "debit_on": on.isoformat(),
        "note": "",
        "creation_key": str(uuid.uuid4()),
    }
    data.update(values)
    return data


# --- The frame -------------------------------------------------------------------------------


def test_the_one_off_route_renders_the_frame_with_the_debit_card_chosen(fake: FakeProvider) -> None:
    charge = invoiced(fake, "100.00")
    body = get(Role.ADMIN, page_of(charge))
    assert '<h1 class="store-title">Add payment <span id="charge-status">' in body
    # The charge page's status words (a one-off charge is "Owed"), with the balance due.
    assert "Owed · $100.00 due" in body
    line = element(body, '<p class="store-line" id="add-payment-line">', "p")
    assert charge.operator is not None
    assert f"{charge.label} · {charge.operator.display_name} · due " in line
    cards = tiles(body)
    assert 'id="how-debit" checked' in cards["debit"] and "is-chosen" in cards["debit"]
    assert 'id="how-record"' in cards["record"] and "checked" not in cards["record"]
    assert "is-unavailable" not in cards["debit"]
    assert f"···· {LAST_FOUR} · {NICKNAME} (on file)" in cards["debit"]
    assert 'name="debit_amount"' in cards["debit"] and 'value="100.00"' in cards["debit"]
    assert 'max="100.00"' in cards["debit"]
    assert f'name="debit_on" value="{today().isoformat()}"' in cards["debit"]
    assert f'min="{today().isoformat()}"' in cards["debit"]
    assert "the charge shows Pending until the bank settles" in cards["debit"]
    for name in ("method", "amount", "paid_on", "reference"):
        assert f'name="{name}"' in cards["record"], name
    # The reference's help as before: it names the check number, never an account number.
    assert REFERENCE_HELP in cards["record"]
    assert '<option value="cash">Cash</option>' in cards["record"]
    side = summary(body)
    for tile_id in ("tile-charge", "tile-paid", "tile-balance", "tile-this-payment"):
        assert f'id="{tile_id}"' in side, tile_id
    assert side.count("btn-primary") == 1 and body.count("btn-primary") == 1
    assert 'id="operator-card"' in side and "Connected to" in side
    assert f"bank ····{LAST_FOUR}" in side and "no returned payments this year" in side
    form = element(body[body.index("<main") :], '<form method="post"', "form")
    assert side in form and 'id="note-card"' in form


def test_the_rent_route_renders_the_frame_with_the_debit_card_greyed() -> None:
    lease = a_lease(a_store("1991 Istanbul", 1991), a_plain_operator("Fatih Co"))
    charge = a_rent_charge(lease, date(2026, 10, 1), due="2000.00")
    body = get(Role.ADMIN, reverse("ledger:charges:record_payment", kwargs={"pk": charge.pk}))
    cards = tiles(body)
    assert "is-unavailable" in cards["debit"] and 'id="how-debit" disabled' in cards["debit"]
    assert (
        escape(RENT_DEBIT_REASON) in cards["debit"] and 'name="debit_amount"' not in cards["debit"]
    )
    assert 'id="how-record" checked' in cards["record"]
    assert "Rent Oct 2026, debit 1 · #1991 Istanbul · Fatih Co · due" in body
    assert "Unpaid · $2,000.00 due" in body
    side = summary(body)
    assert side.count("btn-primary") == 1
    assert 'id="operator-card"' in side and "no bank account on file" in side


@pytest.mark.parametrize("case", ["no bank", "flag off", "billing"])
def test_the_debit_card_is_greyed_with_its_reason_and_the_record_card_chosen(
    fake: FakeProvider, case: str
) -> None:
    role = Role.BILLING if case == "billing" else Role.ADMIN
    if case == "no bank":
        charge = invoiced(fake, operator=an_operator(fake, bank=None))
        reason = str(service.DEBIT_NO_METHOD)
    else:
        charge = invoiced(fake)
        reason = str(service.DEBIT_OFF) if case == "flag off" else DEBIT_NEEDS_ADMIN
    if case == "flag off":
        with override_settings(STAX_PAY_INVOICE_ENABLED=False):
            body = get(role, page_of(charge))
    else:
        body = get(role, page_of(charge))
    cards = tiles(body)
    assert "is-unavailable" in cards["debit"] and "disabled" in cards["debit"]
    assert reason in cards["debit"]
    assert 'id="how-record" checked' in cards["record"]
    assert 'name="debit_on"' not in body
    assert "hidden" in words(body, "debit") and "hidden" not in words(body, "record")


def test_a_viewer_cannot_open_the_page(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    assert client(Role.VIEWER).get(page_of(charge)).status_code == 403


# --- Record a payment received outside the provider -----------------------------------------


@pytest.mark.parametrize("method", [PaymentMethod.ZELLE.value, PaymentMethod.CASH.value])
def test_posting_record_records_the_payment_as_before(fake: FakeProvider, method: str) -> None:
    charge = invoiced(fake, "100.00")
    response = client(Role.BILLING).post(page_of(charge), record_post(method=method))
    assert response.status_code == 302
    [payment] = Payment.objects.filter(one_off_charge=charge)
    assert (payment.method, payment.amount) == (method, D("40.00"))
    # A partial payment is recorded here only: the provider invoice is not marked.
    assert not Job.objects.filter(kind=JobKind.ONE_OFF_PAYMENT_MARK).exists()


def test_a_payment_covering_the_whole_price_queues_the_provider_marking(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake, "100.00")
    response = client(Role.BILLING).post(page_of(charge), record_post(amount="100.00"))
    assert response.status_code == 302
    assert Job.objects.filter(kind=JobKind.ONE_OFF_PAYMENT_MARK, entity_id=charge.pk).count() == 1


def test_a_post_without_how_is_the_record_card(fake: FakeProvider) -> None:
    charge = invoiced(fake, "100.00")
    data = record_post()
    del data["how"]
    assert client(Role.BILLING).post(page_of(charge), data).status_code == 302
    assert Payment.objects.filter(one_off_charge=charge).count() == 1


def test_the_rent_route_records_a_payment_as_before() -> None:
    lease = a_lease(a_store("1991 Istanbul", 1991), a_plain_operator("Fatih Co"))
    charge = a_rent_charge(lease, date(2026, 10, 1), due="2000.00")
    path = reverse("ledger:charges:record_payment", kwargs={"pk": charge.pk})
    response = client(Role.BILLING).post(path, record_post(amount="500.00"))
    assert response.status_code == 302
    assert Payment.objects.filter(rent_charge=charge, amount=D("500.00")).count() == 1


def test_the_note_is_required_to_record(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    billing = client(Role.BILLING)
    before = written()
    billing.post(page_of(charge), record_post(note="   "))
    assert written() == before
    body = billing.get(page_of(charge)).content.decode()
    assert "A note is required: say what was received." in body


def test_a_card_number_is_refused_in_the_reference_even_when_the_debit_is_chosen(
    fake: FakeProvider,
) -> None:
    charge = setup_charge(fake)
    before = written()
    data = debit_post(charge, today() + timedelta(days=5), reference="4111 1111 1111 1111")
    client(Role.ADMIN).post(page_of(charge), data)
    assert written() == before
    assert fresh(charge).collection == CollectionMethod.NONE


# --- Charge the operator's bank account -------------------------------------------------------


def test_a_debit_today_redirects_to_the_confirmation_and_writes_nothing(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake, "100.00")
    before = written()
    data = debit_post(charge, today())
    response = client(Role.ADMIN).post(page_of(charge), data)
    assert response.status_code == 302
    confirm = reverse("ledger:one_off_charges:charge_now", kwargs={"pk": charge.pk})
    assert response["Location"] == f"{confirm}?key={data['creation_key']}"
    assert written() == before
    assert fresh(charge).collection == CollectionMethod.EMAIL


def test_the_confirmation_reads_an_emailed_charge_as_todays_debit_and_debits_once(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake, "100.00")
    key = str(uuid.uuid4())
    confirm = reverse("ledger:one_off_charges:charge_now", kwargs={"pk": charge.pk})
    admin = client(Role.ADMIN)
    body = admin.get(f"{confirm}?key={key}").content.decode()
    assert 'id="charge-now-submit"' in body and 'id="charge-now-refusal"' not in body
    assert f'name="key" value="{key}"' in body
    assert admin.post(f"{confirm}?key={key}", {"key": key}).status_code == 302
    assert admin.post(f"{confirm}?key={key}", {"key": key}).status_code == 302
    assert Job.objects.filter(kind=JobKind.INVOICE_PAY, entity_id=charge.pk).count() == 1
    assert (
        AuditLog.objects.filter(entity_id=str(charge.pk), action=service.DEBIT_NOW_ACTION).count()
        == 1
    )
    assert fresh(charge).collection == CollectionMethod.CHARGE_NOW


def test_a_debit_on_a_later_date_schedules_and_lands_on_the_charge(fake: FakeProvider) -> None:
    charge = setup_charge(fake, "250.00")
    on = today() + timedelta(days=10)
    response = client(Role.ADMIN).post(page_of(charge), debit_post(charge, on), follow=True)
    assert response.redirect_chain[-1][0] == reverse(
        "ledger:one_off_charges:detail", kwargs={"pk": charge.pk}
    )
    row = fresh(charge)
    assert (row.collection, row.due_date) == (CollectionMethod.AUTOPAY_DUE, on)
    assert Job.objects.filter(kind=JobKind.SCHEDULE_CREATE, entity_id=charge.pk).count() == 1
    assert "will debit $250.00 on" in response.content.decode()
    # The note is optional for a debit.
    assert not Payment.objects.filter(one_off_charge=charge).exists()


def test_a_forced_debit_without_a_bank_account_is_refused_with_the_reason(
    fake: FakeProvider,
) -> None:
    charge = created(an_operator(fake, bank=None), item(CollectionMethod.MANUAL, "80.00"))
    admin = client(Role.ADMIN)
    before = written()
    response = admin.post(page_of(charge), debit_post(charge, today()))
    assert response.status_code == 302 and "charge-now" not in response["Location"]
    assert written() == before
    body = admin.get(page_of(charge)).content.decode()
    # Once on the greyed card, once as the refusal under the card.
    assert body.count(str(service.DEBIT_NO_METHOD)) == 2


@pytest.mark.parametrize(
    ("amount", "message"),
    [("99.00", "A bank debit takes the whole balance"), ("101.00", service.DEBIT_OVER_BALANCE)],
)
def test_a_debit_of_another_amount_is_refused(
    fake: FakeProvider, amount: str, message: str
) -> None:
    charge = invoiced(fake, "100.00")
    admin = client(Role.ADMIN)
    before = written()
    response = admin.post(page_of(charge), debit_post(charge, today(), debit_amount=amount))
    assert "charge-now" not in response["Location"]
    assert written() == before
    assert message in admin.get(page_of(charge)).content.decode()


def test_a_large_debit_on_a_later_date_needs_the_confirmation(fake: FakeProvider) -> None:
    charge = setup_charge(fake, "5000.00")
    admin = client(Role.ADMIN)
    body = admin.get(page_of(charge)).content.decode()
    assert 'name="confirm_large_debit"' in tiles(body)["debit"]
    on = today() + timedelta(days=5)
    before = written()
    admin.post(page_of(charge), debit_post(charge, on))
    assert written() == before
    response = admin.post(page_of(charge), debit_post(charge, on, confirm_large_debit="on"))
    assert response.status_code == 302
    assert fresh(charge).collection == CollectionMethod.AUTOPAY_DUE


def test_billing_cannot_post_a_debit(fake: FakeProvider) -> None:
    charge = setup_charge(fake)
    before = written()
    client(Role.BILLING).post(page_of(charge), debit_post(charge, today() + timedelta(days=5)))
    assert written() == before


# --- The note, the button and the After this payment line ---------------------------------


def test_the_note_label_follows_the_card(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    debit_body = get(Role.ADMIN, page_of(charge))
    note = element(debit_body, '<section class="card form-card note-card"', "section")
    assert f'<label for="id_note">{NOTE_OPTIONAL_LABEL}</label>' in note
    assert f'data-label="{NOTE_REQUIRED_LABEL}"' in note and 'data-label-when="how-record"' in note
    record_body = get(Role.BILLING, page_of(charge))
    note = element(record_body, '<section class="card form-card note-card"', "section")
    assert f'<label for="id_note">{NOTE_REQUIRED_LABEL}</label>' in note


def test_the_button_and_the_after_line_follow_the_card(fake: FakeProvider) -> None:
    charge = invoiced(fake, "5000.00")
    debit_body = get(Role.ADMIN, page_of(charge))
    assert "hidden" not in words(debit_body, "debit") and "hidden" in words(debit_body, "record")
    side = summary(debit_body)
    assert 'Charge <span class="payment-amount">$5,000.00</span> by ACH' in side
    assert 'id="after-payment-value"' in side and ">Paid in full<" in side
    record_body = get(Role.BILLING, page_of(charge))
    side = summary(record_body)
    assert 'Record <span class="payment-amount">$0.00</span> as paid' in side
    assert "$5,000.00 still owed" in side
    assert "ledger/add_payment.js" in record_body


def test_the_phone_rules_one_column_and_the_summary_above_the_full_width_button() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 768px) {", 1)[1].split("\n}\n", 1)[0]
    assert ".form-layout { grid-template-columns: minmax(0, 1fr);" in phone
    assert ".form-summary .form-actions { position: fixed;" in phone
    assert ".how-card .collect-fields .field { flex-basis: 100%; }" in phone
    assert ".form-summary .form-actions .btn { width: 100%;" in css
    assert ".how-tiles { grid-template-columns: minmax(0, 1fr); }" in css


def test_the_summary_tiles_come_before_the_button(fake: FakeProvider) -> None:
    side = summary(get(Role.ADMIN, page_of(invoiced(fake))))
    assert side.index('id="summary-tiles"') < side.index('id="after-payment"')
    assert side.index('id="after-payment"') < side.index('id="save-payment"')
    assert side.index('id="save-payment"') < side.index('id="cancel-payment"')
    assert side.index('id="payment-summary"') < side.index('id="operator-card"')


def test_no_provider_name_or_account_number_in_the_templates() -> None:
    root = Path(__file__).resolve().parents[1] / "ledger" / "templates" / "ledger" / "payments"
    for name in ("add_payment.html", "_how.html", "_add_payment_summary.html"):
        text = (root / name).read_text(encoding="utf-8")
        assert "stax" not in text.lower(), name
    assert not (root.parent / "one_off_charges" / "payment_form.html").exists()
    assert not (root.parent / "charges" / "payment_form.html").exists()


def test_a_get_writes_nothing(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    before = written()
    get(Role.ADMIN, page_of(charge))
    confirm = reverse("ledger:one_off_charges:charge_now", kwargs={"pk": charge.pk})
    get(Role.ADMIN, f"{confirm}?key=not-a-uuid")
    assert written() == before
    assert fresh(charge).collection == CollectionMethod.EMAIL
