"""Adversarial cases for PLAN-007, written by the QA owner (plan §6 row 11)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.db import connections
from django.test import Client

from accounts.roles import Role
from ledger.models import CollectionSchedule, Invoice, Payment, RentCharge, ScheduleState
from ledger.services import leases as lease_service
from ledger.services import rent_run
from payments.fake import FakeProvider
from payments.models import ExternalRef
from payments.provider import ScheduleInput, ScheduleUpdate
from tests.auth_helpers import make_user, verified_client
from tests.job_helpers import worker_between_requests  # noqa: F401
from tests.test_lease_services import CUSTOMER, _race, edit, make_lease, make_operator, method
from tests.test_operator_sync import requires_postgresql
from tests.test_rent_run import DAY, NOW, PERIOD, RENT, charge_of, enrolled, invoice, lease

pytestmark = pytest.mark.django_db

D = Decimal
CARD = "4111111111111111"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def _client(role: Role) -> Client:
    user, device = make_user(f"adv7-{role.value}-{uuid.uuid4().hex[:6]}", role)
    assert device is not None
    return verified_client(user, device)


@pytest.fixture(autouse=True)
def _worker_runs_between_requests(request: pytest.FixtureRequest) -> None:
    """PLAN-010: views queue provider work; these cases check the outcome after the worker ran."""
    request.getfixturevalue("worker_between_requests")


def _schedule_creates(fake: FakeProvider) -> list[ScheduleInput]:
    return [args[0] for name, args in fake.calls if name == "create_scheduled_invoice"]  # type: ignore[misc]


def _schedule_updates(fake: FakeProvider) -> list[ScheduleUpdate]:
    return [args[1] for name, args in fake.calls if name == "update_scheduled_invoice"]  # type: ignore[misc]


# --- enrolment ----------------------------------------------------------------------------------


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_enable_clicks_make_exactly_one_schedule_per_debit(
    fake: FakeProvider,
) -> None:
    row = make_lease(debit_day=5, second_debit_day=20)
    customer = lease_service.customer_ref(row.operator)
    fake.add_payment_method(customer, method("pm-race"))

    outcomes = _race(
        lambda: lease_service.enable_autopay(row.pk, "pm-race", actor=None, now=NOW),
        lambda: lease_service.enable_autopay(row.pk, "pm-race", actor=None, now=NOW),
    )

    assert len(_schedule_creates(fake)) == 2  # one per debit day, not per click
    live = [
        s for s in CollectionSchedule.objects.filter(lease=row) if s.state == ScheduleState.ACTIVE
    ]
    assert sorted(s.seq for s in live) == [1, 2]
    assert sum(1 for o in outcomes if o.action == lease_service.AutopayAction.ENABLED) == 1


def test_a_payment_method_of_another_customer_is_refused_without_a_create(
    fake: FakeProvider,
) -> None:
    row = make_lease()
    fake.add_payment_method("cus-someone-else", method("pm-theirs"))
    fake.add_payment_method(CUSTOMER, method("pm-theirs-card", kind="card"))

    for method_id in ("pm-theirs", "pm-theirs-card", "pm-does-not-exist"):
        outcome = lease_service.enable_autopay(row.pk, method_id, actor=None, now=NOW)
        assert not outcome.ok, method_id
    assert _schedule_creates(fake) == []
    assert not CollectionSchedule.objects.filter(lease=row, state=ScheduleState.ACTIVE).exists()


def test_the_schedule_total_cannot_be_tampered_with_through_the_enrol_form(
    fake: FakeProvider,
) -> None:
    row = make_lease(rent=D("1234.56"))
    fake.add_payment_method(CUSTOMER, method("pm-form"))
    admin = _client(Role.ADMIN)
    admin.post(
        f"/leases/{row.pk}/enrol/",
        {
            "payment_method_id": "pm-form",
            "total": "1.00",
            "amount": "1.00",
            "rent": "1.00",
        },
    )
    creates = _schedule_creates(fake)
    assert len(creates) == 1
    assert creates[0].amounts.total == D("1234.56")


def test_a_rent_change_while_autopay_is_active_never_edits_the_schedule(fake: FakeProvider) -> None:
    row, refs = enrolled(fake)
    edit(row, D("1600.00"), starts=date(2026, 10, 2))
    assert _schedule_updates(fake) == []
    assert _schedule_creates(fake) and len(_schedule_creates(fake)) == 1
    flagged = CollectionSchedule.objects.get(lease=row, state=ScheduleState.ACTIVE)
    assert flagged.needs_review is True
    # Pause still works and sends only active=false; resume is refused while flagged.
    lease_service.pause_autopay(row.pk, actor=None)
    updates = _schedule_updates(fake)
    assert [u.active for u in updates] == [False]
    assert all(not hasattr(u, "total") for u in updates)
    resumed = lease_service.resume_autopay(row.pk, actor=None, now=NOW)
    assert (
        resumed.action != lease_service.AutopayAction.RESUMED
        if hasattr(lease_service.AutopayAction, "RESUMED")
        else not resumed.ok
    )
    assert [u.active for u in _schedule_updates(fake)] == [False]


def test_resume_never_carries_an_amount_even_if_the_rent_changed(fake: FakeProvider) -> None:
    row, _ = enrolled(fake)
    lease_service.pause_autopay(row.pk, actor=None)
    outcome = lease_service.resume_autopay(
        row.pk, actor=None, now=datetime(2026, 10, 2, 9, tzinfo=UTC)
    )
    assert outcome.ok, outcome.text
    resumed = _schedule_updates(fake)[-1]
    assert resumed.active is True
    assert resumed.rule is not None and resumed.rule.startswith("DTSTART=2026")
    assert "total" not in str(resumed)


# --- the run ------------------------------------------------------------------------------------


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_runs_on_the_same_day_create_each_charge_once(fake: FakeProvider) -> None:
    for _ in range(5):
        lease(debit_day=DAY.day)
    lease(debit_day=DAY.day, second_debit_day=DAY.day + 1)
    reports = _race(
        lambda: rent_run.create_charges(DAY, actor=None),
        lambda: rent_run.create_charges(DAY, actor=None),
        lambda: rent_run.create_charges(DAY, actor=None),
    )
    assert RentCharge.objects.filter(period_month=PERIOD).count() == 6
    assert sum(len(r.created) for r in reports) == 6


def test_a_run_for_a_day_with_no_due_lease_writes_nothing_and_a_run_for_yesterday_is_separate(
    fake: FakeProvider,
) -> None:
    lease(debit_day=15)
    rent_run.create_charges(date(2026, 10, 14), actor=None)
    assert RentCharge.objects.count() == 0
    rent_run.create_charges(date(2026, 10, 15), actor=None)
    rent_run.create_charges(date(2026, 11, 15), actor=None)
    assert sorted(c.period_month for c in RentCharge.objects.all()) == [
        date(2026, 10, 1),
        date(2026, 11, 1),
    ]


def test_an_invoice_dated_exactly_at_the_month_boundary_is_not_guessed(fake: FakeProvider) -> None:
    row, refs = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    for dated in ("2026-10-31 23:59:59", "2026-11-01 00:00:00", "2026-09-30 23:59:59"):
        invoice(fake, refs[0], dated=dated)
    report = rent_run.match_schedule_invoices(actor=None)
    linked = Invoice.objects.filter(rent_charge=charge_of(row))
    # Only invoices inside October may link; at most the 31 October one.
    assert linked.count() <= 1
    assert report is not None


def test_matching_the_same_invoice_twice_never_reassigns_the_number_or_duplicates(
    fake: FakeProvider,
) -> None:
    row, refs = enrolled(fake)
    rent_run.create_charges(DAY, actor=None)
    invoice(fake, refs[0])
    rent_run.match_schedule_invoices(actor=None)
    first = Invoice.objects.get(rent_charge=charge_of(row))
    rent_run.match_schedule_invoices(actor=None)
    rent_run.match_schedule_invoices(actor=None)
    assert Invoice.objects.filter(rent_charge=charge_of(row)).count() == 1
    assert Invoice.objects.get(pk=first.pk).invoice_number == first.invoice_number
    assert ExternalRef.objects.filter(entity_type="invoice", entity_id=first.pk).count() == 1


def test_provider_status_cannot_be_written_from_the_ledger_directly() -> None:
    """The ledger's own ORM path must not set provider_status; only the sync writer may."""
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("the trigger exists on PostgreSQL")
    from django.db import IntegrityError, transaction

    row = lease()
    assert row.operator is not None
    rent_run.create_charges(DAY, actor=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.create(
            invoice_number="INV-2026-9999",
            operator=row.operator,
            issue_date=DAY,
            due_date=DAY,
            subtotal=RENT,
            tax=D("0.00"),
            total=RENT,
            provider_status="PAID",
            creation_key=uuid.uuid4(),
        )


# --- manual payments ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "note", "method_name"),
    [
        (D("1500.01"), "over", "wire"),
        (D("0.00"), "zero", "wire"),
        (D("-1.00"), "negative", "zelle"),
        (D("1.005"), "half cent", "zelle"),
        (D("100.00"), "", "wire"),
        (D("100.00"), "   ", "other"),
        (D("100.00"), f"paid by card {CARD}", "other"),
        (D("100.00"), "ok", "card"),
        (D("100.00"), "ok", "ach"),
        (D("100.00"), "ok", "card"),  # "cash" is accepted since PLAN-012f-b Batch C
    ],
)
def test_bad_manual_payments_save_nothing(
    fake: FakeProvider, amount: Decimal, note: str, method_name: str
) -> None:
    lease(debit_day=DAY.day)
    rent_run.create_charges(DAY, actor=None)
    charge = RentCharge.objects.get()
    outcome = rent_run.record_manual_payment(
        charge.pk, method=method_name, amount=amount, paid_on=DAY, note=note, actor=None, today=DAY
    )
    assert not outcome.ok
    assert Payment.objects.count() == 0
    charge.refresh_from_db()
    assert charge.amount_paid == D("0.00")


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_partial_payments_never_exceed_the_balance(fake: FakeProvider) -> None:
    lease(debit_day=DAY.day)
    rent_run.create_charges(DAY, actor=None)
    charge = RentCharge.objects.get()

    def pay() -> Any:
        return rent_run.record_manual_payment(
            charge.pk,
            method="wire",
            amount=D("1000.00"),
            paid_on=DAY,
            note="wire ref",
            actor=None,
            today=DAY,
        )

    _race(pay, pay, pay)
    charge.refresh_from_db()
    assert charge.amount_paid <= RENT
    assert Payment.objects.filter(rent_charge=charge).count() == 1


def test_viewer_and_billing_cannot_change_leases_or_enrol(fake: FakeProvider) -> None:
    row = make_lease()
    fake.add_payment_method(CUSTOMER, method("pm-x"))
    for role in (Role.VIEWER, Role.BILLING):
        client = _client(role)
        for path in (
            f"/leases/{row.pk}/enrol/",
            f"/leases/{row.pk}/pause/",
            f"/leases/{row.pk}/resume/",
        ):
            response = client.post(path, {"payment_method_id": "pm-x"})
            assert response.status_code == 403, (role, path)
    assert _schedule_creates(fake) == []


# --- PLAN-007c row 6: the large-debit confirmation ------------------------------------------------


@pytest.mark.parametrize(
    ("posted", "expect_created"),
    [
        ({}, False),
        ({"confirm_large_debit": "false"}, False),
        ({"confirm_large_debit": "0"}, False),
        ({"confirm_large_debit": ""}, False),
        ({"confirm_large_debit": "on"}, True),
        ({"confirm_large_debit": "true"}, True),
        ({"confirm_large_debit": "1"}, False),
    ],
)
def test_only_an_affirmative_checkbox_confirms_a_large_debit(
    fake: FakeProvider, posted: dict[str, str], expect_created: bool
) -> None:
    from django.test import override_settings

    row = make_lease(rent=D("2100.00"))
    fake.add_payment_method(CUSTOMER, method("pm-big"))
    admin = _client(Role.ADMIN)
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=None):
        admin.post(f"/leases/{row.pk}/enrol/", {"payment_method_id": "pm-big", **posted})
    assert (len(_schedule_creates(fake)) == 1) is expect_created
    from ledger.models import AuditLog

    assert (
        AuditLog.objects.filter(action="lease.autopay_large_debit_confirmed").exists()
        is expect_created
    )


def test_billing_cannot_confirm_a_large_debit(fake: FakeProvider) -> None:
    row = make_lease(rent=D("2100.00"))
    fake.add_payment_method(CUSTOMER, method("pm-big"))
    response = _client(Role.BILLING).post(
        f"/leases/{row.pk}/enrol/", {"payment_method_id": "pm-big", "confirm_large_debit": "on"}
    )
    assert response.status_code == 403
    assert _schedule_creates(fake) == []


def test_amounts_exactly_at_the_thresholds_need_no_confirmation_and_are_not_blocked(
    fake: FakeProvider,
) -> None:
    from django.test import override_settings

    with override_settings(
        LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=D("2000.00")
    ):
        row = make_lease(rent=D("2000.00"))
        fake.add_payment_method(CUSTOMER, method("pm-edge"))
        outcome = lease_service.enable_autopay(row.pk, "pm-edge", actor=None, now=NOW)
    assert outcome.ok, outcome.text
    assert len(_schedule_creates(fake)) == 1


def test_a_confirmation_replayed_after_a_failed_create_does_not_create_twice(
    fake: FakeProvider,
) -> None:
    from django.test import override_settings

    from payments.errors import ProviderUnavailable

    row = make_lease(rent=D("2100.00"))
    fake.add_payment_method(CUSTOMER, method("pm-retry"))
    fake.fail_next("create_scheduled_invoice", ProviderUnavailable(503))
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"), PROVIDER_MAX_TXN_AMOUNT=None):
        first = lease_service.enable_autopay(
            row.pk, "pm-retry", actor=None, confirmed=True, now=NOW
        )
        assert not first.ok
        second = lease_service.enable_autopay(
            row.pk, "pm-retry", actor=None, confirmed=True, now=NOW
        )
        assert second.ok, second.text
        third = lease_service.enable_autopay(
            row.pk, "pm-retry", actor=None, confirmed=True, now=NOW
        )
    assert third.action == lease_service.AutopayAction.ALREADY
    assert len(_schedule_creates(fake)) == 2  # the failed attempt and the successful one
    live = CollectionSchedule.objects.filter(lease=row, state=ScheduleState.ACTIVE)
    assert live.count() == 1


# --- PLAN-008a row 5: manual invoice linking under hostile input ------------------------------


def test_an_invoice_of_another_operator_cannot_be_linked_by_id(fake: FakeProvider) -> None:
    from ledger.models import Invoice
    from tests.test_run_views import a_charge, link_data, link_path, local_invoice

    charge = a_charge()
    other = make_operator(email="other@example.com", customer="cus-other")
    foreign = local_invoice(other, provider_id="foreign-invoice-0001")
    _client(Role.BILLING).post(link_path(charge), link_data(foreign))
    foreign.refresh_from_db()
    assert foreign.rent_charge_id is None
    assert Invoice.objects.filter(rent_charge=charge).count() == 0


def test_a_link_note_with_a_card_number_and_tampered_confirmations_link_nothing(
    fake: FakeProvider,
) -> None:
    from tests.test_run_views import a_charge, link_data, link_path, local_invoice

    charge = a_charge()
    operator = charge.lease.operator
    invoice = local_invoice(operator, total="1400.00", provider_id="dash-0002")  # differs
    billing = _client(Role.BILLING)
    for extra in (
        {"note": f"card {CARD}", "confirm_total_differs": "true"},
        {"note": "ok", "confirm_total_differs": "1"},
        {"note": "ok", "confirm_total_differs": "0"},
        {"note": "ok"},
        {"confirm_total_differs": "true"},
    ):
        data = link_data(invoice, **extra)
        if "confirm_total_differs" in extra:
            data["confirm"] = extra["confirm_total_differs"]
        billing.post(link_path(charge), data)
        invoice.refresh_from_db()
        assert invoice.rent_charge_id is None, extra


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_charges_racing_for_one_invoice_leave_it_on_exactly_one(fake: FakeProvider) -> None:
    from ledger.models import Invoice
    from tests.test_run_views import a_charge_of, local_invoice

    operator = make_operator(email="race@example.com", customer="cus-race")
    first = a_charge_of(operator)
    second = a_charge_of(operator)
    invoice = local_invoice(operator, provider_id="dash-race")

    def link(charge_id: Any) -> Any:
        try:
            return rent_run.link_invoice(charge_id, invoice.pk, confirm=False, note="", actor=None)
        except Exception as error:  # noqa: BLE001 - a refusal is fine, a crash is not
            return error
        finally:
            connections.close_all()

    results = _race(lambda: link(first.pk), lambda: link(second.pk))
    assert not any(isinstance(r, Exception) and not isinstance(r, ValueError) for r in results)
    invoice.refresh_from_db()
    assert invoice.rent_charge_id in {first.pk, second.pk}
    assert Invoice.objects.filter(rent_charge__in=[first.pk, second.pk]).count() == 1
