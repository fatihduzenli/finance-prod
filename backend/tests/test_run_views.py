"""Rent run and charge screens (PLAN-007 §6 rows 5 to 10 through the views).

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`; ``timezone.localdate``
is pinned so the run day does not drift with the clock.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client, override_settings

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Invoice,
    Operator,
    Payment,
    PaymentChannel,
    RentCharge,
    RentChargeStatus,
    Store,
    StoreStatus,
)
from ledger.services import rent_run
from payments import invoice_sync
from payments.fake import FakeProvider
from payments.health import BREAKER_OPEN, BUDGET_EXHAUSTED
from payments.models import ExternalRef
from payments.stax.breaker import CircuitBreaker, Outcome, reset_breaker
from payments.stax.budget import FailureBudget, reset_budget
from payments.stax.state import MemoryStateStore
from tests.auth_helpers import iter_routes, make_user, verified_client
from tests.job_helpers import run_queued_jobs
from tests.test_operator_views import client_for
from tests.test_rent_run import enrolled, invoice, lease

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)
RUN = "/run/"
CREATE = "/run/create-charges/"
MATCH = "/run/match/"
POST_ROUTES = (
    "ledger:run:create_charges",
    "ledger:run:match",
    "ledger:charges:record_payment",
    "ledger:charges:retry_mark",
    "ledger:charges:link_invoice",
    # PLAN-011b D4: sending a step of the collections ladder by hand.
    "ledger:charges:send_step",
    # PLAN-012e D10: Split into installments.
    "ledger:charges:split",
    # PLAN-012b-b D13: repeat a failed release of a rent charge's Stax invoice or autopay move.
    "ledger:charges:retry_release",
)
READ_ROUTES = (
    "ledger:run:today",
    "ledger:charges:detail",
    # PLAN-011 D5: the CSV exports of the run and of rent charges.
    "ledger:run:export",
    "ledger:charges:export",
)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def health() -> Iterator[tuple[CircuitBreaker, FailureBudget]]:
    breaker = CircuitBreaker(MemoryStateStore(), on_open=lambda opened, reopen: None)
    budget = FailureBudget(MemoryStateStore())
    reset_breaker(breaker)
    reset_budget(budget)
    yield breaker, budget
    reset_breaker()
    reset_budget()


@pytest.fixture
def billing() -> Client:
    return client_for(Role.BILLING, "billing-run")[0]


def charge_paths(charge: RentCharge) -> tuple[str, str]:
    return f"/charges/{charge.pk}/", f"/charges/{charge.pk}/payments/new/"


def a_charge(**overrides: Any) -> RentCharge:
    row = lease(payment_channel=PaymentChannel.WIRE, **overrides)
    rent_run.create_charges(TODAY, actor=None)
    return RentCharge.objects.get(lease=row)


def payment_data(**overrides: str) -> dict[str, str]:
    data = {
        "creation_key": str(uuid.uuid4()),
        "method": "wire",
        "amount": "500.00",
        "paid_on": "2026-10-15",
        "note": "Wire received on the 15th.",
    }
    data.update(overrides)
    return data


# --- Row 10: roles, CSRF, registry --------------------------------------------------------------


def test_every_new_route_is_classified_with_the_planned_roles() -> None:
    names = {
        name
        for name, _, _ in iter_routes()
        if name and name.startswith(("ledger:run:", "ledger:charges:"))
    }
    assert names == set(POST_ROUTES) | set(READ_ROUTES)
    assert set(READ_ROUTES) <= registry.READ_ONLY_ROUTE_NAMES
    for name in POST_ROUTES:
        rule = registry.rule_for(name)
        assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}


def test_a_viewer_reads_and_changes_nothing(fake: FakeProvider) -> None:
    viewer, _ = client_for(Role.VIEWER)
    charge = a_charge()
    detail, record = charge_paths(charge)
    assert viewer.get(RUN).status_code == 200
    assert viewer.get(detail).status_code == 200
    assert viewer.get(record).status_code == 403
    for path in (CREATE, MATCH, record):
        assert viewer.post(path, payment_data()).status_code == 403, path
    assert not Payment.objects.exists() and fake.calls == []
    page = viewer.get(RUN).content.decode()
    assert (
        'id="create-charges"' not in page
        and 'id="record-payment"' not in viewer.get(detail).content.decode()
    )


def test_a_user_without_a_role_is_refused_everywhere() -> None:
    nobody, _ = client_for(None)
    charge = a_charge()
    for path in (RUN, *charge_paths(charge)):
        assert nobody.get(path).status_code == 403, path


@pytest.mark.parametrize("role", [Role.BILLING, Role.ADMIN])
def test_billing_and_admin_may_run(fake: FakeProvider, role: Role) -> None:
    client, _ = client_for(role)
    lease()
    response = client.post(CREATE)
    assert (response.status_code, response["Location"]) == (302, RUN)
    assert RentCharge.objects.count() == 1


def test_every_post_route_enforces_csrf(fake: FakeProvider) -> None:
    user, device = make_user("csrf-billing-run", Role.BILLING)
    assert device is not None
    client = verified_client(user, device, Client(enforce_csrf_checks=True))
    charge = a_charge()
    for path in (CREATE, MATCH, charge_paths(charge)[1]):
        assert client.post(path, payment_data()).status_code == 403, path
    assert RentCharge.objects.count() == 1 and not Payment.objects.exists()
    assert fake.calls == []


def test_get_is_refused_on_the_run_actions(billing: Client) -> None:
    assert billing.get(CREATE).status_code == 405
    assert billing.get(MATCH).status_code == 405


def test_a_malformed_or_unknown_charge_id_is_404(billing: Client) -> None:
    for path in (f"/charges/{uuid.uuid4()}/", "/charges/nope/", "/charges/1/payments/new/"):
        assert billing.get(path).status_code == 404, path


# --- Row 5 through the screen -------------------------------------------------------------------


def test_the_run_page_lists_due_and_skipped_leases_and_a_double_run_creates_nothing(
    billing: Client,
) -> None:
    due = lease()
    lease(collection_status="paused")
    page = billing.get(RUN).content.decode()
    assert "Daily charges, Oct 15, 2026" in page
    assert "Not created" in page and rent_run.SKIP_PAUSED in page
    first = billing.post(CREATE)
    assert (first.status_code, first["Location"]) == (302, RUN)
    after = billing.get(RUN).content.decode()
    assert (
        "Charges created: 1. Created (catch-up): 0. Already created: 0. Skipped leases: 1." in after
    )
    billing.post(CREATE)
    again = billing.get(RUN).content.decode()
    assert "Charges created: 0. Created (catch-up): 0. Already created: 1." in again
    assert RentCharge.objects.filter(lease=due).count() == 1


def test_the_run_page_shows_last_months_status_and_balance(billing: Client) -> None:
    row = lease(started_on=date(2026, 9, 1))
    rent_run.create_charges(date(2026, 9, 15), actor=None)
    RentCharge.objects.filter(lease=row).update(
        status=RentChargeStatus.PARTIAL, amount_paid=D("1000.00")
    )
    page = billing.get(RUN).content.decode()
    assert "Partially paid</a>, balance $500.00" in page


# --- Rows 6 and 9 through the screen ------------------------------------------------------------


def test_match_links_and_reports_through_the_page(billing: Client, fake: FakeProvider) -> None:
    _, [ref] = enrolled(fake)
    rent_run.create_charges(TODAY, actor=None)
    invoice(fake, ref, total=D("1499.00"))
    fake.calls.clear()
    response = billing.post(MATCH)
    assert (response.status_code, response["Location"]) == (302, RUN)
    # PLAN-010 Batch A follow-up: matching is a job; the page shows it queued, then the
    # report once the worker ran it.
    page = billing.get(RUN).content.decode()
    assert 'id="job-state">Waiting<' in page and "Attached, but the amounts differ" not in page
    assert fake.calls == []
    run_queued_jobs()
    page = billing.get(RUN).content.decode()
    assert "Attached, but the amounts differ: 1." in page
    assert "total mismatch" not in page
    assert 'id="match-report"' in page and "differs from the amount due" in page
    assert "Needs review" in page
    charge = RentCharge.objects.get()
    detail = billing.get(charge_paths(charge)[0]).content.decode()
    assert str(rent_run.TOTAL_MISMATCH) in detail
    assert "INV-2026-0001" in detail and "FAKE-SENT" in detail
    assert ref not in page and ref not in detail


def test_the_breaker_banner_shows_and_matching_makes_no_call(
    billing: Client, fake: FakeProvider, health: tuple[CircuitBreaker, FailureBudget]
) -> None:
    breaker, _ = health
    enrolled(fake)
    for _ in range(3):
        breaker.record(Outcome.CLIENT_ERROR)
    page = billing.get(RUN).content.decode()
    assert 'id="provider-banner"' in page and str(BREAKER_OPEN) in page
    assert str(BREAKER_OPEN) == (
        "Calls to Stax are paused after repeated errors. "
        "Update from Stax is unavailable for a few minutes."
    )
    fake.calls.clear()
    billing.post(MATCH)
    assert fake.calls == []
    assert str(BREAKER_OPEN) in billing.get(RUN).content.decode()


def test_the_budget_banner_shows_when_a_debit_would_be_refused(
    billing: Client, health: tuple[CircuitBreaker, FailureBudget]
) -> None:
    _, budget = health
    assert 'id="provider-banner"' not in billing.get(RUN).content.decode()
    for _ in range(5):
        budget.record_http_failure()
    page = billing.get(RUN).content.decode()
    assert str(BUDGET_EXHAUSTED) in page
    assert str(BUDGET_EXHAUSTED) == (
        "Too many recent errors from Stax: a debit would be refused now. "
        "Update from Stax is unavailable until the error count drops."
    )


@pytest.mark.parametrize(("rent", "large"), [("2000.00", False), ("2000.01", True)])
def test_the_large_debit_warning_starts_above_the_threshold(
    billing: Client, rent: str, large: bool
) -> None:
    lease(rent=D(rent))
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00")):
        page = billing.get(RUN).content.decode()
        # PLAN-012e-2 D10: the note moved to the help page the run page links.
        help_page = billing.get("/help/autopay-and-daily-charges/").content.decode()
    assert ('class="large-debit"' in page) is large
    assert 'href="/help/autopay-and-daily-charges/"' in page
    assert "placeholder until Stax states its per-transaction limit" not in page
    assert '"Large debit" marks an amount above $2,000.00.' in help_page.replace("&quot;", '"')
    assert "placeholder until Stax states its per-transaction limit" in help_page


@pytest.mark.parametrize(
    ("rent", "large", "over"),
    [("2000.00", False, False), ("3000.00", True, False), ("3000.01", True, True)],
)
def test_the_run_page_tags_large_debits_and_those_above_the_confirmed_limit(
    billing: Client, rent: str, large: bool, over: bool
) -> None:
    lease(rent=D(rent))
    with override_settings(
        LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=D("3000.00")
    ):
        page = billing.get(RUN).content.decode()
    assert ('<strong class="large-debit">Large debit</strong>' in page) is large
    assert ('<strong class="over-limit">exceeds confirmed limit</strong>' in page) is over


def test_the_run_page_never_tags_over_limit_while_the_limit_is_unknown(billing: Client) -> None:
    lease(rent=D("999999.99"))
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=None):
        page = billing.get(RUN).content.decode()
    assert 'class="large-debit"' in page
    assert "exceeds confirmed limit" not in page


# --- Row 7 through the screens ------------------------------------------------------------------


def test_billing_records_a_partial_then_a_full_payment(billing: Client, fake: FakeProvider) -> None:
    charge = a_charge()
    detail, record = charge_paths(charge)
    form = billing.get(record).content.decode()
    assert 'name="csrfmiddlewaretoken"' in form and 'name="creation_key"' in form
    response = billing.post(record, payment_data())
    assert (response.status_code, response["Location"]) == (302, detail)
    page = billing.get(detail).content.decode()
    assert "Payment recorded." in page and "Partially paid" in page
    billing.post(record, payment_data(amount="1000.00", method="zelle"))
    charge.refresh_from_db()
    assert (charge.amount_paid, charge.status) == (D("1500.00"), RentChargeStatus.PAID)
    assert 'id="record-payment"' not in billing.get(detail).content.decode()
    assert AuditLog.objects.filter(action="rent_charge.payment_recorded").count() == 2
    assert fake.calls == []


def test_a_replayed_payment_form_records_once(billing: Client) -> None:
    charge = a_charge()
    data = payment_data()
    billing.post(charge_paths(charge)[1], data)
    billing.post(charge_paths(charge)[1], data)
    assert Payment.objects.count() == 1


@pytest.mark.parametrize(
    ("overrides", "text"),
    [
        ({"note": ""}, "A note is required"),
        ({"amount": "1500.01"}, rent_run.PAY_OVER_BALANCE),
        ({"amount": "0"}, "greater than or equal to 0.01"),
        ({"amount": "-1"}, "greater than or equal to 0.01"),
        ({"paid_on": "2026-10-16"}, rent_run.PAY_FUTURE),
        ({"method": "ach"}, "Select a valid choice"),
        ({"note": "card 4111 1111 1111 1111"}, "may not contain card or account numbers"),
    ],
)
def test_an_invalid_payment_is_held_shown_once_and_saves_nothing(
    billing: Client, overrides: dict[str, str], text: str
) -> None:
    charge = a_charge()
    record = charge_paths(charge)[1]
    response = billing.post(record, payment_data(**overrides))
    assert (response.status_code, response["Location"]) == (302, record)
    page = billing.get(record).content.decode()
    assert text in page
    # The rejected value never comes back, in any spacing. ("4111" alone could appear
    # inside a CSRF token or a UUID, so the check is on the number itself.)
    assert "4111 1111 1111 1111" not in page and "4111111111111111" not in page
    assert text not in billing.get(record).content.decode()
    assert not Payment.objects.exists()


# --- PLAN-008a row 4: linking an invoice from the charge page ------------------------------

_invoice_numbers = iter(range(9001, 10**6))


def local_invoice(
    operator: Operator | None,
    total: str = "1500.00",
    *,
    provider_id: str = "",
    charge: RentCharge | None = None,
    status: str = "",
) -> Invoice:
    """A local invoice as the webhook handler makes it, with its provider reference."""
    assert operator is not None
    row = Invoice.objects.create(
        invoice_number=f"INV-2026-{next(_invoice_numbers)}",
        operator=operator,
        rent_charge=charge,
        issue_date=date(2026, 10, 14),
        due_date=date(2026, 10, 14),
        subtotal=D(total),
        total=D(total),
    )
    if status:
        invoice_sync.record_provider_status(row.pk, status)
    ExternalRef.objects.create(
        entity_type="invoice",
        entity_id=row.pk,
        provider="stax_pay",
        external_kind="invoice",
        external_id=provider_id or f"dash-invoice-{row.invoice_number[-4:]}",
    )
    return row


def link_path(charge: RentCharge) -> str:
    return f"/charges/{charge.pk}/link-invoice/"


def link_data(invoice: Invoice, **overrides: str) -> dict[str, str]:
    return {"invoice": str(invoice.pk), **overrides}


def test_the_link_list_shows_only_the_operators_unlinked_invoices(billing: Client) -> None:
    charge = a_charge()
    operator = charge.lease.operator
    other_charge = RentCharge.objects.create(
        lease=lease(
            operator=operator,
            store=Store.objects.create(external_label="Store 0202", status=StoreStatus.ACTIVE),
        ),
        period_month=date(2026, 10, 1),
        seq=1,
        due_on=TODAY,
        amount_due=D("1500.00"),
        status=RentChargeStatus.DUE,
    )
    mine = local_invoice(operator, provider_id="dashboard-invoice-0001", status="DRAFT")
    linked = local_invoice(operator, charge=other_charge)
    foreign = local_invoice(lease().operator)
    page = billing.get(link_path(charge)).content.decode()
    assert mine.invoice_number in page
    assert linked.invoice_number not in page and foreign.invoice_number not in page
    assert "dashboard-invoice-0001" not in page and "dash…0001" in page
    assert "Oct 14, 2026" in page and "$1,500.00" in page and "DRAFT" in page
    assert f'value="{mine.pk}"' in page
    assert 'id="balance">$1,500.00<' in page


def test_the_charge_page_offers_the_link_to_billing_only_while_it_has_no_invoice(
    billing: Client,
) -> None:
    charge = a_charge()
    detail = charge_paths(charge)[0]
    assert 'id="link-invoice"' in billing.get(detail).content.decode()
    viewer, _ = client_for(Role.VIEWER)
    assert 'id="link-invoice"' not in viewer.get(detail).content.decode()
    local_invoice(charge.lease.operator, charge=charge)
    assert 'id="link-invoice"' not in billing.get(detail).content.decode()
    page = billing.get(link_path(charge)).content.decode()
    assert 'id="link-refused"' in page and 'id="link-form"' not in page


def test_an_equal_total_links_without_confirmation_and_is_audited(billing: Client) -> None:
    charge = a_charge()
    invoice = local_invoice(charge.lease.operator, provider_id="dashboard-invoice-0001")
    response = billing.post(link_path(charge), link_data(invoice))
    assert (response.status_code, response["Location"]) == (302, charge_paths(charge)[0])
    invoice.refresh_from_db()
    charge.refresh_from_db()
    assert invoice.rent_charge_id == charge.pk
    assert not charge.needs_review
    page = billing.get(charge_paths(charge)[0]).content.decode()
    assert f"Invoice {invoice.invoice_number} linked to this charge." in page
    assert invoice.invoice_number in page and 'id="link-invoice"' not in page
    audit = AuditLog.objects.get(action="invoice.linked")
    assert audit.actor is not None and audit.actor.username == "billing-run"
    assert audit.entity_id == str(invoice.pk)
    assert audit.after == {
        "invoice_number": invoice.invoice_number,
        "invoice_total": "1500.00",
        "charge_balance": "1500.00",
        "rent_charge": str(charge.pk),
        "amount_due": "1500.00",
        "provider_invoice": "dash…0001",
        "total_differs": False,
        "note": "",
    }
    assert not AuditLog.objects.filter(action=rent_run.LINKED_TOTAL_DIFFERS_ACTION).exists()


@pytest.mark.parametrize(
    ("overrides", "text"),
    [
        ({}, rent_run.LINK_CONFIRM),
        ({"note": "Dashboard invoice for October."}, rent_run.LINK_CONFIRM),
        ({"confirm": "on"}, rent_run.LINK_NOTE_REQUIRED),
        ({"confirm": "on", "note": "   "}, rent_run.LINK_NOTE_REQUIRED),
    ],
)
def test_a_different_total_needs_the_confirmation_and_a_note(
    billing: Client, overrides: dict[str, str], text: str
) -> None:
    charge = a_charge()
    invoice = local_invoice(charge.lease.operator, "1450.00")
    response = billing.post(link_path(charge), link_data(invoice, **overrides))
    assert (response.status_code, response["Location"]) == (302, link_path(charge))
    assert text in billing.get(link_path(charge)).content.decode()
    invoice.refresh_from_db()
    assert invoice.rent_charge_id is None
    assert not AuditLog.objects.filter(action="invoice.linked").exists()


def test_a_different_total_with_confirmation_and_note_links_and_flags_the_charge(
    billing: Client,
) -> None:
    charge = a_charge()
    invoice = local_invoice(charge.lease.operator, "1450.00")
    note = "Operator agreed a $50 credit for October."
    data = link_data(invoice, confirm="on", note=note)
    response = billing.post(link_path(charge), data)
    assert response.status_code == 302
    invoice.refresh_from_db()
    charge.refresh_from_db()
    assert invoice.rent_charge_id == charge.pk
    assert charge.needs_review and charge.amount_due == D("1500.00")
    page = billing.get(charge_paths(charge)[0]).content.decode()
    assert rent_run.LINKED_TOTAL_DIFFERS in page and "Needs review" in page
    audit = AuditLog.objects.get(action="invoice.linked")
    assert audit.after is not None
    assert audit.after["note"] == note and audit.after["total_differs"] is True
    assert (audit.after["invoice_total"], audit.after["charge_balance"]) == ("1450.00", "1500.00")
    flag = AuditLog.objects.get(action=rent_run.LINKED_TOTAL_DIFFERS_ACTION)
    assert flag.entity_id == str(charge.pk) and flag.actor == audit.actor


def test_the_total_is_compared_with_the_balance_not_the_amount_due(billing: Client) -> None:
    charge = a_charge()
    billing.post(charge_paths(charge)[1], payment_data(amount="500.00"))
    charge.refresh_from_db()
    assert charge.balance == D("1000.00")
    invoice = local_invoice(charge.lease.operator, "1500.00")  # equals the amount due
    billing.post(link_path(charge), link_data(invoice))
    assert rent_run.LINK_CONFIRM in billing.get(link_path(charge)).content.decode()
    invoice.refresh_from_db()
    assert invoice.rent_charge_id is None
    exact = local_invoice(charge.lease.operator, "1000.00")
    billing.post(link_path(charge), link_data(exact))
    exact.refresh_from_db()
    assert exact.rent_charge_id == charge.pk


def test_a_second_invoice_is_refused(billing: Client) -> None:
    charge = a_charge()
    first = local_invoice(charge.lease.operator)
    second = local_invoice(charge.lease.operator)
    billing.post(link_path(charge), link_data(first))
    response = billing.post(link_path(charge), link_data(second))
    assert response.status_code == 302
    second.refresh_from_db()
    assert second.rent_charge_id is None
    assert list(charge.invoices.all()) == [first]
    assert AuditLog.objects.filter(action="invoice.linked").count() == 1
    outcome = rent_run.link_invoice(charge.pk, second.pk, confirm=True, note="x", actor=None)
    assert (outcome.ok, outcome.message) == (False, rent_run.LINK_CHARGE_HAS_INVOICE)


def test_the_same_link_posted_twice_changes_nothing(billing: Client) -> None:
    charge = a_charge()
    invoice = local_invoice(charge.lease.operator)
    billing.post(link_path(charge), link_data(invoice))
    billing.post(link_path(charge), link_data(invoice))
    assert AuditLog.objects.filter(action="invoice.linked").count() == 1
    assert rent_run.LINK_SAME in billing.get(charge_paths(charge)[0]).content.decode()


def test_an_invoice_already_linked_elsewhere_or_unknown_is_refused(billing: Client) -> None:
    charge = a_charge()
    other = a_charge_of(charge.lease.operator)
    elsewhere = local_invoice(charge.lease.operator, charge=other)
    for invoice_id in (str(elsewhere.pk), str(uuid.uuid4())):
        billing.post(link_path(charge), {"invoice": invoice_id})
        page = billing.get(link_path(charge)).content.decode().replace("&#x27;", "'")
        assert rent_run.LINK_CHOOSE in page
    billing.post(link_path(charge), {"invoice": "not-a-uuid"})
    assert "Enter a valid UUID." in billing.get(link_path(charge)).content.decode()
    elsewhere.refresh_from_db()
    assert elsewhere.rent_charge_id == other.pk
    assert not charge.invoices.exists()


def test_a_viewer_and_a_user_without_a_role_cannot_link() -> None:
    charge = a_charge()
    invoice = local_invoice(charge.lease.operator)
    viewer, _ = client_for(Role.VIEWER)
    nobody, _ = client_for(None)
    for client in (viewer, nobody):
        assert client.get(link_path(charge)).status_code == 403
        assert client.post(link_path(charge), link_data(invoice)).status_code == 403
    invoice.refresh_from_db()
    assert invoice.rent_charge_id is None
    assert not AuditLog.objects.filter(action="invoice.linked").exists()


@pytest.mark.parametrize("role", [Role.ADMIN, Role.BILLING])
def test_admin_and_billing_may_link(role: Role) -> None:
    client, _ = client_for(role)
    charge = a_charge()
    invoice = local_invoice(charge.lease.operator)
    assert client.get(link_path(charge)).status_code == 200
    assert client.post(link_path(charge), link_data(invoice)).status_code == 302
    invoice.refresh_from_db()
    assert invoice.rent_charge_id == charge.pk


def test_the_link_route_enforces_csrf_and_404s_bad_charge_ids() -> None:
    user, device = make_user("csrf-link", Role.BILLING)
    assert device is not None
    client = verified_client(user, device, Client(enforce_csrf_checks=True))
    charge = a_charge()
    invoice = local_invoice(charge.lease.operator)
    assert client.post(link_path(charge), link_data(invoice)).status_code == 403
    billing = client_for(Role.BILLING)[0]
    for pk in ("not-a-uuid", str(uuid.uuid4())):
        assert billing.get(f"/charges/{pk}/link-invoice/").status_code == 404
    invoice.refresh_from_db()
    assert invoice.rent_charge_id is None


def test_the_run_page_says_matching_reads_schedules_only(billing: Client) -> None:
    # PLAN-012e-2 D10: the paragraph moved to the help page the run page links.
    page = billing.get(RUN).content.decode()
    assert 'id="match-scope"' not in page
    assert 'href="/help/autopay-and-daily-charges/"' in page
    help_page = billing.get("/help/autopay-and-daily-charges/").content.decode()
    assert 'id="help-match-scope"' in help_page
    assert "reads Stax's autopay invoices only" in help_page.replace("&#x27;", "'")
    assert '"Attach Stax invoice"' in help_page.replace("&quot;", '"')


def a_charge_of(operator: Operator | None) -> RentCharge:
    row = lease(
        operator=operator,
        store=Store.objects.create(external_label="Store 0303", status=StoreStatus.ACTIVE),
    )
    return RentCharge.objects.create(
        lease=row,
        period_month=date(2026, 10, 1),
        seq=1,
        due_on=TODAY,
        amount_due=D("1500.00"),
        status=RentChargeStatus.DUE,
    )
