"""Fable's adversarial cases for PLAN-012f-b slice C2, the rent-charge bank debit (plan §10
D19). Opus's ``tests/test_rent_debit.py`` holds the behaviour; this file holds the abuse,
race and boundary cases the plan left to Fable: two debits at the same moment, the ended
lease on each path, forged and missing keys on the confirmation, hostile parameters, what a
provider refusal leaves, what nothing is ever written (payment rows, the rent charge), and
the lease whose own autopay would debit the charge tonight."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection, connections
from django.test import override_settings
from django.urls import reverse

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Lease,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import leases, receivables, rent_run
from ledger.services import one_off_charges as service
from payments.errors import ProviderRejected
from payments.fake import FakeProvider
from payments.models import Job, JobKind
from tests.job_helpers import run_queued_jobs
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_debit import (
    a_debit,
    a_payment,
    a_rent,
    admin,
    an_autopay_rent,
    billing,
    confirm_page,
    debit_post,
    detail,
    fresh_rent,
    page,
    rent_debit,
    today,
    written,
)

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


def another(fake: FakeProvider, bank: str | None = "pm-bank-2") -> Operator:
    """A second operator with its own provider customer and method (the fake's refs are unique)."""
    return an_operator(fake, customer=f"cust-{uuid.uuid4().hex[:8]}", bank=bank)


requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="row locks and the partial index: PostgreSQL only"
)


def debit_rows(rent: RentCharge) -> list[OneOffCharge]:
    return list(OneOffCharge.objects.filter(source_rent_charge=rent).order_by("created_at"))


def started_rows(rent: RentCharge) -> int:
    return AuditLog.objects.filter(action=service.RENT_DEBIT_ACTION, entity_id=str(rent.pk)).count()


def rent_state(rent: RentCharge) -> tuple[str, Decimal, Decimal]:
    fresh = fresh_rent(rent)
    return (fresh.status, Decimal(fresh.amount_paid), Decimal(fresh.balance))


# --- Two debits at the same moment ---------------------------------------------------------


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_debits_at_the_same_moment_leave_one_debit_one_job_one_audit_row(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake, "1500.00")
    actor = billing()
    on = today() + timedelta(days=3)
    barrier = threading.Barrier(2)
    outcomes: list[service.Outcome] = []
    errors: list[BaseException] = []

    def debit() -> None:
        try:
            barrier.wait(timeout=10)
            outcomes.append(
                service.rent_debit(
                    fresh_rent(rent), on, actor=actor, confirmed=False, key=uuid.uuid4()
                )
            )
        except BaseException as error:
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=debit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert sorted(outcome.ok for outcome in outcomes) == [False, True]
    [loser] = [outcome for outcome in outcomes if not outcome.ok]
    assert loser.message == service.RENT_DEBIT_PENDING
    assert len(debit_rows(rent)) == 1
    assert Job.objects.filter(kind=JobKind.SCHEDULE_CREATE).count() == 1
    assert started_rows(rent) == 1


# --- The ended lease, each path -------------------------------------------------------------


def end_lease(lease: Lease) -> None:
    lease.ended_on = today() - timedelta(days=1)
    lease.save(update_fields=["ended_on", "updated_at"])


def test_an_ended_lease_still_takes_todays_debit_but_not_a_scheduled_one(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake, "1500.00")
    end_lease(rent.lease)
    before = written()
    later = rent_debit(rent, today() + timedelta(days=3), actor=billing())
    assert (later.ok, later.message, later.field) == (False, service.LEASE_ENDED, "on")
    assert written() == before and debit_rows(rent) == []
    now = rent_debit(rent, today(), confirmed=False)
    assert now.ok and now.message == service.CHARGE_NOW_REQUESTED
    [debit] = debit_rows(rent)
    assert debit.lease_id == rent.lease_id and debit.due_date == today()


# --- The confirmation page's keys and parameters -------------------------------------------


def test_the_confirmation_get_and_head_write_nothing_and_leave_the_session_alone(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake, "1500.00")
    client, _ = client_for(Role.ADMIN)

    def session() -> dict[str, object]:
        # The sign-in middleware's last-seen stamp moves on every request; nothing else may.
        return {k: v for k, v in client.session.items() if k != "accounts_last_seen_at"}

    session_before = session()
    before = written()
    for method in (client.get, client.head):
        response = method(f"{confirm_page(rent)}?key={uuid.uuid4()}")
        assert response.status_code == 200
    assert written() == before
    assert session() == session_before
    assert Payment.objects.count() == 0


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_the_confirmation_refuses_below_admin_on_get_and_post(
    fake: FakeProvider, role: Role
) -> None:
    rent = a_rent(fake, "1500.00")
    client, _ = client_for(role)
    before = written()
    assert client.get(confirm_page(rent)).status_code == 403
    assert client.post(confirm_page(rent), {"key": str(uuid.uuid4())}).status_code == 403
    assert written() == before


def test_a_key_already_used_by_another_charge_debits_nothing(fake: FakeProvider) -> None:
    first = a_rent(fake, "1500.00")
    second = a_rent(fake, "900.00", operator=another(fake))
    key = uuid.uuid4()
    assert rent_debit(first, today() + timedelta(days=3), actor=billing(), key=key).ok
    before = written()
    client, _ = client_for(Role.ADMIN)
    response = client.post(confirm_page(second), {"key": str(key)})
    assert response.status_code == 302 and response["Location"] == detail(second)
    # The replay of the key answers "already", and the second rent charge got no debit.
    assert written() == before and debit_rows(second) == []
    assert len(debit_rows(first)) == 1


def test_two_confirm_posts_without_a_key_leave_one_debit(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    client, _ = client_for(Role.ADMIN)
    first = client.post(confirm_page(rent), {})
    assert first.status_code == 302 and first["Location"] == detail(rent)
    count = written()
    second = client.post(confirm_page(rent), {})
    assert second.status_code == 302
    assert len(debit_rows(rent)) == 1
    # The second post wrote no charge, invoice or job (a held form at most).
    assert written()[:3] == count[:3]


def test_a_key_that_is_not_a_uuid_renders_the_page_with_a_fresh_key(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    client, _ = client_for(Role.ADMIN)
    for hostile in ("not-a-key", "'; DROP TABLE one_off_charges; --", "%00", "x" * 2000):
        response = client.get(confirm_page(rent), {"key": hostile})
        assert response.status_code == 200
        body = response.content.decode()
        assert hostile not in body and "Traceback" not in body


def test_another_stores_context_on_the_confirmation_is_not_found(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    other = a_rent(fake, "700.00", operator=another(fake, bank=None))
    client, _ = client_for(Role.ADMIN)
    url = f"{confirm_page(rent)}?store={other.lease.store_id}"
    assert client.get(url).status_code == 404
    assert client.post(url, {"key": str(uuid.uuid4())}).status_code == 404
    assert debit_rows(rent) == []


def test_the_operators_name_is_escaped_on_the_confirmation(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    operator = rent.lease.operator
    assert operator is not None
    operator.display_name = '<b onmouseover="x()">Northwind</b>'
    operator.save(update_fields=["display_name", "updated_at"])
    client, _ = client_for(Role.ADMIN)
    body = client.get(confirm_page(rent)).content.decode()
    assert '<b onmouseover="x()">' not in body
    assert "&lt;b onmouseover=&quot;x()&quot;&gt;Northwind&lt;/b&gt;" in body


# --- Dates on the scheduled path -------------------------------------------------------------


def test_tomorrow_is_the_first_date_billing_may_schedule_and_the_horizon_holds(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake, "1500.00")
    client, _ = client_for(Role.BILLING)
    before = written()
    far = client.post(page(rent), debit_post(rent, today() + timedelta(days=366)))
    assert far.status_code == 302 and written() == before
    held = client.get(far["Location"]).content.decode()
    assert 'id="how-debit"' in held and debit_rows(rent) == []
    soon = client.post(page(rent), debit_post(rent, today() + timedelta(days=1)))
    assert soon.status_code == 302 and soon["Location"] == detail(rent)
    [debit] = debit_rows(rent)
    assert debit.due_date == today() + timedelta(days=1)


def test_billing_cannot_debit_today_from_add_payment(fake: FakeProvider) -> None:
    rent = a_rent(fake, "1500.00")
    client, _ = client_for(Role.BILLING)
    before = written()
    response = client.post(page(rent), debit_post(rent, today()))
    # The form refuses today for billing (Debit on starts tomorrow) and re-opens the page.
    assert response.status_code == 302 and response["Location"] != confirm_page(rent)
    assert client.get(response["Location"]).status_code == 200
    assert written() == before and debit_rows(rent) == []
    # The confirmation itself is an admin's, whatever the key.
    assert client.get(f"{confirm_page(rent)}?key={uuid.uuid4()}").status_code == 403
    assert written() == before


# --- Rule 2: no payment state is written here ----------------------------------------------


@pytest.mark.parametrize("when", ["today", "later"])
def test_no_payment_row_and_no_rent_change_until_the_bank_settles(
    fake: FakeProvider, when: str
) -> None:
    rent = a_rent(fake, "1500.00")
    state = rent_state(rent)
    on = today() if when == "today" else today() + timedelta(days=3)
    assert rent_debit(rent, on, actor=admin()).ok
    # The action itself writes no payment row (Rule 2).
    assert Payment.objects.count() == 0
    run_queued_jobs()
    # The worker may record the provider's attempt, pending at most, on the debit charge,
    # never on the rent charge; nothing is paid until the bank settles.
    assert not Payment.objects.filter(rent_charge=rent).exists()
    assert not Payment.objects.exclude(status="pending").exists()
    assert rent_state(rent) == state
    assert rent_run.own_paid(fresh_rent(rent)) == D("0.00")


def test_a_provider_refusal_leaves_the_debit_owed_the_rent_untouched_and_an_audit_trail(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake, "1500.00")
    state = rent_state(rent)
    assert rent_debit(rent, today()).ok
    fake.fail_next("pay_invoice", ProviderRejected(422, error_fields=("payment_method_id",)))
    run_queued_jobs()
    [debit] = debit_rows(rent)
    assert debit.status == OneOffChargeStatus.OWED and Payment.objects.count() == 0
    assert rent_state(rent) == state
    failed = AuditLog.objects.filter(action=service.CHARGE_FAILED_ACTION, entity_id=str(debit.pk))
    assert failed.count() == 1
    assert started_rows(rent) == 1
    assert service.rent_debit_options(fresh_rent(rent)).reason == service.RENT_DEBIT_PENDING


# --- Carried once, whatever the mix of sources ---------------------------------------------


def test_a_returned_payments_redebit_and_a_bank_debit_carry_under_the_rent_charge_once_each(
    fake: FakeProvider,
) -> None:
    rent = a_rent(fake, "1500.00", status=RentChargeStatus.UNPAID)
    returned = a_payment(rent, "returned", "1500.00")
    OneOffCharge.objects.create(
        operator=rent.lease.operator,
        lease=rent.lease,
        category="redebit",
        collection="autopay_due",
        description="Re-debit of Rent Sep 2026",
        price=D("1000.00"),
        due_date=today() + timedelta(days=2),
        status=OneOffChargeStatus.OWED,
        source_payment=returned,
    )
    a_debit(rent, price=D("500.00"))
    carried, _ = receivables.carried_by_redebits()
    assert carried[(receivables.KIND_RENT, rent.pk)] == D("1500.00")
    assert service.rent_debit_options(fresh_rent(rent)).reason == service.RENT_DEBIT_PENDING


# --- The lease's own autopay (Fable's C2-1) ---------------------------------------------------


def test_a_due_charge_the_leases_autopay_will_debit_tonight_is_not_offered(
    fake: FakeProvider,
) -> None:
    rent = an_autopay_rent(fake, today())
    options = service.rent_debit_options(rent)
    assert options.reason.startswith("The lease's autopay debits this charge on ")
    assert str(today().year) in options.reason and options.reason.endswith(".")
    before = written()
    refused = rent_debit(rent, today() + timedelta(days=3), actor=billing())
    assert (refused.ok, refused.message) == (False, options.reason)
    assert written() == before


def test_a_due_charge_the_autopay_did_not_take_is_offered(fake: FakeProvider) -> None:
    rent = an_autopay_rent(fake, today() - timedelta(days=1))
    assert service.rent_debit_options(rent).reason == ""


def test_a_paused_autopay_offers_the_debit(fake: FakeProvider) -> None:
    rent = an_autopay_rent(fake, today(), paused=True)
    assert not leases.autopay_on(rent.lease)
    assert service.rent_debit_options(fresh_rent(rent)).reason == ""


def test_the_add_payment_page_greys_the_card_with_the_autopay_sentence(
    fake: FakeProvider,
) -> None:
    rent = an_autopay_rent(fake, today())
    client, _ = client_for(Role.ADMIN)
    body = client.get(page(rent)).content.decode()
    assert "autopay debits this charge on" in body and 'id="how-debit" disabled' in body
    before = written()
    response = client.post(page(rent), debit_post(rent, today() + timedelta(days=3)))
    assert response.status_code == 302 and written() == before


# --- The route and its rule -------------------------------------------------------------------


def test_the_confirmation_route_is_registered_as_an_admins_post(fake: FakeProvider) -> None:
    from accounts.registry import rule_for

    rule = rule_for("ledger:charges:debit_confirm")
    assert rule is not None and rule.min_role == Role.ADMIN
    assert "POST" in rule.methods


def test_an_unknown_charge_is_not_found_on_both_routes(fake: FakeProvider) -> None:
    client, _ = client_for(Role.ADMIN)
    missing = uuid.uuid4()
    for name in ("ledger:charges:debit_confirm", "ledger:charges:record_payment"):
        assert client.get(reverse(name, kwargs={"pk": missing})).status_code == 404
