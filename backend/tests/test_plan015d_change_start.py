"""PLAN-015d task 6: Change start date — preview and apply, every branch of §3 (1) (the unsent
draft rebuilt, the sent unpaid invoice voided and redrafted, paid rent kept as a credit or
refunded through the refund job, the months before the new start void, an earlier start into a
paid month billing the extra days with a confirmation, autopay moved by a job, the deposit's
tick), the refusals (overlap, too far, the same date, an active lease's future date, past its
first month), the roles, the dialog and the timeline (docs/qa/15/PLAN-015d §3 (1), §9.1, §9.3)."""

from __future__ import annotations

import html
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionStatus,
    InvoiceStatus,
    Lease,
    LeaseCredit,
    OneOffCharge,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import invoice_builder, move_in, rent_run, start_date, timeline
from payments.fake import FakeProvider
from payments.models import Job, JobKind
from tests import test_invoice_builder as builder_tests
from tests.job_helpers import run_queued_jobs
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_builder_lines import a_deposit
from tests.test_invoice_builder import a_rent_lease, fresh, sent, user
from tests.test_one_off_charges import an_operator as a_provider_operator
from tests.test_one_off_charges import calls
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

fixed_now = builder_tests.fixed_now  # 2026-10-06
fake = builder_tests.fake
worker = builder_tests.worker
D = Decimal
NOV_5 = date(2026, 11, 5)


@pytest.fixture
def nov_5(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: NOV_5)
    return NOV_5


def admin() -> Any:
    return user(Role.ADMIN)


def an_active_lease(started: date = date(2026, 11, 1), **fields: Any) -> Lease:
    """An active lease started this month (rent $3,000.00, debit day 1) and its first charge."""
    lease = a_lease(
        a_store(f"Store {uuid.uuid4().hex[:4]}"),
        an_operator(f"Start {uuid.uuid4().hex[:6]}"),
        started=started,
        rent="3000.00",
        debit_day=1,
        **fields,
    )
    rent_run.catch_up_lease(lease, NOV_5, None)
    return lease


def november(lease: Lease) -> RentCharge:
    return RentCharge.objects.get(lease=lease, period_month=date(2026, 11, 1))


def pay(charge: RentCharge, amount: str, method: str = PaymentMethod.ZELLE) -> Payment:
    payment = Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=method,
        amount=D(amount),
        status=PaymentStatus.SETTLED,
        initiated_on=charge.due_on,
        settled_on=charge.due_on,
        note="Paid." if method != PaymentMethod.ACH else "",
    )
    rent_run.recompute_charge(RentCharge.objects.get(pk=charge.pk))
    return payment


def apply(lease: Lease, new: date, actor: Any = None, **options: Any) -> start_date.StartDateChange:
    values: dict[str, Any] = {
        "reason": "The operator moved in later.",
        "choice": None,
        "move_deposit": False,
        "actor": actor or admin(),
        "today": options.pop("today", NOV_5),
    }
    values.update(options)
    return start_date.apply_change(lease.pk, new, **values)


def refused(lease: Lease, new: date, **options: Any) -> start_date.StartDateRefused:
    with pytest.raises(start_date.StartDateRefused) as raised:
        apply(lease, new, **options)
    return raised.value


# --- refusals --------------------------------------------------------------------------------


def test_the_refusals_write_nothing(nov_5: date) -> None:
    lease = an_active_lease()
    assert refused(lease, date(2026, 11, 1)).message == start_date.SAME_DATE
    assert refused(lease, date(2026, 11, 9)).message == start_date.ACTIVE_FUTURE
    assert refused(lease, date(2025, 10, 1)).message == start_date.TOO_FAR
    assert refused(lease, date(2026, 11, 3), reason=" ").message == start_date.REASON_REQUIRED
    card = "4111 1111 1111 1111"
    assert refused(lease, date(2026, 11, 3), reason=card).field == "reason"
    past = an_active_lease(started=date(2026, 10, 1))
    assert refused(past, date(2026, 10, 3)).message == start_date.NOT_CHANGEABLE
    assert Lease.objects.get(pk=lease.pk).started_on == date(2026, 11, 1)
    assert not AuditLog.objects.filter(action=start_date.CHANGED_ACTION).exists()


def test_a_start_inside_a_month_another_lease_is_billed_for_is_refused(nov_5: date) -> None:
    lease = an_active_lease()
    other = a_lease(lease.store, an_operator("Before"), started=date(2026, 10, 1), rent="900.00")
    RentCharge.objects.create(
        lease=other,
        period_month=date(2026, 10, 1),
        due_on=date(2026, 10, 1),
        amount_due=D("900.00"),
        status=RentChargeStatus.DUE,
    )
    message = refused(lease, date(2026, 10, 20)).message
    assert message == start_date.OVERLAP.format(month="October 2026")


# --- an active lease in its first month -------------------------------------------------------


def test_a_later_start_reprices_the_unpaid_first_month_and_moves_the_rent_row(nov_5: date) -> None:
    lease = an_active_lease()
    charge = november(lease)
    assert charge.amount_due == D("3000.00")
    change = apply(lease, date(2026, 11, 4))
    lease.refresh_from_db()
    assert lease.started_on == date(2026, 11, 4)
    assert lease.rent_history.get().effective_from == date(2026, 11, 4)
    charge = fresh(charge)
    # 4–30 Nov, 27 of 30 days of $3,000.00.
    assert (charge.amount_due, charge.due_on, charge.status) == (
        D("2700.00"),
        date(2026, 11, 4),
        RentChargeStatus.DUE,
    )
    audit = AuditLog.objects.get(action=start_date.CHANGED_ACTION)
    assert (audit.after["old_start"], audit.after["new_start"]) == ("2026-11-01", "2026-11-04")
    assert audit.after["reason"] == "The operator moved in later."
    assert audit.before["started_on"] == "2026-11-01"
    assert change.paid_back == D("0")


def test_paid_rent_for_days_that_move_needs_a_choice_and_an_admin(nov_5: date) -> None:
    lease = an_active_lease()
    pay(november(lease), "3000.00")
    assert refused(lease, date(2026, 11, 4)).message == start_date.CHOICE_REQUIRED
    billing = user(Role.BILLING)
    assert refused(lease, date(2026, 11, 4), actor=billing, choice="credit").message == (
        start_date.ADMIN_ONLY
    )
    assert refused(lease, date(2026, 11, 4), choice="refund").message == start_date.REFUND_NOT_ACH


def test_kept_as_credit_takes_the_rent_off_the_charge_into_a_credit(nov_5: date) -> None:
    lease = an_active_lease()
    payment = pay(november(lease), "3000.00")
    change = apply(lease, date(2026, 11, 4), choice="credit")
    assert change.paid_back == D("300.00") and change.outcome == start_date.OUTCOME_CREDIT
    charge = november(lease)
    assert (charge.amount_due, charge.amount_paid, charge.status, charge.balance) == (
        D("2700.00"),
        D("2700.00"),
        RentChargeStatus.PAID,
        D("0.00"),
    )
    refund = Payment.objects.get(parent_payment=payment)
    assert (refund.is_refund, refund.amount, refund.status) == (True, D("300.00"), "settled")
    credit = LeaseCredit.objects.get(lease=lease)
    assert (credit.amount, credit.remaining, credit.method) == (D("300.00"), D("300.00"), "zelle")
    assert not Job.objects.filter(kind=JobKind.PAYMENT_REFUND).exists()


def test_refund_queues_the_refund_job_and_never_calls_the_provider(
    nov_5: date, fake: FakeProvider
) -> None:
    lease = an_active_lease()
    payment = pay(november(lease), "3000.00", PaymentMethod.ACH)
    change = apply(lease, date(2026, 11, 4), choice="refund")
    assert change.outcome == start_date.OUTCOME_REFUND
    [job] = Job.objects.filter(kind=JobKind.PAYMENT_REFUND)
    assert (job.entity_id, job.params["amount"]) == (payment.pk, "300.00")
    assert job.params["confirmed"] is True
    assert not fake.calls
    assert not LeaseCredit.objects.exists()


def test_an_earlier_start_into_a_paid_month_bills_the_extra_days_once_confirmed(
    nov_5: date,
) -> None:
    lease = an_active_lease(started=date(2026, 11, 3))
    charge = november(lease)
    assert charge.amount_due == D("2800.00")  # 3–30 Nov, 28 of 30 days
    pay(charge, "2800.00")
    preview = start_date.preview(lease, date(2026, 11, 1), today=NOV_5)
    assert (preview.extra, preview.paid_back) == (D("200.00"), D("0"))
    assert preview.extra_text == (
        "Nov 1 – Nov 2: $200.00 more billed on the November rent (the start moved earlier)."
    )
    assert refused(lease, date(2026, 11, 1)).message == start_date.CONFIRM_REQUIRED
    billing = user(Role.BILLING)
    assert refused(lease, date(2026, 11, 1), actor=billing, confirmed=True).message == (
        start_date.ADMIN_ONLY
    )
    apply(lease, date(2026, 11, 1), confirmed=True)
    charge = november(lease)
    assert (charge.amount_due, charge.amount_paid, charge.status, charge.balance) == (
        D("3000.00"),
        D("2800.00"),
        RentChargeStatus.PARTIAL,
        D("200.00"),
    )
    audit = AuditLog.objects.get(action=start_date.CHANGED_ACTION)
    assert audit.after["extra"] == "200.00"


def test_an_earlier_start_into_last_month_charges_it_and_prices_this_month_in_full(
    nov_5: date,
) -> None:
    lease = an_active_lease(started=date(2026, 11, 3))
    apply(lease, date(2026, 10, 25))
    assert november(lease).amount_due == D("3000.00")
    october = RentCharge.objects.get(lease=lease, period_month=date(2026, 10, 1))
    # 25–31 Oct, 7 of 31 days of $3,000.00.
    assert (october.amount_due, october.due_on) == (D("677.42"), date(2026, 10, 25))


def test_autopay_is_moved_by_a_job_from_the_new_date(
    nov_5: date, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider
) -> None:
    lease = an_active_lease()
    monkeypatch.setattr("ledger.services.start_date.leases.autopay_on", lambda lease: True)
    apply(lease, date(2026, 11, 4))
    [job] = Job.objects.filter(kind=JobKind.AUTOPAY_MOVE)
    assert (job.entity_id, job.params["start"]) == (lease.pk, "2026-11-04")
    assert not fake.calls


# --- a prospective lease and its move-in invoice ------------------------------------------------


def a_prospective(fake: FakeProvider) -> tuple[Lease, Any]:
    operator = a_provider_operator(fake)
    lease = a_rent_lease(operator, collection_status=CollectionStatus.PROSPECTIVE)
    deposit = a_deposit(lease)
    made = move_in.create_draft(
        lease,
        [deposit],
        creation_key=uuid.uuid4(),
        actor=user(Role.BILLING),
        today=date(2026, 10, 6),
    )
    assert made.draft is not None, made.message
    return lease, made.draft


def test_billing_moves_a_prospective_start_and_the_draft_is_made_again(fake: FakeProvider) -> None:
    lease, draft = a_prospective(fake)
    change = apply(lease, date(2026, 11, 10), actor=user(Role.BILLING), today=date(2026, 10, 6))
    assert change.outcome == start_date.OUTCOME_REBUILT
    new = move_in.current_draft(lease)
    assert new is not None and new.pk != draft.pk
    rent, _deposit = invoice_builder.lines_of(new)
    assert (rent.rent_month, rent.unit_amount) == (date(2026, 11, 1), D("700.00"))
    assert not fake.calls


def test_a_sent_unpaid_move_in_invoice_is_voided_by_the_worker_and_redrafted(
    fake: FakeProvider, worker: None
) -> None:
    lease, draft = a_prospective(fake)
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    october = RentCharge.objects.get(lease=lease, period_month=date(2026, 10, 1))
    change = apply(lease, date(2026, 11, 10), actor=user(Role.BILLING), today=date(2026, 10, 6))
    assert change.outcome == start_date.OUTCOME_VOIDED
    assert not calls(fake, "delete_invoice")  # the view's request never calls the provider
    run_queued_jobs()
    assert len(calls(fake, "delete_invoice")) == 1
    assert fresh(invoice).internal_status == InvoiceStatus.VOID
    assert fresh(october).status == RentChargeStatus.VOID
    assert move_in.current_draft(lease) is not None


def test_the_deposit_moves_only_when_ticked_and_only_its_future_installments(
    fake: FakeProvider,
) -> None:
    lease, _draft = a_prospective(fake)
    deposit = OneOffCharge.objects.get(lease=lease, fee_type__refundable=True)
    # Take it off the draft: due on a date of its own.
    move_in.current_draft(lease).delete()  # type: ignore[union-attr]
    OneOffCharge.objects.filter(pk=deposit.pk).update(due_date=date(2026, 10, 20))
    apply(lease, date(2026, 10, 20), actor=user(Role.BILLING), today=date(2026, 10, 6))
    assert fresh(deposit).due_date == date(2026, 10, 20)
    apply(
        lease,
        date(2026, 10, 25),
        actor=user(Role.BILLING),
        today=date(2026, 10, 6),
        move_deposit=True,
    )
    assert fresh(deposit).due_date == date(2026, 10, 25)
    audit = AuditLog.objects.get(action=start_date.CHANGED_ACTION, after__new_start="2026-10-25")
    assert audit.after["deposit_moved"] is True


# --- the route, the dialog, the timeline --------------------------------------------------------


def test_the_roles_on_the_route(nov_5: date) -> None:
    lease = an_active_lease()
    path = f"/leases/{lease.pk}/change-start/"
    viewer = client_for(Role.VIEWER, "viewer-start")[0]
    billing = client_for(Role.BILLING, "billing-start")[0]
    assert viewer.get(path).status_code == 403
    assert billing.get(path).status_code == 403  # an active lease: admin
    data = {"new_start": "2026-11-04", "reason": "Moved in later."}
    assert billing.post(path, data).status_code == 403
    assert Lease.objects.get(pk=lease.pk).started_on == date(2026, 11, 1)
    admin_client = client_for(Role.ADMIN, "admin-start")[0]
    page = admin_client.get(path + "?date=2026-11-04").content.decode()
    assert 'id="change-start-effects"' in page
    assert "Start date: Nov 1, 2026 → Nov 4, 2026." in page
    assert "First month at the new date: $2,700.00." in page
    response = admin_client.post(path, data)
    assert response.status_code == 302
    assert Lease.objects.get(pk=lease.pk).started_on == date(2026, 11, 4)


def test_the_dialog_shows_a_refusal_and_the_lease_card_offers_the_link(nov_5: date) -> None:
    lease = an_active_lease()
    admin_client = client_for(Role.ADMIN, "admin-card")[0]
    store_page = admin_client.get(f"/stores/{lease.store_id}/").content.decode()
    assert f'id="change-start" href="/leases/{lease.pk}/change-start/"' in store_page
    billing = client_for(Role.BILLING, "billing-card")[0]
    assert 'id="change-start"' not in billing.get(f"/stores/{lease.store_id}/").content.decode()
    page = admin_client.get(f"/leases/{lease.pk}/change-start/?date=2026-11-01").content.decode()
    assert html.escape(start_date.SAME_DATE) in page and 'id="change-start-submit"' not in page


def test_the_timeline_lists_the_change(nov_5: date, client: Client) -> None:
    lease = an_active_lease()
    apply(lease, date(2026, 11, 4))
    [row] = timeline.status_changes(lease.store)
    words = timeline.status_words(row)
    assert (
        words.text == "Start date changed Nov 1, 2026 → Nov 4, 2026 — The operator moved in later."
    )
    assert (words.label, words.tone) == ("Active", "paid")
