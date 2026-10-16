"""Adversarial cases for PLAN-012d Batch C (the setup checklist without tick boxes; the
builder's single combo box, month select, rent-amount endpoint, existing-charge lines,
trash-icon remove), by the QA owner (plan §6 C). The rent-amount endpoint refuses another
operator's lease and a viewer; an existing charge of another lease or operator cannot be
attached; a removed line with bad values never blocks the save; an existing-charge line's
amount cannot exceed its balance; a posted tick field charges nothing and an empty price
charges nothing."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import InvoiceDraft, InvoiceDraftLine, OneOffCharge
from ledger.services import invoice_builder
from ledger.services.invoice_builder import LineInput
from payments.fake import FakeProvider
from tests import test_invoice_builder as builder_tests
from tests.test_invoice_builder import TODAY, a_world, user
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
OCTOBER = date(2026, 10, 1)
# The builder file's fixtures, re-exported so pytest finds them here.
fixed_now = builder_tests.fixed_now
fake = builder_tests.fake
worker = builder_tests.worker


def _rent_amount_url(lease_pk: object, month: str = "2026-10") -> str:
    return f"{reverse('ledger:invoices:rent_amount')}?lease={lease_pk}&month={month}"


# --- the rent-amount endpoint ------------------------------------------------------------


def test_the_rent_amount_endpoint_refuses_another_operators_lease_and_a_viewer(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    other = make_lease(operator=an_operator(fake, customer="cus-c-1", bank="pm-c-1"))
    billing, _ = client_for(Role.BILLING)
    mine = billing.get(_rent_amount_url(lease.pk) + f"&operator={operator.pk}")
    assert mine.status_code == 200 and "amount" in mine.json()
    theirs = billing.get(_rent_amount_url(other.pk) + f"&operator={operator.pk}")
    assert theirs.status_code in (403, 404)
    assert billing.get(_rent_amount_url(uuid.uuid4())).status_code == 404
    assert billing.get(_rent_amount_url(lease.pk, "2026-13")).status_code in (400, 404)
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get(_rent_amount_url(lease.pk)).status_code == 403
    assert Client().get(_rent_amount_url(lease.pk)).status_code == 401


# --- existing-charge lines ----------------------------------------------------------------


def _existing_line(charge: OneOffCharge, amount: str | None = None) -> LineInput:
    return LineInput(
        "Existing open charge",
        "",
        None,
        D(amount) if amount is not None else None,
        existing_charge=charge.pk,
    )


def test_an_existing_charge_of_another_lease_or_operator_cannot_be_attached(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    other_operator = an_operator(fake, customer="cus-c-2", bank="pm-c-2")
    other_lease = make_lease(operator=other_operator)
    theirs = OneOffCharge.objects.create(
        operator=other_operator,
        lease=other_lease,
        category="damage",
        collection="manual",
        description="Their shelf",
        price=D("400.00"),
        due_date=TODAY,
        status="owed",
    )
    second_lease = make_lease(operator=operator)
    on_second = OneOffCharge.objects.create(
        operator=operator,
        lease=second_lease,
        category="damage",
        collection="manual",
        description="Other store's shelf",
        price=D("150.00"),
        due_date=TODAY,
        status="owed",
    )
    for charge in (theirs, on_second):
        refused = invoice_builder.create_draft(
            operator,
            lease,
            "Move-in",
            None,
            [_existing_line(charge)],
            user(Role.BILLING),
            today=TODAY,
        )
        assert not refused.ok, charge.description
    assert not InvoiceDraft.objects.exists() and not InvoiceDraftLine.objects.exists()
    theirs.refresh_from_db()
    assert theirs.balance == D("400.00")


def test_an_existing_charge_lines_amount_cannot_exceed_its_balance(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    mine = OneOffCharge.objects.create(
        operator=operator,
        lease=lease,
        category="damage",
        collection="manual",
        description="Broken shelf",
        price=D("150.00"),
        due_date=TODAY,
        status="owed",
    )
    for amount in ("150.01", "1000.00", "0.00", "-1.00"):
        refused = invoice_builder.create_draft(
            operator,
            lease,
            "Move-in",
            None,
            [_existing_line(mine, amount)],
            user(Role.BILLING),
            today=TODAY,
        )
        assert not refused.ok, amount
    assert not InvoiceDraft.objects.exists()
    made = invoice_builder.create_draft(
        operator, lease, "Move-in", None, [_existing_line(mine)], user(Role.BILLING), today=TODAY
    )
    assert made.ok and made.draft is not None, made.message
    [line] = InvoiceDraftLine.objects.filter(draft=made.draft)
    assert line.unit_amount == D("150.00") and line.existing_charge_id == mine.pk


# --- removed lines never block ---------------------------------------------------------------


def test_a_removed_line_with_bad_values_never_blocks_the_save(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    billing, _ = client_for(Role.BILLING)
    url = reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id})
    data = {
        "creation_key": str(uuid.uuid4()),
        "lease": str(lease.pk),
        "memo": "Move-in",
        "due_date": "",
        "lines-TOTAL_FORMS": "2",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "20",
        # Line 1: removed, with values that would be refused.
        "lines-0-item": "Rent",
        "lines-0-quantity": "-3",
        "lines-0-unit_amount": "NaN",
        "lines-0-rent_month": "2026-13",
        "lines-0-DELETE": "on",
        # Line 2: a plain typed item.
        "lines-1-item": "Sign repair",
        "lines-1-quantity": "1",
        "lines-1-unit_amount": "100.00",
    }
    response = billing.post(url, data)
    assert response.status_code == 302, response.content[:300]
    draft = InvoiceDraft.objects.get()
    lines = list(InvoiceDraftLine.objects.filter(draft=draft))
    assert len(lines) == 1 and lines[0].item == "Sign repair"


# --- the setup checklist ------------------------------------------------------------------------


def test_a_posted_tick_field_charges_nothing_and_an_empty_price_charges_nothing() -> None:
    from tests.test_setup_charges import fee, lease_form

    sign = fee("sign_fee", default_price=D("450.00"), show_in_setup=True)
    admin, _ = client_for(Role.ADMIN)
    base = lease_form()
    # The old tick field with an empty price: nothing charged (the tick is ignored).
    response = admin.post(
        "/leases/new/",
        {**base, f"setup_{sign.code}_tick": "on", f"setup_{sign.code}_price": ""},
    )
    assert response.status_code in (200, 302)
    assert not OneOffCharge.objects.exists()
    # The placeholder value is never a filled value: an empty field posts empty.
    page = admin.get("/leases/new/").content.decode()
    assert 'placeholder="450.00"' in page or 'placeholder="450"' in page
    assert 'value="450.00"' not in page
