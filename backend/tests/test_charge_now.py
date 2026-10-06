"""Charge now (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D4, D9; §6 test plan
items for Charge now, happy paths, boundaries and error paths; the adversarial cases are
the QA owner's file).

The service and its confirmation page, the worker's single attempt through the queue,
the transaction applied as a pending ``one_off`` payment that a webhook settles, the
flag, the one-minute wait, and the stored idempotency identifier. Nothing reaches a
network: the payment provider is a :class:`~payments.fake.FakeProvider`; the adapter's
single request is tested in ``test_stax_adapter.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone
from django.utils.html import escape

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    InvoiceStatus,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services import one_off_charges as service
from payments import jobs, one_off_invoices
from payments.errors import ProviderRejected, ProviderUnavailable, TransportError
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind, JobStatus, ProviderSnapshot
from payments.provider import PayWithStoredMethod
from tests.job_helpers import run_queued_jobs
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import (
    BANK,
    CUSTOMER,
    an_operator,
    calls,
    created,
    fresh,
    invoice_of,
    item,
    provider_ref,
)
from tests.test_operator_views import client_for
from tests.test_webhook_handlers import deliver

pytestmark = pytest.mark.django_db

D = Decimal
LAST_FOUR = "6789"
NICKNAME = "Operating account"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


def store_methods(operator: Operator, *, method: str = "bank", status: str = "") -> None:
    """The operator's method list as the worker's refresh stores it (no provider call)."""
    ref = ExternalRef.objects.get(
        entity_type="operator", entity_id=operator.pk, external_kind="customer"
    )
    ProviderSnapshot.objects.create(
        external_ref=ref,
        payload=[
            {
                "external_id": BANK,
                "raw_status": status,
                "method": method,
                "last_four": LAST_FOUR,
                "bank_type": "checking",
                "nickname": NICKNAME,
                "is_default": True,
            }
        ],
        fetched_at=timezone.now(),
    )


def operator_of(charge: OneOffCharge) -> Operator:
    assert charge.operator is not None
    return charge.operator


def admin() -> Any:
    return client_for(Role.ADMIN)


def ready(
    fake: FakeProvider,
    amount: str = "100.00",
    *,
    lease: bool = False,
    methods: bool = True,
    operator: Operator | None = None,
) -> OneOffCharge:
    """A Charge now charge whose invoice is at the provider, as the worker leaves it. A
    second charge of a test is made for the first one's ``operator``."""
    if operator is None:
        operator = an_operator(fake)
        if methods:
            store_methods(operator)
    on_lease = make_lease(operator=operator) if lease else None
    charge = created(
        operator,
        item(CollectionMethod.CHARGE_NOW, amount, confirmed=True),
        lease=on_lease,
        actor=admin()[1],
    )
    run_queued_jobs()
    job = Job.objects.get(kind=JobKind.INVOICE_CREATE, entity_id=charge.pk)
    assert job.status == JobStatus.DONE, job.result
    fake.calls.clear()
    return fresh(charge)


def confirm(charge: OneOffCharge, actor: Any = None, confirmed: bool = False) -> service.Outcome:
    return service.charge_now_confirm(charge.pk, actor=actor or admin()[1], confirmed=confirmed)


def actions(charge: OneOffCharge) -> list[str]:
    return list(
        AuditLog.objects.filter(entity_type="one_off_charge", entity_id=str(charge.pk))
        .order_by("id")
        .values_list("action", flat=True)
    )


def pays(fake: FakeProvider) -> list[tuple[object, ...]]:
    return calls(fake, "pay_invoice")


# --- Happy path -----------------------------------------------------------------------------


def test_charge_now_makes_one_pay_call_with_the_stored_key_and_a_pending_payment(
    fake: FakeProvider, enabled: None
) -> None:
    """The batch's done-when: Charge now with the fake provider yields a pending
    ``one_off`` payment."""
    charge = ready(fake)
    outcome = confirm(charge)
    assert (outcome.ok, outcome.message) == (True, service.CHARGE_NOW_REQUESTED)
    charge = fresh(charge)
    assert charge.charge_now_key is not None
    job = jobs.latest_for(charge)
    assert job is not None and (job.kind, job.status) == (JobKind.INVOICE_PAY, JobStatus.QUEUED)
    assert pays(fake) == []  # the service never calls the provider
    [done] = run_queued_jobs()
    assert done.status == JobStatus.DONE, done.result
    assert done.result == one_off_invoices.CHARGED
    ref = provider_ref(invoice_of(charge))
    [(paid_ref, request)] = pays(fake)
    assert paid_ref == ref
    assert request == PayWithStoredMethod(BANK, str(charge.charge_now_key), email_receipt=True)
    # The invoice was re-fetched before the call.
    names = [name for name, _ in fake.calls]
    assert names.index("get_invoice") < names.index("pay_invoice")
    [payment] = Payment.objects.filter(one_off_charge=charge)
    assert (payment.kind, payment.status, payment.amount, payment.is_refund) == (
        PaymentKind.ONE_OFF,
        PaymentStatus.PENDING,
        D("100.00"),
        False,
    )
    assert payment.invoice == invoice_of(charge)
    charge = fresh(charge)
    assert charge.status == OneOffChargeStatus.OWED  # paid only once the debit settles
    assert actions(charge)[-2:] == [
        service.CHARGE_NOW_REQUESTED_ACTION,
        service.CHARGED_ACTION,
    ]
    charged = AuditLog.objects.filter(action=service.CHARGED_ACTION).get()
    assert charged.after is not None
    assert charged.after["payment"] == str(payment.pk)
    assert ref not in str(charged.after) and BANK not in str(charged.after)
    page = admin()[0].get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert "Pending" in page
    assert 'id="charge-now"' not in page  # a pending payment stops a second charge
    assert service.CHARGE_NOW_HAS_PAYMENT in page


def test_the_payment_settles_by_webhook_and_pays_the_charge(
    fake: FakeProvider, enabled: None
) -> None:
    charge = ready(fake)
    confirm(charge)
    run_queued_jobs()
    [payment] = Payment.objects.filter(one_off_charge=charge)
    txn = next(iter(fake.transactions))
    fake.transactions[txn] = replace(fake.transactions[txn], settled_at=timezone.now())
    assert deliver("update_transaction_settled", id=txn) == "processed"
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.SETTLED
    charge = fresh(charge)
    assert (charge.status, charge.amount_paid) == (OneOffChargeStatus.PAID, D("100.00"))


def test_the_key_is_set_once_and_reused_by_a_later_attempt(
    fake: FakeProvider, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    charge = ready(fake)
    fake.fail_next("pay_invoice", TransportError())
    confirm(charge)
    run_queued_jobs()
    key = fresh(charge).charge_now_key
    assert key is not None
    later = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("django.utils.timezone.now", lambda: later)
    assert confirm(charge).ok
    run_queued_jobs()
    assert fresh(charge).charge_now_key == key
    assert [request.idempotency_id for _, request in pays(fake)] == [str(key)] * 2  # type: ignore[attr-defined]


def test_the_key_cannot_change_once_set(fake: FakeProvider, enabled: None) -> None:
    charge = ready(fake)
    confirm(charge)
    charge = fresh(charge)
    key = charge.charge_now_key
    charge.charge_now_key = uuid.uuid4()
    with pytest.raises(ValidationError):
        charge.save(update_fields=["charge_now_key"])
    with pytest.raises(ValidationError):
        charge.save()
    charge.charge_now_key = None
    with pytest.raises(ValidationError):
        charge.save()
    assert fresh(charge).charge_now_key == key
    # Saving other fields of the charge still works.
    same = fresh(charge)
    same.needs_review = True
    same.save()
    assert fresh(charge).needs_review is True


# --- The confirmation page ------------------------------------------------------------------


def test_the_confirmation_page_repeats_the_details_and_never_calls_the_provider(
    fake: FakeProvider, enabled: None
) -> None:
    charge = ready(fake, lease=True)
    client, _ = admin()
    page = client.get(f"/one-off-charges/{charge.pk}/charge-now/").content.decode()
    assert fake.calls == []
    assert charge.lease is not None
    assert charge.lease.store.external_label in page
    assert "Northwind Stores" in page
    assert "$100.00" in page
    assert invoice_of(charge).invoice_number in page
    assert f"ending {LAST_FOUR} ({NICKNAME})" in page
    assert service.CHARGE_NOW_ONE_ATTEMPT in page
    assert 'id="charge-now-large-debit"' not in page
    assert BANK not in page and CUSTOMER not in page
    detail = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert 'id="charge-now"' in detail
    assert fake.calls == []


def test_an_operator_level_charge_says_so(fake: FakeProvider, enabled: None) -> None:
    charge = ready(fake)
    page = admin()[0].get(f"/one-off-charges/{charge.pk}/charge-now/").content.decode()
    assert "Operator-level" in page


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"))
def test_the_large_debit_box_is_shown_and_required_above_the_threshold(
    fake: FakeProvider, enabled: None
) -> None:
    at = ready(fake, "2000.00")
    page = admin()[0].get(f"/one-off-charges/{at.pk}/charge-now/").content.decode()
    assert 'id="charge-now-large-debit"' not in page
    assert confirm(at).ok
    above = ready(fake, "2000.01", operator=operator_of(at))
    page = admin()[0].get(f"/one-off-charges/{above.pk}/charge-now/").content.decode()
    assert 'id="charge-now-large-debit"' in page
    refused = confirm(above)
    assert (refused.ok, refused.field) == (False, "confirm")
    assert not Job.objects.filter(kind=JobKind.INVOICE_PAY, entity_id=above.pk).exists()
    assert confirm(above, confirmed=True).ok
    job = Job.objects.get(kind=JobKind.INVOICE_PAY, entity_id=above.pk)
    assert job.params == {"confirmed": True}


def test_the_page_post_queues_the_job_and_redirects(fake: FakeProvider, enabled: None) -> None:
    charge = ready(fake)
    client, _ = admin()
    response = client.post(f"/one-off-charges/{charge.pk}/charge-now/", {})
    assert response.status_code == 302
    assert response["Location"] == f"/one-off-charges/{charge.pk}/"
    assert Job.objects.filter(kind=JobKind.INVOICE_PAY, entity_id=charge.pk).count() == 1
    assert pays(fake) == []


# --- Flag and roles -------------------------------------------------------------------------


def test_with_the_flag_off_the_page_says_so_and_nothing_is_queued(fake: FakeProvider) -> None:
    with override_settings(STAX_PAY_INVOICE_ENABLED=True):
        charge = ready(fake)
    client, user = admin()
    page = client.get(f"/one-off-charges/{charge.pk}/charge-now/").content.decode()
    assert "Charge now is not enabled in this environment" in page
    assert 'id="charge-now-form"' not in page
    client.post(f"/one-off-charges/{charge.pk}/charge-now/", {})
    refused = confirm(charge, user)
    assert (refused.ok, refused.message) == (False, service.CHARGE_NOW_DISABLED)
    assert not Job.objects.filter(kind=JobKind.INVOICE_PAY).exists()
    assert fresh(charge).charge_now_key is None
    assert pays(fake) == []


def test_a_job_queued_before_the_flag_was_turned_off_calls_nothing(
    fake: FakeProvider, enabled: None
) -> None:
    charge = ready(fake)
    confirm(charge)
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.FAILED, service.CHARGE_NOW_DISABLED)
    assert fake.calls == []


def test_billing_and_viewer_get_403_on_the_page_and_the_post(
    fake: FakeProvider, enabled: None
) -> None:
    charge = ready(fake)
    for role in (Role.BILLING, Role.VIEWER):
        client, user = client_for(role)
        assert client.get(f"/one-off-charges/{charge.pk}/charge-now/").status_code == 403
        assert client.post(f"/one-off-charges/{charge.pk}/charge-now/", {}).status_code == 403
        assert confirm(charge, user).message == service.CHARGE_NOW_ADMIN
        detail = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
        assert 'id="charge-now"' not in detail
    assert not Job.objects.filter(kind=JobKind.INVOICE_PAY).exists()


# --- Refusals before any call ---------------------------------------------------------------


def test_without_a_linked_method_nothing_is_queued(fake: FakeProvider, enabled: None) -> None:
    charge = ready(fake)
    ExternalRef.objects.filter(external_kind="payment_method").delete()
    assert confirm(charge).message == service.CHARGE_NOW_NO_METHOD
    assert not Job.objects.filter(kind=JobKind.INVOICE_PAY).exists()


@pytest.mark.parametrize(
    ("method", "status"), [("card", ""), ("bank", "deleted")], ids=["card", "deleted"]
)
def test_a_method_the_snapshot_does_not_show_as_a_usable_bank_is_refused(
    fake: FakeProvider, enabled: None, method: str, status: str
) -> None:
    charge = ready(fake, methods=False)
    store_methods(operator_of(charge), method=method, status=status)
    assert confirm(charge).message == service.CHARGE_NOW_METHOD_NOT_STORED


def test_a_method_missing_from_the_snapshot_is_refused(fake: FakeProvider, enabled: None) -> None:
    charge = ready(fake, methods=False)
    assert confirm(charge).message == service.CHARGE_NOW_METHOD_NOT_STORED
    page = admin()[0].get(f"/one-off-charges/{charge.pk}/charge-now/").content.decode()
    assert escape(service.CHARGE_NOW_METHOD_NOT_STORED) in page


def test_above_the_hard_limit_is_refused_and_at_it_is_allowed(
    fake: FakeProvider, enabled: None
) -> None:
    at = ready(fake, "100.00")
    above = ready(fake, "100.01", operator=operator_of(at))
    with override_settings(PROVIDER_MAX_TXN_AMOUNT=D("100.00")):
        refused = confirm(above)
        assert not refused.ok and "Exceeds the confirmed provider limit" in refused.message
        assert confirm(at).ok
    assert Job.objects.filter(kind=JobKind.INVOICE_PAY).count() == 1


def test_a_charge_whose_invoice_is_not_linked_yet_is_refused(
    fake: FakeProvider, enabled: None
) -> None:
    operator = an_operator(fake)
    store_methods(operator)
    charge = created(operator, item(CollectionMethod.CHARGE_NOW, "100.00"), actor=admin()[1])
    assert confirm(charge).message == service.CHARGE_NOW_NOT_LINKED


def test_a_charge_not_collected_by_charge_now_is_refused(fake: FakeProvider, enabled: None) -> None:
    operator = an_operator(fake)
    store_methods(operator)
    charge = created(operator, item(CollectionMethod.EMAIL, "100.00"))
    run_queued_jobs()
    assert confirm(charge).message == service.CHARGE_NOW_NOT_ELIGIBLE


def test_a_paid_invoice_at_the_provider_is_refused_by_the_worker_before_paying(
    fake: FakeProvider, enabled: None
) -> None:
    charge = ready(fake)
    ref = provider_ref(invoice_of(charge))
    fake.invoices[ref] = replace(fake.invoices[ref], raw_status="PAID")
    assert confirm(charge).ok  # the page cannot know; the worker re-fetches
    [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.FAILED, one_off_invoices.CHARGE_PAID_AT_PROVIDER)
    assert pays(fake) == []
    assert calls(fake, "get_invoice") == [(ref,)]
    assert invoice_of(charge).provider_status == "PAID"
    assert not Payment.objects.filter(one_off_charge=charge).exists()
    assert actions(charge)[-1] == service.CHARGE_FAILED_ACTION


def test_a_pending_or_settled_payment_on_the_charge_refuses(
    fake: FakeProvider, enabled: None
) -> None:
    charge = ready(fake)
    Payment.objects.create(
        one_off_charge=charge,
        invoice=invoice_of(charge),
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("10.00"),
        status=PaymentStatus.PENDING,
        initiated_on=timezone.localdate(),
    )
    assert confirm(charge).message == service.CHARGE_NOW_HAS_PAYMENT
    assert not Job.objects.filter(kind=JobKind.INVOICE_PAY).exists()


def test_a_waived_charge_is_refused(fake: FakeProvider, enabled: None) -> None:
    charge = ready(fake)
    service.waive(charge.pk, note="Goodwill.", actor=None)
    assert confirm(charge).message == service.CHARGE_NOW_CLOSED


# --- Failures and concurrency ---------------------------------------------------------------


def test_a_503_is_one_call_shown_once_and_the_button_returns_after_a_minute(
    fake: FakeProvider, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    charge = ready(fake)
    fake.fail_next("pay_invoice", ProviderUnavailable(503))
    confirm(charge)
    key = fresh(charge).charge_now_key
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    assert job.result.startswith(one_off_invoices.CHARGE_FAILED)
    assert "ProviderUnavailable" in job.result
    assert len(pays(fake)) == 1
    charge = fresh(charge)
    assert (charge.status, charge.charge_now_key) == (OneOffChargeStatus.OWED, key)
    assert not Payment.objects.filter(one_off_charge=charge).exists()
    failed = AuditLog.objects.filter(action=service.CHARGE_FAILED_ACTION).get()
    assert failed.after is not None and failed.after["failure"] == "ProviderUnavailable"
    client, _ = admin()
    page = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert "ProviderUnavailable" in page
    assert 'id="charge-now"' not in page and 'id="charge-now-wait"' in page
    waited = confirm(charge)
    assert not waited.ok and "possible again in" in waited.message
    later = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("django.utils.timezone.now", lambda: later)
    assert service.charge_now_wait(fresh(charge)) == 0
    page = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert 'id="charge-now"' in page
    assert len(pays(fake)) == 1  # nothing was repeated by itself


def test_a_rejected_pay_names_the_class_and_fields_only(fake: FakeProvider, enabled: None) -> None:
    charge = ready(fake)
    fake.fail_next("pay_invoice", ProviderRejected(422, error_fields=("payment_method_id",)))
    confirm(charge)
    [job] = run_queued_jobs()
    assert "ProviderRejected (fields: payment_method_id)" in job.result
    failed = AuditLog.objects.filter(action=service.CHARGE_FAILED_ACTION).get()
    assert failed.after == {
        "failure": "ProviderRejected",
        "fields": "payment_method_id",
        "provider_invoice": failed.after["provider_invoice"],
    }
    assert "…" in failed.after["provider_invoice"]


def test_two_confirms_queue_one_job_and_make_one_call(fake: FakeProvider, enabled: None) -> None:
    charge = ready(fake)
    first, second = confirm(charge), confirm(charge)
    assert first.ok and (second.ok, second.message) == (False, service.CHARGE_NOW_BUSY)
    assert Job.objects.filter(kind=JobKind.INVOICE_PAY).count() == 1
    run_queued_jobs()
    assert len(pays(fake)) == 1
    # A replayed job finds the pending payment and calls nothing.
    job = Job.objects.get(kind=JobKind.INVOICE_PAY)
    replayed = jobs.run(job)
    assert replayed.status == JobStatus.FAILED
    assert replayed.result == service.CHARGE_NOW_HAS_PAYMENT
    assert len(pays(fake)) == 1


def test_cancel_is_refused_while_the_charged_payment_is_pending(
    fake: FakeProvider, enabled: None
) -> None:
    charge = ready(fake)
    confirm(charge)
    run_queued_jobs()
    refused = service.cancel_charge(charge.pk, note="Mistake.", actor=None)
    assert (refused.ok, refused.message) == (False, service.CANCEL_PENDING)
    assert invoice_of(charge).internal_status != InvoiceStatus.VOID


def test_cancel_after_a_failed_charge_now_is_allowed(fake: FakeProvider, enabled: None) -> None:
    charge = ready(fake)
    fake.fail_next("pay_invoice", ProviderUnavailable(503))
    confirm(charge)
    run_queued_jobs()
    outcome = service.cancel_charge(charge.pk, note="Collected another way.", actor=None)
    assert outcome.ok
    run_queued_jobs()
    assert fresh(charge).status == OneOffChargeStatus.VOIDED
