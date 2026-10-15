"""The store page as the workspace (docs/qa/PLAN-012d-store-workspace.md §6 A, Batch A).

The header card, the action bar by role (a vacant store offers New lease), the forms the
bar opens with the store and lease known (the hidden fields, the read-only line, the
return to the right tab), the tabs and their filters, the query budget, the drafts'
Edit, Send and Delete, the lease page without its lists, the "← Store" links, the
attention list's store links, the navigation, the History tab and the "already on
draft" link. The adversarial cases are the QA owner's (test_plan012d_a_adversarial.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    ContactEntry,
    ContactKind,
    FeeType,
    InstallmentCadence,
    InstallmentCollection,
    InstallmentPlan,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    InvoiceStatus,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    ScheduleKind,
    ScheduleState,
    Store,
)
from ledger.services import attention, invoice_builder, workspace
from ledger.services.invoice_builder import LineInput
from payments.models import ExternalRef
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_paid_rent_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_invoice,
    an_operator,
)
from tests.test_invoice_builder import page_data
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
OCTOBER = date(2026, 10, 1)
STARTED = date(2026, 9, 1)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


def a_world(label: str = "2477", name: str = "Acme Ops") -> Lease:
    """A store with a wire lease from 1 September 2026, debit day 5, rent $3,500."""
    return a_lease(a_store(label), an_operator(name), started=STARTED, channel=PaymentChannel.WIRE)


def with_customer(operator: Operator) -> Operator:
    ExternalRef.objects.create(
        entity_type="operator",
        entity_id=operator.pk,
        provider="stax_pay",
        external_kind="customer",
        external_id=f"cus-{uuid.uuid4().hex[:12]}",
    )
    return operator


def store_page(store_id: object, **query: str) -> str:
    url = reverse("ledger:stores:detail", kwargs={"pk": store_id})
    return url + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")


def tab(store_id: object, name: str) -> str:
    return store_page(store_id) + f"?tab={name}#tab-{name}"


def known(lease: Lease) -> str:
    return f"?store={lease.store_id}&lease={lease.pk}"


def page(client: Client, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200, (url, response.status_code)
    return response.content.decode()


def a_draft(lease: Lease, item: str = "Sign repair") -> InvoiceDraft:
    assert lease.operator is not None
    outcome = invoice_builder.create_draft(
        lease.operator,
        lease,
        "Move-in",
        None,
        [LineInput(item=item, description="", quantity=None, unit_amount=D("100.00"))],
        client_for(Role.BILLING)[1],
        today=TODAY,
    )
    assert outcome.ok and outcome.draft is not None, outcome.message
    return outcome.draft


# --- The header (D1) --------------------------------------------------------------------------


def test_the_header_shows_the_lease_balance_credit_next_payment_and_open_promise() -> None:
    lease = a_world()
    assert lease.operator is not None
    a_rent_charge(lease, OCTOBER, due="1000.00")
    a_one_off_charge(
        lease.operator, "250.00", date(2026, 10, 25), lease=lease, collection="autopay_due"
    )
    an_insurance_charge(lease, 2026, date(2026, 10, 10), "695.00")
    CollectionSchedule.objects.create(
        lease=lease,
        kind=ScheduleKind.RENT,
        seq=1,
        debit_day=5,
        amount=D("3500.00"),
        state=ScheduleState.ACTIVE,
    )
    ContactEntry.objects.create(
        operator=lease.operator,
        lease=lease,
        kind=ContactKind.PROMISE,
        happened_on=TODAY,
        promised_date=date(2026, 10, 20),
        promised_amount=D("500.00"),
        body="Will pay on the 20th.",
    )
    space = workspace.store_workspace(lease.store, TODAY, Role.BILLING)
    header = space.header
    assert header.lease == lease and header.operator == lease.operator
    assert header.rent is not None and header.rent.amount == D("3500.00")
    assert header.autopay == "Active"
    assert header.open_balance == D("1945.00")
    assert header.credit == D("0.00")
    # The one-off autopay on 25 October comes before the rent debit on 5 November.
    assert header.next_payment == workspace.NextPayment(date(2026, 10, 25), "Broken shelf")
    assert header.open_promise is not None and header.open_promise.promised_amount == D("500")
    html = page(client_for(Role.VIEWER)[0], store_page(lease.store_id))
    assert 'id="header-open-balance">$1945.00<' in html
    assert 'id="header-credit">$0.00<' in html
    assert "2026-10-25: Broken shelf" in html
    assert "$500.00 by 2026-10-20" in html
    assert "Acme Ops" in html and "From 2026-09-01" in html


def test_without_a_one_off_due_the_next_payment_is_the_rent_debit() -> None:
    lease = a_world()
    CollectionSchedule.objects.create(
        lease=lease,
        kind=ScheduleKind.RENT,
        seq=1,
        debit_day=5,
        amount=D("3500.00"),
        state=ScheduleState.ACTIVE,
    )
    header = workspace.store_workspace(lease.store, TODAY, Role.VIEWER).header
    assert header.next_payment == workspace.NextPayment(date(2026, 11, 5), workspace.NEXT_RENT)


def test_a_vacant_store_offers_new_lease_and_hides_the_lease_actions() -> None:
    lease = a_world()
    Lease.objects.filter(pk=lease.pk).update(
        ended_on=date(2026, 9, 30), collection_status=CollectionStatus.ENDED
    )
    admin, _ = client_for(Role.ADMIN)
    html = page(admin, store_page(lease.store_id))
    assert f'href="/leases/new/?store={lease.store_id}"' in html
    assert 'id="bar-new-lease"' in html and 'id="bar-upload-document"' in html
    for hidden in ("new-invoice", "bar-record-payment", "bar-charge-now", "bar-edit-lease"):
        assert f'id="{hidden}"' not in html, hidden
    assert 'id="store-end-lease"' not in html and 'id="store-transfer"' not in html
    header = workspace.store_workspace(lease.store, TODAY, Role.ADMIN).header
    assert header.lease is None and header.next_payment is None


# --- The action bar (D2) -----------------------------------------------------------------------


def test_the_bar_by_role() -> None:
    lease = a_world()
    store = lease.store

    def keys(role: Role) -> list[str]:
        return [action.key for action in workspace.store_workspace(store, TODAY, role).actions]

    billing = [
        "create_invoice",
        "record_payment",
        "add_setup_charge",
        "upload_document",
        "add_contact",
    ]
    assert keys(Role.VIEWER) == []
    assert keys(Role.BILLING) == billing
    assert keys(Role.ADMIN) == [
        "create_invoice",
        "record_payment",
        "charge_now",
        *billing[2:],
        "edit_lease",
        "transfer",
        "end_lease",
    ]
    html = page(client_for(Role.ADMIN)[0], store_page(store.pk))
    assert f'href="/invoices/new/store/{store.pk}/?store={store.pk}&amp;lease={lease.pk}"' in html
    setup = f"/leases/{lease.pk}/setup-charges/new/"
    assert f'href="{setup}?store={store.pk}&amp;lease={lease.pk}"' in html
    # "Schedule a payment" waits for Batch B (``ledger:stores:schedule_payment``).
    assert "Schedule a payment" not in html and "collection=autopay_due" not in html
    assert f'href="/stores/{store.pk}/payments/new/"' in html
    assert f'href="/stores/{store.pk}/charge-now/"' in html
    assert f'href="/leases/{lease.pk}/edit/?store={store.pk}&amp;lease={lease.pk}"' in html
    assert 'id="action-bar"' not in page(client_for(Role.VIEWER)[0], store_page(store.pk))


def test_record_a_payment_opens_the_charges_tab_until_batch_b() -> None:
    lease = a_world()
    billing, _ = client_for(Role.BILLING)
    response = billing.get(reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}))
    assert (response.status_code, response["Location"]) == (302, tab(lease.store_id, "charges"))
    assert client_for(Role.VIEWER)[0].get(response["Location"]).status_code == 200


def test_charge_now_lists_the_stores_chargeable_charges_for_admins() -> None:
    lease = a_world()
    assert lease.operator is not None
    charge = a_one_off_charge(lease.operator, "120.00", TODAY, lease=lease, collection="charge_now")
    a_one_off_charge(lease.operator, "80.00", TODAY, lease=lease, description="Manual one")
    url = reverse("ledger:stores:charge_now_pick", kwargs={"pk": lease.store_id})
    assert client_for(Role.BILLING)[0].get(url).status_code == 403
    html = page(client_for(Role.ADMIN)[0], url)
    # Not chargeable here (no provider invoice, the flag off): listed with its reason.
    assert charge.label in html and "Manual one" not in html
    assert 'id="charge-now-refused"' in html


# --- Forms opened from the store (D2, D8) -------------------------------------------------------


def test_add_setup_charge_from_the_store_shows_the_line_and_returns_to_the_charges_tab() -> None:
    lease = a_world()
    fee = FeeType.objects.create(code=f"sign-{uuid.uuid4().hex[:6]}", name="Sign", active=True)
    billing, _ = client_for(Role.BILLING)
    url = reverse("ledger:leases:add_setup_charge", kwargs={"pk": lease.pk}) + known(lease)
    html = page(billing, url)
    assert "Store 2477 · Lease from 2026-09-01 · Operator Acme Ops" in html
    assert 'id="store-crumb"' in html
    response = billing.post(
        url,
        {
            "fee_type": str(fee.pk),
            "price": "75.00",
            "cost": "",
            "vendor": "",
            "due_date": "2026-10-20",
        },
    )
    assert (response.status_code, response["Location"]) == (302, tab(lease.store_id, "charges"))
    assert OneOffCharge.objects.get(lease=lease, fee_type=fee).price == D("75.00")
    # A refusal keeps the context on the form.
    again = billing.post(url, {"fee_type": str(fee.pk), "price": "", "due_date": "2026-10-20"})
    assert again["Location"] == url


def test_edit_lease_from_the_store_hides_store_and_operator_and_returns_to_the_store() -> None:
    lease = a_world()
    admin, _ = client_for(Role.ADMIN)
    url = reverse("ledger:leases:edit", kwargs={"pk": lease.pk}) + known(lease)
    html = page(admin, url)
    assert '<select name="operator"' not in html and '<select name="store"' not in html
    assert "Store 2477 · Lease from 2026-09-01 · Operator Acme Ops" in html
    other = an_operator("Someone Else")
    response = admin.post(
        url,
        {
            "store": str(a_store("9999").pk),
            "operator": str(other.pk),
            "debit_day": "7",
            "second_debit_day": "",
            "payment_channel": PaymentChannel.WIRE,
            "started_on": "2026-09-01",
            "collection_status": CollectionStatus.ACTIVE,
            "prorate_first_month": "on",
            "rent": "3500.00",
            "rent_effective_from": "",
        },
    )
    assert (response.status_code, response["Location"]) == (302, store_page(lease.store_id))
    lease.refresh_from_db()
    # The hidden fields are disabled: a posted store or operator never changes them.
    assert lease.debit_day == 7
    assert lease.store.external_label == "2477" and lease.operator is not None
    assert lease.operator.display_name == "Acme Ops"


def test_new_lease_from_a_vacant_store_has_the_store_known() -> None:
    store = a_store("3100")
    admin, _ = client_for(Role.ADMIN)
    html = page(admin, f"/leases/new/?store={store.pk}")
    assert "Store 3100" in html and '<select name="store"' not in html
    assert admin.get(f"/leases/new/?store={uuid.uuid4()}").status_code == 404
    assert admin.get("/leases/new/?store=not-a-uuid").status_code == 404


def test_add_contact_from_the_store_hides_the_lease_and_returns_to_the_contact_tab() -> None:
    lease = a_world()
    billing, _ = client_for(Role.BILLING)
    url = reverse("ledger:stores:contacts_add", kwargs={"pk": lease.store_id}) + known(lease)
    html = page(billing, url)
    assert f'type="hidden" name="lease" value="{lease.pk}"' in html
    assert "Store 2477 · Lease from 2026-09-01 · Operator Acme Ops" in html
    response = billing.post(
        url,
        {
            "kind": "call",
            "happened_on": TODAY.isoformat(),
            "body": "Called.",
            "lease": str(lease.pk),
        },
    )
    assert (response.status_code, response["Location"]) == (302, tab(lease.store_id, "contacts"))
    assert ContactEntry.objects.get().lease == lease


def test_a_document_upload_returns_to_the_documents_tab() -> None:
    lease = a_world()
    billing, _ = client_for(Role.BILLING)
    response = billing.post(f"/documents/stores/{lease.store_id}/upload/", {})
    assert (response.status_code, response["Location"]) == (302, tab(lease.store_id, "documents"))


def test_a_rent_payment_from_the_charges_tab_returns_to_the_payments_tab() -> None:
    lease = a_world()
    charge = a_rent_charge(lease, OCTOBER, due="1000.00")
    billing, _ = client_for(Role.BILLING)
    html = page(billing, store_page(lease.store_id))
    url = f"/charges/{charge.pk}/payments/new/?store={lease.store_id}"
    assert f'href="{url}"' in html
    form = page(billing, url)
    assert 'id="store-crumb"' in form
    response = billing.post(
        url,
        {
            "creation_key": str(uuid.uuid4()),
            "method": "zelle",
            "amount": "400.00",
            "paid_on": TODAY.isoformat(),
            "reference": "",
            "note": "Zelle received.",
        },
    )
    assert (response.status_code, response["Location"]) == (302, tab(lease.store_id, "payments"))
    charge.refresh_from_db()
    assert charge.amount_paid == D("400.00")
    payments = page(billing, store_page(lease.store_id, tab="payments"))
    assert "$400.00" in payments and 'id="store-payments"' in payments


def test_a_one_off_payment_from_the_charges_tab_returns_to_the_payments_tab() -> None:
    lease = a_world()
    assert lease.operator is not None
    charge = a_one_off_charge(lease.operator, "90.00", TODAY, lease=lease)
    billing, _ = client_for(Role.BILLING)
    url = f"/one-off-charges/{charge.pk}/payments/new/?store={lease.store_id}"
    assert url in page(billing, store_page(lease.store_id))
    response = billing.post(
        url,
        {
            "creation_key": str(uuid.uuid4()),
            "method": "wire",
            "amount": "90.00",
            "paid_on": TODAY.isoformat(),
            "reference": "",
            "note": "Zelle received.",
        },
    )
    assert (response.status_code, response["Location"]) == (302, tab(lease.store_id, "payments"))


def test_a_new_invoice_from_the_store_keeps_the_context_to_the_draft() -> None:
    lease = a_world()
    billing, _ = client_for(Role.BILLING)
    url = reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id}) + known(lease)
    html = page(billing, url)
    assert f'type="hidden" name="lease" value="{lease.pk}"' in html
    assert "Store 2477 · Lease from 2026-09-01 · Operator Acme Ops" in html
    response = billing.post(
        url,
        page_data(
            [{"kind": "item", "item": "Sign repair", "unit_amount": "100.00"}],
            lease=str(lease.pk),
        ),
    )
    draft = InvoiceDraft.objects.get()
    assert response["Location"] == f"/invoices/drafts/{draft.pk}/{known(lease)}"
    draft_page = page(billing, response["Location"])
    assert f'action="/invoices/drafts/{draft.pk}/send/?store={lease.store_id}' in draft_page
    assert 'id="store-crumb"' in draft_page


# --- The tabs (D3) -------------------------------------------------------------------------------


def test_the_charges_tab_lists_every_kind_and_filters_open_paid_all() -> None:
    lease = a_world()
    assert lease.operator is not None
    a_rent_charge(lease, OCTOBER, due="1000.00")
    a_paid_rent_charge(lease, date(2026, 9, 1), "3500.00", date(2026, 9, 5))
    a_one_off_charge(lease.operator, "250.00", TODAY, lease=lease)
    an_insurance_charge(lease, 2026, date(2026, 10, 10), "695.00")
    other = a_world("5555", "Other Ops")
    a_rent_charge(other, OCTOBER, due="777.00")
    viewer, _ = client_for(Role.VIEWER)
    opened = page(viewer, store_page(lease.store_id))
    assert 'id="tab-charges"' in opened
    assert "Rent 2026-10" in opened and "Broken shelf" in opened
    assert "Insurance, policy year 2026" in opened
    assert "Rent 2026-09" not in opened and "$777.00" not in opened
    assert 'id="charges-balance">$1945.00<' in opened
    assert "Record payment" not in opened  # billing only
    paid = page(viewer, store_page(lease.store_id, tab="charges", filter="paid"))
    assert "Rent 2026-09" in paid and "Rent 2026-10" not in paid
    every = page(viewer, store_page(lease.store_id, tab="charges", filter="all"))
    assert "Rent 2026-09" in every and "Rent 2026-10" in every
    assert f'href="/charges/export.csv?store={lease.store_id}"' in opened
    exported = viewer.get(f"/charges/export.csv?store={lease.store_id}")
    body = b"".join(exported.streaming_content).decode()  # type: ignore[attr-defined]
    assert "2026-10" in body and "777.00" not in body


def test_a_rescheduled_insurance_return_is_owed_once_on_the_tab_as_in_the_header() -> None:
    lease = a_world()
    assert lease.operator is not None
    insurance = an_insurance_charge(lease, 2026, date(2026, 10, 10), "695.00")
    returned = Payment.objects.create(
        insurance_charge=insurance,
        kind=PaymentKind.INSURANCE,
        policy_year=2026,
        method=PaymentMethod.ACH,
        amount=D("695.00"),
        status=PaymentStatus.RETURNED,
        returned_at=NOW,
    )
    redebit = a_one_off_charge(
        lease.operator,
        "695.00",
        date(2026, 10, 20),
        lease=lease,
        paid="300.00",
        status=OneOffChargeStatus.PARTIAL,
        category=OneOffCategory.REDEBIT,
        source_payment=returned,
    )
    a_one_off_charge(
        lease.operator,
        "25.00",
        date(2026, 10, 20),
        lease=lease,
        category=OneOffCategory.NSF_FEE,
        companion_of=redebit,
    )
    header = workspace.store_workspace(lease.store, TODAY, Role.ADMIN).header
    # 395 left on the re-debit and its 25 fee; the insurance charge is carried.
    assert header.open_balance == D("420.00")
    opened = workspace.charges_tab(lease.store, workspace.FILTER_OPEN, Role.ADMIN)
    assert opened["charge_total"] == header.open_balance
    assert insurance.pk not in {row.charge_id for row in opened["charge_rows"]}
    every = workspace.charges_tab(lease.store, workspace.FILTER_ALL, Role.ADMIN)
    [row] = [row for row in every["charge_rows"] if row.charge_id == insurance.pk]
    # What the re-debit collected is paid on the insurance charge.
    assert (row.balance, row.paid, row.status) == (D("0.00"), D("300.00"), "Paid")
    assert every["charge_total"] == header.open_balance


def test_the_invoices_tab_lists_drafts_with_their_actions_and_filters_invoices() -> None:
    lease = a_world()
    assert lease.operator is not None
    draft = a_draft(lease)
    charge = a_one_off_charge(lease.operator, "300.00", TODAY, lease=lease)
    voided = a_one_off_charge(lease.operator, "50.00", TODAY, lease=lease, description="Old")
    open_invoice = an_invoice(lease.operator, 1, "300.00", one_off_charge=charge)
    void = an_invoice(
        lease.operator, 2, "50.00", one_off_charge=voided, internal_status=InvoiceStatus.VOID
    )
    billing, _ = client_for(Role.BILLING)
    html = page(billing, store_page(lease.store_id, tab="invoices"))
    assert draft.number in html
    assert f'href="/invoices/drafts/{draft.pk}/?store={lease.store_id}"' in html
    assert f'action="/invoices/drafts/{draft.pk}/send/?store={lease.store_id}"' in html
    assert f'action="/invoices/drafts/{draft.pk}/delete/?store={lease.store_id}"' in html
    assert "js-single-submit draft-send" in html
    assert open_invoice.invoice_number in html and void.invoice_number not in html
    every = page(billing, store_page(lease.store_id, tab="invoices", filter="all"))
    assert void.invoice_number in every
    viewer = page(client_for(Role.VIEWER)[0], store_page(lease.store_id, tab="invoices"))
    assert draft.number in viewer and "/delete/" not in viewer and "/send/" not in viewer


def test_delete_of_a_never_sent_draft_needs_no_note_and_returns_to_the_tab() -> None:
    lease = a_world()
    draft = a_draft(lease)
    billing, user = client_for(Role.BILLING)
    url = reverse("ledger:invoices:delete_draft", kwargs={"pk": draft.pk})
    response = billing.post(f"{url}?store={lease.store_id}")
    assert (response.status_code, response["Location"]) == (302, tab(lease.store_id, "invoices"))
    assert not InvoiceDraft.objects.filter(pk=draft.pk).exists()
    row = AuditLog.objects.get(action="invoice_draft.deleted")
    assert row.entity_id == str(draft.pk) and row.actor == user
    assert row.before is not None and row.before["status"] == InvoiceDraftStatus.DRAFT
    assert f"Draft {draft.number} deleted." in page(billing, response["Location"])
    # Without the store: back to the operator page.
    other = a_draft(lease)
    response = billing.post(reverse("ledger:invoices:delete_draft", kwargs={"pk": other.pk}))
    assert response["Location"] == f"/operators/{lease.operator_id}/#invoice-drafts"


def test_delete_refuses_a_sent_or_cancelled_draft_and_a_transfers_draft() -> None:
    lease = a_world()
    assert lease.operator is not None
    with_customer(lease.operator)
    billing, user = client_for(Role.BILLING)
    sent = a_draft(lease)
    assert invoice_builder.send(sent.pk, user).ok
    cancelled = a_draft(lease)
    assert invoice_builder.cancel(cancelled.pk, user).ok
    for draft, message in ((sent, invoice_builder.DELETE_SENT), (cancelled, None)):
        outcome = invoice_builder.delete_draft(draft.pk, user)
        assert not outcome.ok and (message is None or outcome.message == message)
        assert InvoiceDraft.objects.filter(pk=draft.pk).exists()
    response = billing.post(reverse("ledger:invoices:delete_draft", kwargs={"pk": sent.pk}))
    assert response["Location"] == f"/invoices/drafts/{sent.pk}/"
    assert not AuditLog.objects.filter(action="invoice_draft.deleted").exists()
    viewer, viewer_user = client_for(Role.VIEWER)
    assert not invoice_builder.delete_draft(a_draft(lease).pk, viewer_user).ok


def test_send_from_the_invoices_tab_returns_to_the_tab() -> None:
    lease = a_world()
    assert lease.operator is not None
    with_customer(lease.operator)
    draft = a_draft(lease)
    billing, _ = client_for(Role.BILLING)
    url = reverse("ledger:invoices:send", kwargs={"pk": draft.pk}) + f"?store={lease.store_id}"
    response = billing.post(url)
    assert (response.status_code, response["Location"]) == (302, tab(lease.store_id, "invoices"))
    draft.refresh_from_db()
    assert draft.status == InvoiceDraftStatus.SENT
    # A second Send changes nothing and stays on the draft.
    again = billing.post(url)
    assert again["Location"] == f"/invoices/drafts/{draft.pk}/{known(lease)}"
    assert Invoice.objects.count() == 1
    html = page(billing, store_page(lease.store_id, tab="invoices"))
    assert f'href="/invoices/drafts/{draft.pk}/cancel/"' in html


def test_the_schedules_tab_lists_schedules_autopay_charges_and_live_plans() -> None:
    lease = a_world()
    assert lease.operator is not None
    CollectionSchedule.objects.create(
        lease=lease,
        kind=ScheduleKind.RENT,
        seq=1,
        debit_day=5,
        amount=D("3500.00"),
        state=ScheduleState.ACTIVE,
    )
    CollectionSchedule.objects.create(
        lease=lease,
        kind=ScheduleKind.INSURANCE,
        seq=1,
        debit_day=10,
        amount=D("695.00"),
        state=ScheduleState.PAUSED,
    )
    a_one_off_charge(
        lease.operator,
        "60.00",
        date(2026, 10, 30),
        lease=lease,
        collection="autopay_due",
        description="Key copy",
    )
    rent = a_rent_charge(lease, OCTOBER, due="1000.00")
    plan = InstallmentPlan.objects.create(
        rent_charge=rent,
        operator=lease.operator,
        lease=lease,
        count=2,
        first_due=date(2026, 10, 20),
        cadence=InstallmentCadence.MONTHLY,
        collection=InstallmentCollection.MANUAL,
        confirmed_at=NOW,
    )
    html = page(client_for(Role.VIEWER)[0], store_page(lease.store_id, tab="schedules"))
    assert 'id="tab-schedules"' in html
    assert "2026-11-05" in html and "Insurance" in html and "Paused" in html
    assert "Key copy" in html and f"/plans/{plan.pk}/" in html


def test_the_contacts_and_documents_tabs_render_the_existing_sections() -> None:
    lease = a_world()
    billing, _ = client_for(Role.BILLING)
    contacts = page(billing, store_page(lease.store_id, tab="contacts"))
    assert 'id="tab-contacts"' in contacts and 'id="add-store-contact"' in contacts
    documents = page(billing, store_page(lease.store_id, tab="documents"))
    assert 'id="tab-documents"' in documents and 'id="documents"' in documents
    # A tab is built only when it is open.
    assert 'id="documents"' not in page(billing, store_page(lease.store_id))


def test_the_history_tab_is_for_admins_and_scoped_to_the_store() -> None:
    lease = a_world()
    charge = a_rent_charge(lease, OCTOBER, due="1000.00")
    for entity_type, entity_id, marker in (
        ("store", lease.store_id, "STORE-ROW"),
        ("lease", lease.pk, "LEASE-ROW"),
        ("rent_charge", charge.pk, "CHARGE-ROW"),
        ("store", a_store().pk, "ELSEWHERE-ROW"),
    ):
        AuditLog.objects.create(
            action="test.row",
            entity_type=entity_type,
            entity_id=str(entity_id),
            after={"marker": marker},
        )
    admin, _ = client_for(Role.ADMIN)
    html = page(admin, store_page(lease.store_id, tab="history"))
    for marker in ("STORE-ROW", "LEASE-ROW", "CHARGE-ROW"):
        assert marker in html, marker
    assert "ELSEWHERE-ROW" not in html
    assert f"store_of={lease.store_id}" in html
    audit_page = page(admin, f"/audit/?store_of={lease.store_id}&from=2026-01-01&to=2026-12-31")
    assert "STORE-ROW" in audit_page and "ELSEWHERE-ROW" not in audit_page
    billing = page(client_for(Role.BILLING)[0], store_page(lease.store_id, tab="history"))
    assert 'id="tab-charges"' in billing and "tab=history" not in billing


# --- The query budget (D1) -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["charges", "invoices", "payments", "schedules", "history"])
def test_the_header_and_the_open_tab_stay_within_the_query_budget(name: str) -> None:
    def build(store: Store) -> int:
        with CaptureQueriesContext(connection) as queries:
            workspace.store_workspace(store, TODAY, Role.ADMIN, tab=name)
        return len(queries)

    lease = a_world()
    assert lease.operator is not None
    charge = a_rent_charge(lease, OCTOBER, due="1000.00")
    a_payment(charge, "100.00", TODAY)
    an_invoice(lease.operator, 99, "1000.00", rent_charge=charge)
    a_draft(lease)
    small = build(lease.store)
    for month in range(1, 9):
        more = a_rent_charge(lease, date(2026, month, 1), due="100.00")
        a_payment(more, "10.00", date(2026, month, 5))
        a_one_off_charge(lease.operator, "20.00", date(2026, month, 3), lease=lease)
        an_invoice(lease.operator, 100 + month, "20.00", rent_charge=more)
    a_draft(lease)
    large = build(lease.store)
    assert small <= 25 and large <= 25, (small, large)
    assert large == small


# --- The lease page (D4) ---------------------------------------------------------------------


def test_the_lease_page_keeps_the_terms_and_links_the_store_tabs() -> None:
    lease = a_world()
    assert lease.operator is not None
    a_rent_charge(lease, OCTOBER, due="1000.00")
    a_one_off_charge(lease.operator, "250.00", TODAY, lease=lease)
    html = page(client_for(Role.ADMIN)[0], f"/leases/{lease.pk}/")
    assert 'id="setup-charges"' not in html and "Broken shelf" not in html
    assert 'id="export-charges"' not in html
    assert f'href="/stores/{lease.store_id}/?tab=charges#tab-charges"' in html
    assert 'id="rent-history"' in html and 'id="schedules"' in html
    assert 'id="prorate-first-month"' in html and "<h2>Notes</h2>" in html


# --- Links and navigation (D5) -------------------------------------------------------------------


def test_detail_pages_link_back_to_the_store() -> None:
    lease = a_world()
    assert lease.operator is not None
    rent = a_rent_charge(lease, OCTOBER, due="1000.00")
    payment = a_payment(rent, "100.00", TODAY)
    one_off = a_one_off_charge(lease.operator, "250.00", TODAY, lease=lease)
    draft = a_draft(lease)
    plan = InstallmentPlan.objects.create(
        rent_charge=rent,
        operator=lease.operator,
        lease=lease,
        count=2,
        first_due=date(2026, 10, 20),
        cadence=InstallmentCadence.MONTHLY,
        collection=InstallmentCollection.MANUAL,
        confirmed_at=timezone.now(),
    )
    billing, _ = client_for(Role.BILLING)
    crumb = (
        f'<p class="crumb" id="store-crumb"><a href="/stores/{lease.store_id}/">'
        "&larr; Store 2477</a></p>"
    )
    for url in (
        f"/charges/{rent.pk}/",
        f"/one-off-charges/{one_off.pk}/",
        f"/invoices/drafts/{draft.pk}/",
        f"/plans/{plan.pk}/",
        f"/payments/{payment.pk}/",
    ):
        assert crumb in page(billing, url), url


def test_the_attention_list_links_the_store_tabs() -> None:
    lease = a_world()
    Lease.objects.filter(pk=lease.pk).update(ended_on=date(2026, 11, 1))
    schedule = CollectionSchedule.objects.create(
        lease=lease,
        kind=ScheduleKind.RENT,
        seq=1,
        debit_day=5,
        amount=D("3500.00"),
        state=ScheduleState.ACTIVE,
        needs_review=True,
    )
    an_insurance_charge(lease, 2026, date(2026, 10, 30), None, needs_premium=True)
    items = {item.kind: item.url for item in attention.attention(TODAY).items}
    assert schedule.pk
    assert items[attention.LEASE_ENDING] == tab(lease.store_id, "schedules")
    assert items[attention.SCHEDULE_REVIEW] == tab(lease.store_id, "schedules")
    assert items[attention.INSURANCE_PREMIUM_MISSING] == tab(lease.store_id, "charges")


def test_the_run_page_links_stores() -> None:
    lease = a_world()
    Lease.objects.filter(pk=lease.pk).update(debit_day=15)
    html = page(client_for(Role.BILLING)[0], "/run/")
    assert f'href="/stores/{lease.store_id}/">2477</a>' in html
    assert f'href="/leases/{lease.pk}/">2477</a>' not in html


def test_the_nav_moves_rent_run_under_admin_and_marks_the_current_section() -> None:
    def nav(client: Client, url: str) -> str:
        html = page(client, url)
        return html[html.index('<nav aria-label="Main">') : html.index("</nav>")]

    viewer, billing, admin = (
        client_for(role)[0] for role in (Role.VIEWER, Role.BILLING, Role.ADMIN)
    )
    for client in (viewer, billing, admin):
        text = nav(client, "/stores/")
        assert '<details class="nav-menu" id="nav-admin-menu"><summary>Admin</summary>' in text
        assert 'id="nav-django-admin"' in text
        assert text.index("<details") < text.index('id="nav-django-admin"')
        assert '<a href="/stores/" aria-current="page">Stores</a>' in text
        assert text.count('aria-current="page"') == 1
    assert 'id="nav-run"' not in nav(viewer, "/stores/")
    assert 'id="nav-run"' in nav(billing, "/stores/")
    assert 'id="nav-audit"' not in nav(billing, "/stores/")
    assert 'id="nav-audit"' in nav(admin, "/stores/")
    run = nav(billing, "/run/")
    assert '<a href="/run/" id="nav-run" aria-current="page">Rent run</a>' in run
    assert run.index("<details") < run.index('href="/run/"')
    assert 'aria-current="page"' not in nav(
        billing, f"/charges/{a_rent_charge(a_world(), OCTOBER).pk}/"
    )


# --- "Already on draft" (finding 12d-1) ----------------------------------------------------------


def test_the_already_on_draft_refusal_links_the_other_draft() -> None:
    lease = a_lease(
        a_store("6100"),
        an_operator("Rent Ops"),
        started=date(2026, 10, 1),
        channel=PaymentChannel.WIRE,
        debit_day=1,
        rent="1000.00",
    )
    assert lease.operator is not None
    billing, user = client_for(Role.BILLING)
    rent_line = LineInput(
        item="",
        description="",
        quantity=None,
        unit_amount=None,
        rent_lease=lease.pk,
        rent_month=OCTOBER,
    )
    first = invoice_builder.create_draft(
        lease.operator, lease, "", None, [rent_line], user, today=TODAY
    )
    assert first.ok and first.draft is not None, first.message
    second = a_draft(lease, "Poster")
    data = page_data(
        [{"kind": "rent", "rent_lease": str(lease.pk), "rent_month": "2026-10"}],
        lease=str(lease.pk),
    )
    billing.post(f"/invoices/drafts/{second.pk}/edit/", data)
    html = page(billing, f"/invoices/drafts/{second.pk}/")
    number = first.draft.number
    assert f"is already on draft {number}" not in html  # the number is a link
    assert f'<a href="/invoices/drafts/{first.draft.pk}/" class="draft-link">{number}</a>.' in html


def test_a_form_opened_elsewhere_returns_to_a_local_next_only() -> None:
    lease = a_world()
    fee = FeeType.objects.create(code=f"sign-{uuid.uuid4().hex[:6]}", name="Sign", active=True)
    billing, _ = client_for(Role.BILLING)
    url = reverse("ledger:leases:add_setup_charge", kwargs={"pk": lease.pk})
    data = {"fee_type": str(fee.pk), "price": "75.00", "due_date": "2026-10-20"}
    back = f"/operators/{lease.operator_id}/"
    response = billing.post(f"{url}?next={back}", data)
    assert response["Location"] == back
    OneOffCharge.objects.filter(fee_type=fee).delete()
    response = billing.post(f"{url}?next=https://evil.example/", data)
    charge = OneOffCharge.objects.get(fee_type=fee)
    assert response["Location"] == f"/one-off-charges/{charge.pk}/"


def test_a_store_or_lease_of_another_store_answers_404() -> None:
    mine, theirs = a_world(), a_world("8800", "Their Ops")
    billing, _ = client_for(Role.BILLING)
    admin, _ = client_for(Role.ADMIN)
    for client, url in (
        (billing, f"/leases/{mine.pk}/setup-charges/new/?store={theirs.store_id}"),
        (billing, f"/leases/{mine.pk}/setup-charges/new/?store={mine.store_id}&lease={theirs.pk}"),
        (billing, f"/stores/{mine.store_id}/contacts/new/?store={mine.store_id}&lease={theirs.pk}"),
        (billing, f"/invoices/new/store/{mine.store_id}/?store={theirs.store_id}"),
        (billing, f"/invoices/new/operator/{mine.operator_id}/?store={theirs.store_id}"),
        (admin, f"/leases/{mine.pk}/edit/?store={mine.store_id}&lease={theirs.pk}"),
        (admin, f"/leases/{mine.pk}/edit/?store=not-a-uuid"),
    ):
        assert client.get(url).status_code == 404, url
