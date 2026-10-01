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
    Payment,
    PaymentChannel,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import rent_run
from payments.fake import FakeProvider
from payments.health import BREAKER_OPEN, BUDGET_EXHAUSTED
from payments.stax.breaker import CircuitBreaker, Outcome, reset_breaker
from payments.stax.budget import FailureBudget, reset_budget
from payments.stax.state import MemoryStateStore
from tests.auth_helpers import iter_routes, make_user, verified_client
from tests.test_operator_views import client_for
from tests.test_rent_run import enrolled, invoice, lease

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)
RUN = "/run/"
CREATE = "/run/create-charges/"
MATCH = "/run/match/"
POST_ROUTES = ("ledger:run:create_charges", "ledger:run:match", "ledger:charges:record_payment")
READ_ROUTES = ("ledger:run:today", "ledger:charges:detail")


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
    assert "Rent run 2026-10-15" in page
    assert "Not created" in page and rent_run.SKIP_PAUSED in page
    first = billing.post(CREATE)
    assert (first.status_code, first["Location"]) == (302, RUN)
    after = billing.get(RUN).content.decode()
    assert "Charges created: 1. Already created: 0. Skipped leases: 1." in after
    billing.post(CREATE)
    again = billing.get(RUN).content.decode()
    assert "Charges created: 0. Already created: 1." in again
    assert RentCharge.objects.filter(lease=due).count() == 1


def test_the_run_page_shows_last_months_status_and_balance(billing: Client) -> None:
    row = lease()
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
    response = billing.post(MATCH)
    assert (response.status_code, response["Location"]) == (302, RUN)
    page = billing.get(RUN).content.decode()
    assert "Linked with a total mismatch: 1." in page
    assert "Needs review" in page
    charge = RentCharge.objects.get()
    detail = billing.get(charge_paths(charge)[0]).content.decode()
    assert rent_run.TOTAL_MISMATCH in detail
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
    assert 'id="provider-banner"' in page and BREAKER_OPEN in page
    fake.calls.clear()
    billing.post(MATCH)
    assert fake.calls == []
    assert BREAKER_OPEN in billing.get(RUN).content.decode()


def test_the_budget_banner_shows_when_a_debit_would_be_refused(
    billing: Client, health: tuple[CircuitBreaker, FailureBudget]
) -> None:
    _, budget = health
    assert 'id="provider-banner"' not in billing.get(RUN).content.decode()
    for _ in range(5):
        budget.record_http_failure()
    page = billing.get(RUN).content.decode()
    assert BUDGET_EXHAUSTED in page


@pytest.mark.parametrize(("rent", "large"), [("2000.00", False), ("2000.01", True)])
def test_the_large_debit_warning_starts_above_the_threshold(
    billing: Client, rent: str, large: bool
) -> None:
    lease(rent=D(rent))
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00")):
        page = billing.get(RUN).content.decode()
    assert ('class="large-debit"' in page) is large
    assert "placeholder until the payment provider states its per-transaction limit" in page


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
    assert "4111" not in page
    assert text not in billing.get(record).content.decode()
    assert not Payment.objects.exists()
