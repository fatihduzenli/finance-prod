"""Adversarial cases for PLAN-012b-a part 2 (a split that releases the parent's Stax object),
by the QA owner (plan §6). A release job acts on its own entity whatever its parameters carry;
two release jobs queued at once still queue the installments once; the parent cannot be split
again while its release is pending; a viewer sees no release control; a late provider payment
on the released invoice lands on the parent and puts the plan on the attention list, never on
an installment; the money of a released parent is counted once."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    InstallmentPlan,
    InstallmentPlanStatus,
    OneOffCategory,
    OneOffCharge,
    Payment,
    PaymentStatus,
)
from ledger.services import attention, installments, one_off_charges, payments_ledger, pnl
from payments.fake import FakeProvider
from payments.models import Job, JobKind
from tests import test_installments as installment_tests
from tests.job_helpers import run_queued_jobs
from tests.test_installments import (
    CUSTOMER,
    TODAY,
    _settle_installment,
    live_emailed,
    plan_for,
    provider_ref,
    rows_of,
)
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import deliver, provider_txn, settle

pytestmark = pytest.mark.django_db

D = Decimal
# The installment file's fixtures, re-exported so pytest finds them here.
fixed_now = installment_tests.fixed_now
fake = installment_tests.fake
worker = installment_tests.worker
ticking = installment_tests.ticking


def _delete_calls(provider: FakeProvider) -> list[tuple[object, ...]]:
    return [arguments for method, arguments in provider.calls if method == "delete_invoice"]


def _installment_jobs(plan: InstallmentPlan) -> list[Job]:
    ids = [str(row.pk) for row in rows_of(plan)]
    return list(Job.objects.filter(entity_id__in=ids, kind=JobKind.INVOICE_CREATE))


# --- the job acts on its own entity -------------------------------------------------------------


def test_a_release_job_cannot_be_pointed_at_another_charges_invoice_by_its_parameters(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from payments import jobs

    mine, my_invoice = live_emailed(an_operator(fake), "INV-2026-9001")
    other_operator = an_operator(fake, customer="cus-other-1", bank="pm-other-1")
    theirs, their_invoice = live_emailed(other_operator, "INV-2026-9002")
    plan = plan_for(mine)
    # A second release job on my charge, its parameters naming the other charge's objects.
    jobs.enqueue(
        JobKind.INVOICE_DELETE,
        mine,
        {
            installments.RELEASE_PARAM: True,
            "entity_id": str(theirs.pk),
            "charge_id": str(theirs.pk),
            "invoice": provider_ref(their_invoice),
            "external_id": provider_ref(their_invoice),
        },
        None,
    )
    run_queued_jobs()
    assert one_off_charges.live_provider_invoice(theirs) is not None
    assert one_off_charges.live_provider_invoice(mine) is None
    deleted = _delete_calls(fake)
    assert len(deleted) == 1 and provider_ref(my_invoice) in map(str, deleted[0])
    assert provider_ref(their_invoice) not in str(deleted)
    theirs.refresh_from_db()
    assert theirs.status == "owed" and theirs.balance == D("1000.00")
    assert len(_installment_jobs(plan)) == 3


def test_two_release_jobs_queued_at_once_still_queue_the_installments_once(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from payments import jobs

    parent, _ = live_emailed(an_operator(fake))
    plan = plan_for(parent)
    jobs.enqueue(JobKind.INVOICE_DELETE, parent, {installments.RELEASE_PARAM: True}, None)
    jobs.enqueue(JobKind.INVOICE_DELETE, parent, {installments.RELEASE_PARAM: True}, None)
    run_queued_jobs()
    assert len(_installment_jobs(plan)) == 3
    assert len(_delete_calls(fake)) == 1
    assert (
        AuditLog.objects.filter(action=installments.RELEASED_ACTION, entity_id=str(plan.pk)).count()
        == 1
    )
    parent.refresh_from_db()
    assert parent.status == "owed" and parent.price == D("1000.00")
    assert parent.amount_paid == D("0.00")


# --- no second split, no viewer control -----------------------------------------------------------


def test_the_parent_cannot_be_split_again_while_its_release_is_pending(
    fake: FakeProvider, ticking: None
) -> None:
    parent, _ = live_emailed(an_operator(fake))
    plan = plan_for(parent)
    assert installments.release_pending(plan)
    assert installments.split_refusal(parent) == installments.HAS_PLAN
    billing, _ = client_for(Role.BILLING)
    response = billing.post(
        f"/one-off-charges/{parent.pk}/split/",
        {"count": "2", "first_due": TODAY.isoformat(), "cadence": "weekly"},
    )
    assert response.status_code == 302
    assert InstallmentPlan.objects.filter(one_off_charge=parent).count() == 1
    assert OneOffCharge.objects.filter(category=OneOffCategory.INSTALLMENT).count() == 3


def test_a_viewer_sees_no_release_notice_retry_or_plan_action(
    fake: FakeProvider, ticking: None
) -> None:
    parent, _ = live_emailed(an_operator(fake))
    plan = plan_for(parent)
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get(f"/one-off-charges/{parent.pk}/split/").status_code == 403
    page = viewer.get(f"/plans/{plan.pk}/").content.decode()
    assert "/retry/" not in page and "/reschedule/" not in page and "/cancel/" not in page
    charge_page = viewer.get(f"/one-off-charges/{parent.pk}/").content.decode()
    assert "/retry/" not in charge_page and "/split/" not in charge_page
    assert viewer.post(f"/one-off-charges/{parent.pk}/retry/", {}).status_code == 403


# --- a late payment on the released invoice -------------------------------------------------------


def test_a_late_provider_payment_on_the_released_invoice_lands_on_the_parent_only(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    parent, invoice = live_emailed(an_operator(fake))
    plan = plan_for(parent)
    run_queued_jobs()
    assert one_off_charges.live_provider_invoice(parent) is None
    assert len(_installment_jobs(plan)) == 3
    # The operator paid the old invoice after all (a late webhook on the deleted invoice).
    provider_txn(
        fake,
        "txn-late-1",
        total=D("400.00"),
        invoice_external_id=provider_ref(invoice),
        customer_external_id=CUSTOMER,
    )
    deliver("create_transaction", id="txn-late-1")
    settle(fake, "txn-late-1")
    deliver("update_transaction_settled", id="txn-late-1")
    assert not Payment.objects.filter(one_off_charge__plan=plan).exists()
    landed = Payment.objects.filter(status=PaymentStatus.SETTLED, is_refund=False)
    assert landed.count() == 1 and landed.get().one_off_charge_id == parent.pk
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.ATTENTION
    items = [i for i in attention.attention(TODAY).items if i.kind == attention.SPLIT_CHARGE_PAID]
    assert len(items) == 1 and "400.00" in items[0].text
    # Nothing was voided, nothing owed twice: the parent's balance fell, the plan still bills.
    parent.refresh_from_db()
    assert parent.status == "partial" and parent.balance == D("600.00")


# --- the money is counted once --------------------------------------------------------------------


def test_a_released_and_split_charge_is_counted_once_like_the_unsplit_one(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from tests.test_lease_services import make_lease

    operator = an_operator(fake)
    lease = make_lease(operator=operator, payment_channel="wire")
    split_parent, _ = live_emailed(operator, "INV-2026-9001", lease=lease)
    other_operator = an_operator(fake, customer="cus-other-2", bank="pm-other-2")
    other_lease = make_lease(operator=other_operator, payment_channel="wire")
    whole, whole_invoice = live_emailed(other_operator, "INV-2026-9002", lease=other_lease)
    plan = plan_for(split_parent)
    run_queued_jobs()
    run_queued_jobs()
    for number, row in enumerate(rows_of(plan), start=1):
        _settle_installment(fake, row, f"txn-split-{number}")
    provider_txn(
        fake,
        "txn-whole",
        total=D("1000.00"),
        invoice_external_id=provider_ref(whole_invoice),
        customer_external_id="cus-other-2",
    )
    deliver("create_transaction", id="txn-whole")
    settle(fake, "txn-whole")
    assert deliver("update_transaction_settled", id="txn-whole") == "processed"
    split_parent.refresh_from_db()
    whole.refresh_from_db()
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.COMPLETED
    assert split_parent.status == "paid" and whole.status == "paid"
    query = pnl.parse_query({"from": "2026-08", "to": "2026-11"}, today=TODAY)
    rows = pnl.rows(query)

    def figures(store_id: uuid.UUID | None) -> list[tuple[object, ...]]:
        return [
            (r.month, r.rent_billed, r.rent_collected, r.fees_billed, r.fees_collected)
            for r in rows
            if r.store_id == store_id
        ]

    assert figures(lease.store_id) == figures(other_lease.store_id)
    assert sum(r.rent_collected for r in rows) == D("2000.00")

    def ledger_total(store_id: uuid.UUID | None) -> Decimal:
        selected = payments_ledger.PaymentFilter(store=store_id)
        return payments_ledger.totals(payments_ledger.ledger_payments(selected)).settled

    assert ledger_total(lease.store_id) == ledger_total(other_lease.store_id) == D("1000.00")
    assert not Payment.objects.filter(one_off_charge=split_parent).exists()
