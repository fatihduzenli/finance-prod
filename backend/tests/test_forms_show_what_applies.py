"""Forms show only what applies (docs/qa/PLAN-012c-design-pass.md D8; §6 C).

For each form in the plan's walk: the field or section that cannot be used in a state is
absent from the rendered page in that state and present otherwise (the real pages, the
real roles), and a hidden field posted anyway is refused or accepted exactly as before
the batch (hiding never relaxes validation). The items the plan marks "already" have a
test that shows the current behaviour. Dependent fields (shown by ``ledger/shown_when.js``
from another control on the same page) are in the markup in every state, so the page
works without the script; the browser behaviour is in ``tests/e2e/test_dependent_fields.py``.
The adversarial cases are the QA owner's.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from ledger.forms import SHOWN_WHEN, TWICE_MONTHLY_ID
from ledger.models import (
    DepositResolution,
    Document,
    DocumentKind,
    FeeType,
    Lease,
    LeaseCredit,
    LeaseEndReason,
    LeaseTransfer,
    Operator,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RefundMethod,
)
from ledger.services import deposits, lease_end, statements, transfer
from payments.models import ExternalRef, Job
from tests import test_payment_allocation as allocation_tests
from tests.document_files import make_pdf
from tests.receivables_helpers import a_lease, a_one_off_charge, a_rent_charge, a_store, an_operator
from tests.test_documents import post_file
from tests.test_lease_end import (
    ENDED,
    TODAY,
    a_store_lease,
    deductions,
    deposit,
    draft,
    fresh,
    october,
)
from tests.test_lease_views import edit_data
from tests.test_lease_views import form_data as lease_form_data
from tests.test_operator_views import client_for
from tests.test_operator_views import form_data as operator_form_data

pytestmark = pytest.mark.django_db

D = Decimal
SCRIPT = "ledger/shown_when.js"


@pytest.fixture(autouse=True)
def storage(monkeypatch: pytest.MonkeyPatch) -> MemoryDocumentStorage:
    """Document storage on, in memory (the Documents section and the lease-end PDF)."""
    memory = MemoryDocumentStorage()
    monkeypatch.setattr("documents_storage.get_storage", lambda: memory)
    return memory


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


@pytest.fixture
def admin() -> tuple[Client, User]:
    return client_for(Role.ADMIN)


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def named(html: str, name: str) -> bool:
    """Whether a control named ``name`` is rendered."""
    return f'name="{name}"' in html


def field_of(html: str, control_id: str) -> str:
    """The opening tag of the control with ``control_id``."""
    found = re.search(rf'<(?:input|select|textarea)[^>]*id="{control_id}"[^>]*>', html)
    assert found is not None, control_id
    return found.group(0)


# --- Lease-end settlement: the refund's fields only when a refund is due ------------------


def a_refund_draft() -> Any:
    """October paid and a $1,000 deposit held: the deposit comes back, a refund is due."""
    lease = a_store_lease()
    october(lease, paid="2000.00")
    deposit(lease, "1000.00")
    end = draft(lease)
    assert statements.final(end).refund_due > 0
    return end


def an_owing_draft() -> Any:
    """October unpaid, no deposit: the operator owes, nothing is refunded."""
    lease = a_store_lease()
    october(lease)
    end = draft(lease)
    figures = statements.final(end)
    assert figures.refund_due == 0 and figures.amount_owed > 0
    return end


def test_the_settlement_shows_the_refund_fields_when_a_refund_is_due(
    admin: Any, pinned: date
) -> None:
    client, _ = admin
    end = a_refund_draft()
    html = page(client, f"/lease-ends/{end.pk}/settlement/")
    for name in ("method", "refund_on", "reference", "note"):
        assert named(html, name), name
    assert 'id="refund-due"' in html and 'id="amount-owed"' not in html


def test_the_settlement_shows_the_amount_owed_and_save_only_when_the_operator_owes(
    admin: Any, pinned: date
) -> None:
    client, _ = admin
    end = an_owing_draft()
    html = page(client, f"/lease-ends/{end.pk}/settlement/")
    for name in ("method", "refund_on", "reference"):
        assert not named(html, name), name
    assert 'id="amount-owed"' in html and 'id="refund-due"' not in html
    assert "Save and continue" in html and named(html, "note")
    # Saved without the hidden fields: no refund method is set, and the wizard goes on.
    response = client.post(f"/lease-ends/{end.pk}/settlement/", {"note": "Owes October."})
    assert response["Location"] == f"/lease-ends/{end.pk}/confirm/"
    row = fresh(end)
    assert (row.refund_method, row.refund_on, row.settlement_note) == ("", None, "Owes October.")


def test_a_hidden_refund_field_posted_is_checked_as_before(admin: Any, pinned: date) -> None:
    client, _ = admin
    end = an_owing_draft()
    # The service's rules are unchanged: a refund date in the future is refused and the
    # refused field comes back on the page with its error, whatever the state.
    later = (TODAY + timedelta(days=1)).isoformat()
    client.post(
        f"/lease-ends/{end.pk}/settlement/",
        {"method": "zelle", "refund_on": later, "reference": "", "note": ""},
    )
    html = page(client, f"/lease-ends/{end.pk}/settlement/")
    assert lease_end.REFUND_DATE_FUTURE in html and named(html, "refund_on")
    assert fresh(end).refund_method == ""
    # A valid method posted is saved as before the batch (the service never refused it).
    response = client.post(
        f"/lease-ends/{end.pk}/settlement/",
        {"method": "zelle", "refund_on": TODAY.isoformat(), "reference": "ZL-1", "note": ""},
    )
    assert response["Location"] == f"/lease-ends/{end.pk}/confirm/"
    assert fresh(end).refund_method == RefundMethod.ZELLE


# --- The deductions table: the eight rows, the script's "+ Add deduction" ----------------------
# PLAN-012f-a D8 replaced PLAN-012c D8's disclosure: the eight rows are always in the markup (no
# script: all shown); ledger/deduction_rows.js hides the empty rows after the last filled one
# and reveals its dashed "+ Add deduction" button, which the markup keeps hidden.


def _rows(html: str) -> list[str]:
    table = html.split('id="deduction-rows"', 1)[1].split("</table>", 1)[0]
    return re.findall(r'<tr class="deduction-row"[^>]*>', table)


def test_with_no_deduction_every_row_is_in_the_markup_and_the_add_control_waits_for_script(
    admin: Any, pinned: date
) -> None:
    client, _ = admin
    lease = a_store_lease()
    october(lease, paid="2000.00")  # nothing open, no deposit: nothing is suggested
    end = draft(lease)
    html = page(client, f"/lease-ends/{end.pk}/statement/")
    assert "<details" not in html.split('id="lease-end-deductions"', 1)[1].split("</form>", 1)[0]
    rows = _rows(html)
    assert len(rows) == 8 and not any("hidden" in row for row in rows)
    assert re.search(
        r'<button class="btn add-deduction" type="button" id="add-deduction" hidden>', html
    )
    assert "ledger/deduction_rows.js" in html
    # The rows still post as before: a deduction typed in the first row is saved.
    response = client.post(
        f"/lease-ends/{end.pk}/statement/",
        {"kind_1": "cleaning", "description_1": "Floors", "amount_1": "150.00"},
    )
    assert response["Location"] == f"/lease-ends/{end.pk}/settlement/"
    assert [row.amount for row in fresh(end).deductions.all()] == [D("150.00")]


def test_a_saved_or_suggested_deduction_fills_its_row(admin: Any, pinned: date) -> None:
    client, _ = admin
    saved = a_refund_draft()
    assert deductions(saved, ("cleaning", "Floors", "150.00")).ok
    suggested = draft(a_store_lease())  # October open and nothing paid: no row suggested
    lease = suggested.lease
    october(lease)
    deposit(lease, "500.00")  # a deposit and an open balance: the offset is suggested
    for end in (saved, suggested):
        html = page(client, f"/lease-ends/{end.pk}/statement/")
        assert len(_rows(html)) == 8, end.pk
        first = html.split('class="deduction-row"', 2)[1]
        assert 'name="amount_1"' in first and 'value="' in first.split('name="amount_1"')[1][:60]


def test_a_refused_deduction_comes_back_in_its_row(admin: Any, pinned: date) -> None:
    client, _ = admin
    lease = a_store_lease()
    october(lease, paid="2000.00")
    end = draft(lease)
    client.post(f"/lease-ends/{end.pk}/statement/", {"kind_1": "cleaning", "amount_1": "-5"})
    html = page(client, f"/lease-ends/{end.pk}/statement/")
    first = html.split('class="deduction-row"', 2)[1]
    assert "errorlist" in first and 'value="-5"' in first
    assert not fresh(end).deductions.exists()


# --- Lease-end terms: the reason note, a dependent field ---------------------------------


def test_the_reason_note_is_a_dependent_field_present_in_the_markup(
    admin: Any, pinned: date
) -> None:
    client, _ = admin
    for reason in ("moved_out", "other"):
        end = draft(
            a_store_lease(), reason=reason, note="Shop closed." if reason == "other" else ""
        )
        html = page(client, f"/lease-ends/{end.pk}/terms/")
        tag = field_of(html, "id_reason_note")
        # In the markup whatever the reason (the no-script path), shown by the script
        # while the reason is Other.
        assert f'{SHOWN_WHEN}="id_reason={LeaseEndReason.OTHER.value}"' in tag, reason
        assert SCRIPT in html, reason


def test_the_reason_note_rules_are_unchanged(admin: Any, pinned: date) -> None:
    client, _ = admin
    end = draft(a_store_lease())
    terms = f"/lease-ends/{end.pk}/terms/"
    client.post(terms, {"ended_on": ENDED.isoformat(), "reason": "other", "reason_note": ""})
    html = page(client, terms)
    assert lease_end.REASON_NOTE_REQUIRED in html
    assert 'class="field has-error"' in html.split('id="id_reason_note"')[0].rsplit("<div", 1)[1]
    # A note posted with another reason is kept, as before the batch.
    client.post(
        terms,
        {"ended_on": ENDED.isoformat(), "reason": "moved_out", "reason_note": "Moved to 12B."},
    )
    row = fresh(end)
    assert (row.reason, row.reason_note) == ("moved_out", "Moved to 12B.")


# --- The transfer's incoming step: the deposit and the checklist ---------------------------


def an_incoming_step(user: User | None) -> LeaseTransfer:
    from tests.test_transfer import a_clean_lease, begin

    return begin(a_clean_lease(), user)


def test_the_incoming_step_shows_the_deposit_and_the_checklist_when_they_apply(
    admin: Any, pinned: date
) -> None:
    from tests.test_setup_charges import fee

    client, user = admin
    fee("sign_fee")
    row = an_incoming_step(user)
    html = page(client, f"/transfers/{row.pk}/incoming/")
    assert named(html, "deposit_amount")
    assert 'id="setup-rows"' in html and named(html, "setup_sign_fee_price")


def test_without_a_deposit_type_the_deposit_field_is_left_out_and_still_refused(
    admin: Any, pinned: date
) -> None:
    client, user = admin
    row = an_incoming_step(user)
    FeeType.objects.filter(code="security_deposit").update(active=False)
    assert transfer.deposit_type() is None
    path = f"/transfers/{row.pk}/incoming/"
    assert not named(page(client, path), "deposit_amount")
    data = {
        "started_on": (ENDED + timedelta(days=1)).isoformat(),
        "rent": "2500.00",
        "debit_day": "1",
        "payment_channel": "wire",
        "prorate_first_month": "on",
    }
    response = client.post(path, {**data, "deposit_amount": "3000.00"})
    assert response["Location"] == path
    html = page(client, path)
    # Refused as before, and the refused field comes back with its error.
    assert "no active security deposit item" in html and named(html, "deposit_amount")
    assert not transfer.incoming_set(LeaseTransfer.objects.get(pk=row.pk))
    response = client.post(path, data)
    assert response["Location"] == f"/transfers/{row.pk}/confirm/"
    assert LeaseTransfer.objects.get(pk=row.pk).deposit_amount is None


def test_without_a_checklist_item_the_setup_section_is_left_out(admin: Any, pinned: date) -> None:
    client, user = admin
    row = an_incoming_step(user)
    FeeType.objects.filter(refundable=False).update(show_in_setup=False)
    assert transfer.setup_types() == []
    html = page(client, f"/transfers/{row.pk}/incoming/")
    assert 'id="setup-rows"' not in html and 'id="setup-total"' not in html
    assert "<h2>Setup charges</h2>" not in html
    # A setup price posted anyway has no field to land in: nothing is charged, as before.
    data = {
        "started_on": (ENDED + timedelta(days=1)).isoformat(),
        "rent": "2500.00",
        "debit_day": "1",
        "payment_channel": "wire",
        "deposit_amount": "",
        "setup_sign_fee_price": "450.00",
    }
    client.post(f"/transfers/{row.pk}/incoming/", data)
    assert transfer.setup_lines(LeaseTransfer.objects.get(pk=row.pk)) == []


def test_the_override_form_shows_only_with_a_balance_already(admin: Any, pinned: date) -> None:
    from tests.test_transfer import deduct, ready

    client, user = admin
    row = ready(user)
    html = page(client, f"/transfers/{row.pk}/confirm/")
    assert 'id="debt-override-form"' not in html
    deduct(row, "2600.00", user)  # more than the $2,000 deposit: a balance remains
    html = page(client, f"/transfers/{row.pk}/confirm/")
    assert 'id="debt-override-form"' in html


# --- The lease form and page: the second debit day and autopay ------------------------------


def test_the_second_debit_day_follows_the_twice_monthly_tick(admin: Any) -> None:
    client, _ = admin
    monthly = a_lease(a_store("D8-1"), an_operator(), second_debit_day=None)
    twice = a_lease(a_store("D8-2"), an_operator(), debit_day=1, second_debit_day=15)
    for lease, ticked in ((monthly, False), (twice, True)):
        html = page(client, f"/leases/{lease.pk}/edit/")
        tick = field_of(html, TWICE_MONTHLY_ID)
        assert "name=" not in tick, "the tick must never be posted"
        assert (" checked" in tick) is ticked, lease.pk
        assert 'id="twice-monthly-field" hidden data-reveal' in html
        # The field itself is always in the markup (the no-script path).
        assert f'{SHOWN_WHEN}="{TWICE_MONTHLY_ID}"' in field_of(html, "id_second_debit_day")
        assert SCRIPT in html
    html = page(client, "/leases/new/")
    assert " checked" not in field_of(html, TWICE_MONTHLY_ID)


def test_the_second_debit_day_rules_are_unchanged(admin: Any) -> None:
    client, _ = admin
    monthly = a_lease(a_store("D8-3"), an_operator(), debit_day=1, channel=PaymentChannel.WIRE)
    # Posted without the tick (which is never sent), a second day is saved as before.
    response = client.post(f"/leases/{monthly.pk}/edit/", edit_data(monthly, second_debit_day="15"))
    assert response.status_code == 302
    assert Lease.objects.get(pk=monthly.pk).second_debit_day == 15
    response = client.post(f"/leases/{monthly.pk}/edit/", edit_data(monthly, second_debit_day="1"))
    assert response.status_code == 302
    html = page(client, f"/leases/{monthly.pk}/edit/")
    assert "must differ" in html and " checked" in field_of(html, TWICE_MONTHLY_ID)
    assert Lease.objects.get(pk=monthly.pk).second_debit_day == 15
    # Created without a second day, as before.
    response = client.post("/leases/new/", lease_form_data())
    assert response.status_code == 302


def test_enable_autopay_shows_only_on_a_provider_ach_lease(admin: Any) -> None:
    client, _ = admin
    ach = a_lease(a_store("D8-4"), an_operator(), channel=PaymentChannel.PROVIDER_ACH)
    wire = a_lease(a_store("D8-5"), an_operator(), channel=PaymentChannel.WIRE)
    html = page(client, f"/leases/{ach.pk}/")
    assert f'href="/leases/{ach.pk}/enrol/"' in html and 'id="enrolment-blockers"' in html
    html = page(client, f"/leases/{wire.pk}/")
    assert f'href="/leases/{wire.pk}/enrol/"' not in html
    assert 'id="enrolment-blockers"' not in html and 'id="enrolment-notes"' not in html
    assert 'id="schedules"' in html
    # The enrol page and its refusal are unchanged: posted on a wire lease, nothing starts.
    response = client.post(f"/leases/{wire.pk}/enrol/", {"payment_method_id": "pm-1"})
    assert response.status_code == 302
    from ledger.models import CollectionSchedule

    assert not CollectionSchedule.objects.filter(lease=wire).exists()


# --- The operator form and page: consents and the provider fields ------------------------


def test_the_consents_are_dependent_fields_of_the_phone_and_the_email(admin: Any) -> None:
    client, _ = admin
    bare = Operator.objects.create(display_name="Bare Co")
    for path in ("/operators/new/", f"/operators/{bare.pk}/edit/"):
        html = page(client, path)
        assert f'{SHOWN_WHEN}="id_sms_phone|id_phone"' in field_of(html, "id_sms_consent_at")
        assert f'{SHOWN_WHEN}="id_email"' in field_of(html, "id_email_consent_at")
        assert SCRIPT in html, path


def test_a_consent_posted_without_a_phone_or_email_is_saved_as_before(admin: Any) -> None:
    client, _ = admin
    bare = Operator.objects.create(display_name="Bare Co")
    data = operator_form_data(
        display_name="Bare Co",
        email="",
        phone="",
        sms_phone="",
        sms_consent_at="2026-10-01T09:30:00",
    )
    data.pop("creation_key")
    response = client.post(f"/operators/{bare.pk}/edit/", data)
    assert response.status_code == 302, response.content.decode()[:400]
    row = Operator.objects.get(pk=bare.pk)
    assert row.sms_consent_at is not None and row.email_consent_at is None


def test_the_provider_fields_show_only_for_an_operator_with_a_provider_customer() -> None:
    viewer, _ = client_for(Role.VIEWER)
    without = Operator.objects.create(display_name="Unlinked Co")
    linked = Operator.objects.create(display_name="Linked Co", email="linked@example.com")
    ExternalRef.objects.create(
        entity_type="operator",
        entity_id=linked.pk,
        provider="stax_pay",
        external_kind="customer",
        external_id="cus-d8-linked",
    )
    html = page(viewer, f"/operators/{without.pk}/")
    assert 'id="sync-status"' in html
    for marker in ('id="provider-customer"', 'id="provider-method"', 'id="provider-enrolment"'):
        assert marker not in html, marker
    html = page(viewer, f"/operators/{linked.pk}/")
    for marker in ('id="provider-customer"', 'id="provider-method"', 'id="provider-enrolment"'):
        assert marker in html, marker
    assert "cus-d8-linked" not in html  # masked, as before


# --- The store page: lease actions on a vacant store (already) ------------------------------


def test_a_vacant_store_offers_no_lease_action_already() -> None:
    billing, _ = client_for(Role.BILLING)
    lease = a_lease(a_store("D8-6"), an_operator())
    vacant = a_store("D8-7")
    leased_html = page(billing, f"/stores/{lease.store_id}/")
    vacant_html = page(billing, f"/stores/{vacant.pk}/")
    # PLAN-012e-2 D5: the bar is Create invoice and Add payment for billing; a vacant
    # store gives billing no bar (Upload is on the Documents tab).
    for marker in ('id="record-payment"', 'id="create-invoice"', 'id="action-bar"'):
        assert marker in leased_html, marker
        assert marker not in vacant_html, marker
    assert "/schedule-payment/" not in leased_html and "/schedule-payment/" not in vacant_html
    response = billing.post(f"/stores/{vacant.pk}/payments/new/", {"amount": "10.00"})
    assert response.status_code == 302 and not Payment.objects.exists()


# --- The builder: rent fields and the Save tick (already, by its script) ---------------------


def test_the_builder_marks_the_rent_fields_and_the_save_tick_for_its_script_already() -> None:
    billing, _ = client_for(Role.BILLING)
    lease = a_lease(a_store("D8-8"), an_operator())
    html = page(billing, f"/invoices/new/operator/{lease.operator_id}/")
    # PLAN-012f-a D5: one rent-field box (lease and month) under the row's Item box.
    assert html.count('class="rent-field"') == 1 and 'class="save-item"' in html
    assert "ledger/invoice_lines.js" in html
    from django.contrib.staticfiles import finders

    found = finders.find("ledger/invoice_lines.js")
    assert isinstance(found, str)
    with open(found, encoding="utf-8") as handle:
        script = handle.read()
    assert '".rent-field"' in script and "-save_item" in script


# --- Record a payment: the credit inputs (already) and the keep-credit tick ------------------


def test_the_credit_inputs_show_only_when_a_credit_is_held_already() -> None:
    lease = allocation_tests.a_world("D8-9")
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    client = allocation_tests.billing()
    url = allocation_tests.url(lease)
    html = page(client, url)
    assert not named(html, f"credit_{rent.pk}") and 'id="credit-held"' not in html
    # A credit posted without one held is refused, nothing written, as before.
    before = allocation_tests.counts()
    client.post(url, allocation_tests.form("", credit={rent.pk: "10.00"}))
    assert allocation_tests.counts() == before
    assert "more than the credit held" in page(client, url)
    response = client.post(url, allocation_tests.form("50.00", keep_credit="on"))
    assert response.status_code == 302 and LeaseCredit.objects.get().remaining == D("50.00")
    html = page(client, url)
    assert named(html, f"credit_{rent.pk}") and 'id="credit-held"' in html


def test_the_keep_credit_tick_is_in_the_markup_for_the_script(monkeypatch: Any) -> None:
    lease = allocation_tests.a_world("D8-10")
    a_one_off_charge(allocation_tests.operator_of(lease), "250.00", date(2026, 9, 20), lease=lease)
    client = allocation_tests.billing()
    html = page(client, allocation_tests.url(lease))
    wrapper = html.split('<div id="keep-credit-field">', 1)
    assert len(wrapper) == 2 and 'name="keep_credit"' in wrapper[1].split("</div>", 1)[0]
    assert "ledger/payment_allocation.js" in html
    from django.contrib.staticfiles import finders

    found = finders.find("ledger/payment_allocation.js")
    assert isinstance(found, str)
    with open(found, encoding="utf-8") as handle:
        script = handle.read()
    assert "keep-credit-field" in script and "keepTick.disabled" in script


def test_keep_credit_is_checked_by_the_server_as_before() -> None:
    lease = allocation_tests.a_world("D8-11")
    other = a_one_off_charge(
        allocation_tests.operator_of(lease), "250.00", date(2026, 9, 20), lease=lease
    )
    client = allocation_tests.billing()
    url = allocation_tests.url(lease)
    # Fully allocated with the tick: nothing is left, so no credit is kept.
    response = client.post(
        url, allocation_tests.form("250.00", {other.pk: "250.00"}, keep_credit="on")
    )
    assert response.status_code == 302
    assert not LeaseCredit.objects.exists() and Payment.objects.count() == 1
    # Something left without the tick: refused, nothing written, as before.
    second = a_one_off_charge(
        allocation_tests.operator_of(lease), "100.00", date(2026, 9, 25), lease=lease
    )
    before = allocation_tests.counts()
    client.post(url, allocation_tests.form("150.00", {second.pk: "100.00"}))
    assert allocation_tests.counts() == before


# --- The deposit resolution: the transfer-to-lease field -------------------------------------


def test_the_transfer_to_lease_field_shows_only_when_another_lease_exists(admin: Any) -> None:
    client, _ = admin
    owner = an_operator("Two Stores Co")
    lease = a_lease(a_store("D8-12"), owner, channel=PaymentChannel.WIRE)
    charge = deposit(lease, "1000.00")
    path = f"/leases/{lease.pk}/deposits/{charge.pk}/resolve/"
    html = page(client, path)
    assert not named(html, "to_lease") and named(html, "kind") and named(html, "note")
    other = a_lease(a_store("D8-13"), owner, channel=PaymentChannel.WIRE)
    html = page(client, path)
    assert named(html, "to_lease") and f'value="{other.pk}"' in html


def test_a_transfer_to_lease_posted_without_another_lease_is_refused_as_before(
    admin: Any, pinned: date
) -> None:
    client, _ = admin
    lease = a_store_lease()
    charge = deposit(lease, "1000.00")
    # Ended outside the wizard (whose confirm resolves the deposit itself).
    Lease.objects.filter(pk=lease.pk).update(ended_on=ENDED)
    stranger = a_lease(a_store("D8-14"), an_operator("Stranger Co"), channel=PaymentChannel.WIRE)
    path = f"/leases/{lease.pk}/deposits/{charge.pk}/resolve/"
    assert not named(page(client, path), "to_lease")
    response = client.post(
        path,
        {
            "kind": "transferred",
            "amount": "1000.00",
            "resolved_on": TODAY.isoformat(),
            "method": "",
            "to_lease": str(stranger.pk),
            "note": "Moved to the other store.",
        },
    )
    assert response["Location"] == path
    html = page(client, path)
    # The refusal comes back on the field, which is shown with its error.
    assert named(html, "to_lease") and 'class="field has-error"' in html
    assert deposits.TARGET_OTHER_OPERATOR in html
    assert not DepositResolution.objects.exists()


# --- The return queue: Reschedule only on a returned debit -----------------------------------


def a_returned(method: str) -> Payment:
    lease = a_lease(a_store(), an_operator())
    charge = a_rent_charge(lease, date(2026, 9, 1), due="900.00")
    return Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=method,
        amount=D("900.00"),
        status=PaymentStatus.RETURNED,
        returned_at=timezone.now(),
        return_code="R01",
        note="Received." if method != PaymentMethod.ACH else "",
    )


def test_reschedule_shows_only_on_a_returned_ach_debit() -> None:
    billing, _ = client_for(Role.BILLING)
    debit = a_returned(PaymentMethod.ACH)
    wire = a_returned(PaymentMethod.WIRE)
    html = page(billing, "/returns/")
    assert f'id="reschedule-{debit.pk}"' in html
    assert f'id="reschedule-{wire.pk}"' not in html
    # Every return keeps Resolve with a note.
    assert f'action="/returns/{wire.pk}/resolve/"' in html
    assert f'action="/returns/{debit.pk}/resolve/"' in html


def test_a_reschedule_posted_for_a_hidden_row_is_checked_as_before() -> None:
    billing, _ = client_for(Role.BILLING)
    wire = a_returned(PaymentMethod.WIRE)
    jobs = Job.objects.count()
    # The same refusal as before the batch: no date, refused; nothing written.
    response = billing.post(f"/returns/{wire.pk}/reschedule/", {"redebit_date": ""})
    assert response.status_code == 302
    assert Job.objects.count() == jobs
    assert Payment.objects.get(pk=wire.pk).resolved_at is None


# --- The installment pages: Retry only on a failed release (already) -------------------------


def test_retry_shows_only_on_a_failed_release_already() -> None:
    billing, _ = client_for(Role.BILLING)
    lease = a_lease(a_store("D8-15"), an_operator())
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    html = page(billing, f"/charges/{rent.pk}/")
    assert 'id="retry-provider"' not in html
    # Posted anyway: nothing to retry, nothing queued, as before.
    jobs = Job.objects.count()
    response = billing.post(f"/charges/{rent.pk}/retry-release/")
    assert response.status_code == 302 and Job.objects.count() == jobs
    assert "Nothing to retry on this charge." in page(billing, f"/charges/{rent.pk}/")


# --- The document upload: the owner's kinds ---------------------------------------------------


UPLOADABLE = [
    DocumentKind.LEASE_AGREEMENT,
    DocumentKind.AMENDMENT,
    DocumentKind.FLOOR_PLAN,
    DocumentKind.PHOTO,
    DocumentKind.NOTICE,
    DocumentKind.OTHER,
]


def kind_options(html: str) -> list[str]:
    select = html.split('name="kind"', 1)[1].split("</select>", 1)[0]
    return re.findall(r'<option value="([^"]*)"', select)


def test_the_upload_offers_the_owners_kinds_only_already() -> None:
    billing, _ = client_for(Role.BILLING)
    lease = a_lease(a_store("D8-16"), an_operator())
    for path in (f"/stores/{lease.store_id}/?tab=documents", f"/leases/{lease.pk}/"):
        html = page(billing, path)
        assert kind_options(html) == [str(kind) for kind in UPLOADABLE], path
    # A kind the store does not keep, posted anyway, is refused as before.
    response = post_file(
        billing, lease.store, "Evidence.pdf", make_pdf([["Evidence"]]), kind="dispute_evidence"
    )
    assert response.status_code in (200, 302)
    assert not Document.objects.exists()
