"""Waive and a live provider invoice (docs/qa/PLAN-011-dashboard-reports-mobile.md D0, D10).

Waive is refused while the one-off charge has a current provider invoice that is not
void here and not last seen ``DELETED`` there; it still waives a charge with no provider
invoice, or whose invoice reads deleted or void. The remedy, "Delete provider invoice"
(admin), deletes a waived charge's live invoice through the worker, marks the local
invoice void and leaves the charge waived; the dashboard lists such charges until none
remain. The provider is a :class:`~payments.fake.FakeProvider`; nothing reaches a network.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.db.models import Q
from django.test import override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    Invoice,
    InvoiceStatus,
    OneOffCharge,
    OneOffChargeStatus,
)
from ledger.services import one_off_charges as service
from payments import invoice_sync, one_off_invoices
from payments.errors import ProviderUnavailable
from payments.fake import FakeProvider
from payments.models import Job, JobKind, JobStatus
from tests.job_helpers import run_queued_jobs
from tests.test_one_off_charges import (
    an_operator,
    calls,
    created,
    fresh,
    invoice_of,
    item,
    provider_ref,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

NOTE = "Agreed with the operator."


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


def live_charge(fake: FakeProvider) -> OneOffCharge:
    """An e-mailed charge whose invoice the worker created and sent at the provider."""
    charge = created(an_operator(fake), item())
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE, job.result
    assert invoice_of(charge).provider_status == "SENT"
    return charge


def waived_before_the_rule(charge: OneOffCharge) -> OneOffCharge:
    """The state of ``INV-2026-0004``: waived while its invoice was live (before D0)."""
    OneOffCharge.objects.filter(pk=charge.pk).update(
        status=OneOffChargeStatus.WAIVED, waived_at=timezone.now(), waive_note="Old waive."
    )
    return fresh(charge)


def actions(charge: OneOffCharge) -> list[str]:
    return list(
        AuditLog.objects.filter(entity_type="one_off_charge", entity_id=str(charge.pk))
        .order_by("id")
        .values_list("action", flat=True)
    )


# --- The refusal -------------------------------------------------------------------------


def test_the_d0_sentence_is_the_owners() -> None:
    assert service.WAIVE_LIVE_INVOICE == (
        "This charge has a live Stax invoice. Cancel it first (Stax deletes the invoice), "
        "then waive if anything is still owed."
    )
    assert service.CANCEL_MEANING == (
        "removes the Stax invoice and closes the charge, nothing owed"
    )
    assert service.WAIVE_MEANING == (
        "closes a charge that has no Stax invoice without collecting it"
    )


def test_waive_is_refused_while_the_provider_invoice_is_live(
    fake: FakeProvider, worker: None
) -> None:
    charge = live_charge(fake)
    outcome = service.waive(charge.pk, note=NOTE, actor=None)
    assert (outcome.ok, outcome.message) == (False, service.WAIVE_LIVE_INVOICE)
    assert fresh(charge).status == OneOffChargeStatus.OWED
    assert "one_off_charge.waived" not in actions(charge)
    assert calls(fake, "delete_invoice") == []


def test_a_draft_invoice_already_at_the_provider_counts_as_live(fake: FakeProvider) -> None:
    """A provider reference makes the invoice live whatever its status here (manual
    collection: created at the provider, never sent)."""
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    with override_settings(STAX_CALLER="worker"):
        run_queued_jobs()
    assert provider_ref(invoice_of(charge))
    assert service.waive(charge.pk, note=NOTE, actor=None).message == service.WAIVE_LIVE_INVOICE


def test_waive_still_works_without_a_provider_invoice(fake: FakeProvider) -> None:
    """Manual collection for an operator without a provider customer: a local invoice
    and no provider reference."""
    charge = created(an_operator(fake, customer=None), item(CollectionMethod.MANUAL))
    assert invoice_of(charge).internal_status == InvoiceStatus.DRAFT
    outcome = service.waive(charge.pk, note=NOTE, actor=None)
    assert outcome.ok, outcome.message
    assert fresh(charge).status == OneOffChargeStatus.WAIVED


def test_waive_still_works_for_a_setup_charge() -> None:
    from tests.test_setup_charges import a_charge

    charge = a_charge()
    assert service.waive(charge.pk, note=NOTE, actor=None).ok


@pytest.mark.parametrize("shown", ["DELETED", " deleted "])
def test_waive_works_once_the_invoice_reads_deleted_after_a_cancel(
    fake: FakeProvider, worker: None, shown: str
) -> None:
    charge = live_charge(fake)
    invoice_sync.record_provider_status(invoice_of(charge).pk, shown)
    outcome = service.waive(charge.pk, note=NOTE, actor=None)
    assert outcome.ok, outcome.message


def test_waive_works_once_the_local_invoice_is_void(fake: FakeProvider, worker: None) -> None:
    charge = live_charge(fake)
    Invoice.objects.filter(one_off_charge=charge).update(internal_status=InvoiceStatus.VOID)
    assert service.waive(charge.pk, note=NOTE, actor=None).ok


def test_cancel_then_nothing_is_left_to_waive(fake: FakeProvider, worker: None) -> None:
    """The order the sentence gives: Cancel deletes the invoice and closes the charge."""
    charge = live_charge(fake)
    assert service.cancel_charge(charge.pk, note="Duplicate.", actor=None).ok
    run_queued_jobs()
    assert fresh(charge).status == OneOffChargeStatus.VOIDED
    assert service.waive(charge.pk, note=NOTE, actor=None).message == service.WAIVE_VOIDED


def test_the_charge_page_explains_both_actions_and_the_refusal(
    fake: FakeProvider, worker: None
) -> None:
    charge = live_charge(fake)
    client, _ = client_for(Role.ADMIN)
    page = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert f"<strong>Cancel</strong> {service.CANCEL_MEANING}." in page
    assert f"<strong>Waive</strong> {service.WAIVE_MEANING}." in page
    assert 'id="waive-refusal"' in page and "This charge has a live Stax invoice." in page
    waive_page = client.get(f"/one-off-charges/{charge.pk}/waive/").content.decode()
    assert "This charge has a live Stax invoice." in waive_page


def test_a_posted_waive_is_refused_with_the_sentence(fake: FakeProvider, worker: None) -> None:
    charge = live_charge(fake)
    client, _ = client_for(Role.ADMIN)
    response = client.post(f"/one-off-charges/{charge.pk}/waive/", {"note": NOTE}, follow=True)
    assert "This charge has a live Stax invoice. Cancel it first" in response.content.decode()
    assert fresh(charge).status == OneOffChargeStatus.OWED


# --- The remedy --------------------------------------------------------------------------


def test_the_remedy_deletes_the_invoice_through_the_worker_and_keeps_the_waive(
    fake: FakeProvider, worker: None
) -> None:
    charge = waived_before_the_rule(live_charge(fake))
    ref = provider_ref(invoice_of(charge))
    assert [row.pk for row in service.waived_with_live_invoice()] == [charge.pk]
    client, admin = client_for(Role.ADMIN)
    page = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert 'id="delete-invoice"' in page
    home = client.get("/").content.decode()
    assert "Waived with a live Stax invoice: 1" in home and f"/one-off-charges/{charge.pk}/" in home

    response = client.post(f"/one-off-charges/{charge.pk}/delete-invoice/")
    assert response.status_code == 302
    job = Job.objects.get(kind=JobKind.INVOICE_DELETE)
    assert (job.status, job.params, job.requested_by_id) == (
        JobStatus.QUEUED,
        {service.WAIVED_INVOICE_PARAM: True},
        admin.pk,
    )
    assert calls(fake, "delete_invoice") == []  # the view never calls the provider

    [done] = run_queued_jobs()
    assert (done.status, done.result) == (JobStatus.DONE, service.WAIVED_INVOICE_DELETED)
    assert calls(fake, "delete_invoice") == [(ref,)]
    invoice = invoice_of(charge)
    assert (invoice.internal_status, invoice.provider_status) == (InvoiceStatus.VOID, "DELETED")
    after = fresh(charge)
    assert (after.status, after.waive_note) == (OneOffChargeStatus.WAIVED, "Old waive.")
    assert actions(charge)[-2:] == [
        service.WAIVED_INVOICE_DELETE_ACTION,
        service.WAIVED_INVOICE_DELETED_ACTION,
    ]
    deleted = AuditLog.objects.get(action=service.WAIVED_INVOICE_DELETED_ACTION)
    assert deleted.actor_id == admin.pk
    assert deleted.after["invoice_number"] == invoice.invoice_number
    assert ref not in str(deleted.after)  # masked
    assert service.waived_with_live_invoice() == []
    assert "Waived with a live" not in client.get("/").content.decode()
    assert (
        'id="delete-invoice"' not in client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    )
    # Waive is allowed (and refused only as "already waived") now nothing is live.
    assert service.waive(charge.pk, note=NOTE, actor=None).message == service.WAIVE_ALREADY


def test_the_remedy_is_refused_while_a_job_is_active(fake: FakeProvider, worker: None) -> None:
    charge = waived_before_the_rule(live_charge(fake))
    first = service.delete_waived_invoice(charge.pk, actor=None)
    assert first.ok
    second = service.delete_waived_invoice(charge.pk, actor=None)
    assert (second.ok, second.message) == (False, service.CANCEL_BUSY)
    assert Job.objects.filter(kind=JobKind.INVOICE_DELETE).count() == 1


def test_the_remedy_is_refused_for_an_open_charge_or_one_without_a_live_invoice(
    fake: FakeProvider, worker: None
) -> None:
    owed = live_charge(fake)
    outcome = service.delete_waived_invoice(owed.pk, actor=None)
    assert (outcome.ok, outcome.message) == (False, service.DELETE_INVOICE_NOT_WAIVED)
    plain = created(an_operator(fake, customer=None), item(CollectionMethod.MANUAL))
    assert service.waive(plain.pk, note=NOTE, actor=None).ok
    outcome = service.delete_waived_invoice(plain.pk, actor=None)
    assert (outcome.ok, outcome.message) == (False, service.DELETE_INVOICE_NONE)
    assert not Job.objects.filter(kind=JobKind.INVOICE_DELETE).exists()


def test_a_failed_delete_leaves_the_invoice_live_and_the_remedy_offered(
    fake: FakeProvider, worker: None
) -> None:
    charge = waived_before_the_rule(live_charge(fake))
    fake.fail_next("delete_invoice", ProviderUnavailable(503, "unavailable"))
    assert service.delete_waived_invoice(charge.pk, actor=None).ok
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED and "ProviderUnavailable" in job.result
    assert invoice_of(charge).internal_status != InvoiceStatus.VOID
    assert [row.pk for row in service.waived_with_live_invoice()] == [charge.pk]
    assert service.retry_kind(fresh(charge)) is None  # no cancel retry on a waived charge
    client, _ = client_for(Role.ADMIN)
    assert 'id="delete-invoice"' in client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert service.delete_waived_invoice(charge.pk, actor=None).ok
    [again] = run_queued_jobs()
    assert again.status == JobStatus.DONE, again.result
    assert service.waived_with_live_invoice() == []


def test_an_invoice_the_provider_shows_paid_is_not_deleted(
    fake: FakeProvider, worker: None
) -> None:
    from dataclasses import replace

    charge = waived_before_the_rule(live_charge(fake))
    ref = provider_ref(invoice_of(charge))
    fake.invoices[ref] = replace(fake.invoices[ref], raw_status="PAID")
    assert service.delete_waived_invoice(charge.pk, actor=None).ok
    [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.FAILED, one_off_invoices.DELETE_PAID)
    assert calls(fake, "delete_invoice") == []


def test_an_invoice_the_provider_no_longer_has_counts_as_deleted(
    fake: FakeProvider, worker: None
) -> None:
    charge = waived_before_the_rule(live_charge(fake))
    del fake.invoices[provider_ref(invoice_of(charge))]
    assert service.delete_waived_invoice(charge.pk, actor=None).ok
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE, job.result
    assert invoice_of(charge).internal_status == InvoiceStatus.VOID
    assert fresh(charge).status == OneOffChargeStatus.WAIVED


def test_a_job_whose_charge_was_cleared_meanwhile_sends_nothing(
    fake: FakeProvider, worker: None
) -> None:
    charge = waived_before_the_rule(live_charge(fake))
    assert service.delete_waived_invoice(charge.pk, actor=None).ok
    Invoice.objects.filter(one_off_charge=charge).update(internal_status=InvoiceStatus.VOID)
    [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.DONE, service.DELETE_INVOICE_NONE)
    assert calls(fake, "delete_invoice") == []


def test_the_cancel_job_without_the_flag_is_unchanged(fake: FakeProvider, worker: None) -> None:
    """``invoice_delete`` without the waived flag is still the cancel (PLAN-010d D6)."""
    charge = waived_before_the_rule(live_charge(fake))
    from payments import jobs

    jobs.enqueue(JobKind.INVOICE_DELETE, charge, None, None)
    [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.FAILED, service.CANCEL_WAIVED)
    assert calls(fake, "delete_invoice") == []


# --- Roles (D10) -------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_only_an_admin_may_delete_the_invoice(fake: FakeProvider, worker: None, role: Role) -> None:
    charge = waived_before_the_rule(live_charge(fake))
    client, _ = client_for(role)
    assert client.post(f"/one-off-charges/{charge.pk}/delete-invoice/").status_code == 403
    assert not Job.objects.filter(Q(kind=JobKind.INVOICE_DELETE)).exists()
    page = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert 'id="delete-invoice"' not in page


def test_the_remedy_route_takes_post_only(fake: FakeProvider, worker: None) -> None:
    charge = waived_before_the_rule(live_charge(fake))
    client, _ = client_for(Role.ADMIN)
    assert client.get(f"/one-off-charges/{charge.pk}/delete-invoice/").status_code == 405
    assert client.post("/one-off-charges/not-a-uuid/delete-invoice/").status_code == 404
