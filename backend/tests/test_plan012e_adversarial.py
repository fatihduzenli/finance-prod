"""Adversarial cases for PLAN-012e (Step 12b), by the QA owner (plan §6). Batch A: the
end-of-lease wizard must act on its own lease only, never refund more than the deposit held,
keep account numbers out of every line and out of the PDF, mail nothing to a real address
outside production, and stay closed to anyone below admin."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Document,
    DocumentKind,
    Lease,
    LeaseEnd,
    LeaseEndDeduction,
    Payment,
    PaymentKind,
    RentCharge,
)
from ledger.services import lease_end, statements
from ledger.services.lease_end import DeductionInput
from tests import test_lease_end as lease_end_tests
from tests.job_helpers import run_queued_jobs
from tests.test_lease_end import (
    CATCH_ALL,
    ENDED,
    TODAY,
    a_store_lease,
    confirm,
    deductions,
    deposit,
    draft,
    figures,
    fresh,
    october,
    pdf_text,
    settle,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
CARD = "4111 1111 1111 1111"
PAIR = "Routing 123456780 Account 987654321012"
# The lease-end file's fixtures, re-exported under their own names so pytest finds them here.
storage = lease_end_tests.storage
pinned = lease_end_tests.pinned
memory = lease_end_tests.memory


def _admin() -> tuple[Any, User]:
    return client_for(Role.ADMIN)


def _terms(**overrides: str) -> dict[str, str]:
    data = {"ended_on": ENDED.isoformat(), "reason": "moved_out", "prorate_last_month": "on"}
    return {**data, **overrides}


# --- the wizard acts on its own lease only ----------------------------------------------------


def test_the_wizard_cannot_end_another_stores_lease_by_forged_fields(pinned: None) -> None:
    lease_a, lease_b = a_store_lease(), a_store_lease()
    october(lease_b)
    client, admin = _admin()
    response = client.post(f"/stores/{lease_a.store_id}/end-lease/", {"lease": str(lease_b.pk)})
    assert response.status_code in (302, 200)
    end = LeaseEnd.objects.get()
    assert end.lease_id == lease_a.pk
    for forged in (
        {"lease": str(lease_b.pk)},
        {"store": str(lease_b.store_id), "lease_id": str(lease_b.pk)},
    ):
        client.post(f"/lease-ends/{end.pk}/terms/", {**_terms(), **forged})
    assert fresh(end).lease_id == lease_a.pk
    assert fresh(end).ended_on == ENDED
    outcome = confirm(fresh(end), admin)
    assert outcome.ok, outcome.message
    assert fresh(lease_a).ended_on == ENDED
    assert fresh(lease_b).ended_on is None and fresh(lease_b).collection_status == "active"
    assert RentCharge.objects.get(lease=lease_b).amount_due == D("2000.00")
    assert (
        not AuditLog.objects.filter(entity_type="lease", entity_id=str(lease_b.pk))
        .exclude(action="lease.created")
        .exists()
    )


# --- the settlement never refunds more than the deposit held ------------------------------------


@pytest.mark.parametrize(
    ("held", "deduction", "expected_refund"),
    [
        ("2000.00", "0.00", "2000.00"),
        ("2000.00", "500.00", "1500.00"),
        ("300.00", "0.00", "300.00"),
    ],
)
def test_the_refund_is_the_deposit_less_deductions_whatever_the_form_posts(
    pinned: None, held: str, deduction: str, expected_refund: str
) -> None:
    lease = a_store_lease()
    deposit(lease, held)
    end = draft(lease)
    if D(deduction):
        assert deductions(end, ("damage", "Broken door", deduction)).ok
    client, admin = _admin()
    client.post(
        f"/lease-ends/{end.pk}/settlement/",
        {
            "method": "zelle",
            "on": TODAY.isoformat(),
            "reference": "ZL-1",
            "note": "",
            "amount": "9999.00",
            "refund_due": "9999.00",
            "refund_amount": "9999.00",
        },
    )
    assert figures(fresh(end)).refund_due == D(expected_refund)
    assert settle(fresh(end)).ok
    outcome = confirm(fresh(end), admin)
    assert outcome.ok, outcome.message
    refunds = Payment.objects.filter(kind=PaymentKind.REFUND_DEPOSIT)
    assert sum((p.amount for p in refunds), D("0")) == D(expected_refund)
    assert all(p.amount > 0 for p in refunds)


def test_a_deduction_above_the_deposit_and_balance_owes_and_refunds_nothing(pinned: None) -> None:
    # Started in the end month: since PLAN-012f-a D the statement previews (and the save
    # creates) every missing earlier month, which would add to the amount owed.
    lease = a_store_lease(started_on=lease_end_tests.OCTOBER)
    deposit(lease, "500.00")
    end = draft(lease)
    assert deductions(end, ("damage", "Floor", "1200.00")).ok
    final = figures(fresh(end))
    # Owed: the deductions beyond the deposit (700.00) plus the prorated final month the
    # wizard creates when no October charge exists yet (1,225.81).
    assert final.refund_due == 0 and final.amount_owed == D("700.00") + D("1225.81")
    assert confirm(fresh(end), _admin()[1]).ok
    assert not Payment.objects.filter(kind=PaymentKind.REFUND_DEPOSIT).exists()
    assert fresh(end).debt_charge is not None and fresh(end).debt_charge.price == D("700.00")


# --- account numbers never reach a line, a reference or the PDF -------------------------------


@pytest.mark.parametrize("text", [CARD, PAIR, f"Door {CARD.replace(' ', '')}"])
def test_a_deduction_or_a_reference_holding_payment_data_is_refused(
    pinned: None, text: str
) -> None:
    lease = a_store_lease()
    deposit(lease)
    end = draft(lease)
    refused = lease_end.set_deductions(
        end, [DeductionInput("other", text, D("10.00"))], actor=None, today=TODAY
    )
    assert not refused.ok
    assert LeaseEndDeduction.objects.count() == 0
    refused = settle(fresh(end), reference=text)
    assert not refused.ok
    refused = settle(fresh(end), note=text)
    assert not refused.ok
    dump = "".join(str(r.after) + str(r.before) for r in AuditLog.objects.all()).replace(" ", "")
    assert "4111111111111111" not in dump and "987654321012" not in dump


def test_the_stored_pdf_holds_no_long_digit_run_and_passes_the_guard(
    pinned: None, storage: Any
) -> None:
    lease = a_store_lease()
    october(lease, paid="2000.00")
    deposit(lease, "2000.00")
    end = draft(lease)
    assert deductions(end, ("cleaning", "Final clean", "150.00")).ok
    assert settle(fresh(end), reference="ZL-2026-10").ok
    assert confirm(fresh(end), _admin()[1]).ok
    run_queued_jobs()
    end = fresh(end)
    assert end.document is not None
    document = Document.objects.get(pk=end.document_id)
    assert document.kind == DocumentKind.FINAL_STATEMENT and document.store_id == lease.store_id
    [blob] = storage.blobs.values()
    data = blob if isinstance(blob, bytes) else next(p for p in blob if isinstance(p, bytes))
    text = pdf_text(data)
    assert "1,225.81" in text or "1225.81" in text
    assert not re.search(r"\d{9,}", text.replace(" ", "").replace("-", "")), text
    assert "ZL-2026-10" in text


# --- mail never leaves for a real address outside production ----------------------------------


def test_the_statement_mail_goes_to_the_catch_all_with_the_attachment(
    pinned: None, memory: Any
) -> None:
    from ledger.models import Operator

    lease = a_store_lease()
    assert lease.operator_id is not None
    Operator.objects.filter(pk=lease.operator_id).update(email="real.person@realcompany.example")
    deposit(lease, "100.00")
    end = draft(lease)
    assert settle(fresh(end)).ok
    assert confirm(fresh(end), _admin()[1]).ok
    run_queued_jobs()
    run_queued_jobs()
    assert memory.sent, "nothing was sent"
    for message in memory.sent:
        assert message.to == CATCH_ALL
        assert "real.person@realcompany.example" not in message.to
        assert message.attachments and message.attachments[0].content_type == "application/pdf"


# --- below admin, nothing ----------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_only_an_admin_starts_or_drives_the_wizard(pinned: None, role: Role) -> None:
    lease = a_store_lease()
    deposit(lease)
    end = draft(lease)
    client, _ = client_for(role)
    assert client.post(f"/stores/{lease.store_id}/end-lease/", {}).status_code == 403
    assert client.get(f"/stores/{lease.store_id}/end-lease/").status_code == 403
    for step, data in (
        ("terms", _terms()),
        ("blockers", {}),
        ("statement", {}),
        ("settlement", {"method": "zelle", "on": TODAY.isoformat(), "reference": "x", "note": ""}),
        ("confirm", {"confirmed": "on"}),
        ("cancel", {}),
    ):
        assert client.get(f"/lease-ends/{end.pk}/{step}/").status_code in (403, 405), step
        assert client.post(f"/lease-ends/{end.pk}/{step}/", data).status_code == 403, step
    assert client.get(f"/lease-ends/{end.pk}/statement.pdf").status_code == 403
    assert fresh(end).status == "draft" and fresh(lease).ended_on is None
    page = client.get(f"/stores/{lease.store_id}/").content.decode()
    assert "end-lease" not in page


def test_confirm_is_atomic_under_a_forced_failure(pinned: None, monkeypatch: Any) -> None:
    lease = a_store_lease()
    october(lease)
    deposit(lease, "2000.00")
    end = draft(lease)
    assert settle(fresh(end)).ok
    before = (
        fresh(lease).ended_on,
        list(RentCharge.objects.values_list("pk", "amount_due", "status")),
        Payment.objects.count(),
        LeaseEnd.objects.get(pk=end.pk).status,
    )

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("forced")

    monkeypatch.setattr(lease_end, "_debt_charge", boom)
    monkeypatch.setattr(
        statements, "final", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("forced"))
    )
    with pytest.raises(RuntimeError):
        lease_end.confirm(fresh(end), _admin()[1], confirmed=True, today=TODAY)
    after = (
        fresh(lease).ended_on,
        list(RentCharge.objects.values_list("pk", "amount_due", "status")),
        Payment.objects.count(),
        LeaseEnd.objects.get(pk=end.pk).status,
    )
    assert after == before
    assert fresh(lease).collection_status == "active"
    assert isinstance(ENDED, date)


# --- Batch B: the transfer wizard ----------------------------------------------------------------


def _transfer_imports() -> Any:
    from ledger.models import LeaseTransfer
    from ledger.services import transfer
    from tests import test_transfer as transfer_tests

    return LeaseTransfer, transfer, transfer_tests


def test_a_transfer_cannot_create_the_incoming_lease_without_ending_the_outgoing_one(
    pinned: None, monkeypatch: Any
) -> None:
    transfer_model, transfer, tt = _transfer_imports()
    client, admin = _admin()
    row = tt.ready(admin)
    outgoing = fresh(row).outgoing_lease
    before = (Lease.objects.count(), fresh(outgoing).ended_on, tt.snapshot())
    # The incoming lease's creation fails after the outgoing end has been applied.
    from ledger.services import leases as lease_service

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("forced after the end")

    monkeypatch.setattr(lease_service, "create_lease", boom)
    with pytest.raises(RuntimeError):
        transfer.confirm(fresh(row), admin, confirmed=True, today=TODAY)
    assert (Lease.objects.count(), fresh(outgoing).ended_on, tt.snapshot()) == before
    assert fresh(outgoing).collection_status == "active"
    assert transfer_model.objects.get(pk=row.pk).status == "draft"


def test_a_transfer_cannot_be_pointed_at_another_stores_lease_or_an_archived_operator(
    pinned: None,
) -> None:
    from django.utils import timezone

    from ledger.models import Operator

    transfer_model, transfer, tt = _transfer_imports()
    client, admin = _admin()
    row = tt.ready(admin)
    other = tt.a_clean_lease()
    archived = Operator.objects.create(display_name="Gone", archived_at=timezone.now())
    outgoing_operator_id = fresh(row).outgoing_lease.operator_id
    for forged in (
        {"operator": str(archived.pk)},
        {"operator": str(outgoing_operator_id)},
        {"operator": str(fresh(row).incoming_operator_id), "store": str(other.store_id)},
        {"operator": str(fresh(row).incoming_operator_id), "outgoing_lease": str(other.pk)},
    ):
        client.post(f"/transfers/{row.pk}/operator/", forged)
        client.post(
            f"/transfers/{row.pk}/incoming/",
            {
                "started_on": tt.STARTS.isoformat(),
                "rent": "2500.00",
                "debit_day": "1",
                "channel": "wire",
                "prorate_first_month": "on",
                "deposit_amount": "3000.00",
                **forged,
            },
        )
    fresh_row = transfer_model.objects.get(pk=row.pk)
    assert fresh_row.store_id == fresh(row).outgoing_lease.store_id
    assert fresh_row.outgoing_lease_id == fresh(row).outgoing_lease_id
    assert fresh_row.incoming_operator_id not in (archived.pk, outgoing_operator_id)
    assert fresh(other).ended_on is None
    outcome = transfer.confirm(fresh_row, admin, confirmed=True, today=TODAY)
    assert outcome.ok, outcome.message
    assert fresh(other).ended_on is None and fresh(other).collection_status == "active"
    incoming = Lease.objects.get(store=fresh_row.store, started_on=tt.STARTS)
    assert incoming.operator_id == fresh_row.incoming_operator_id


def test_nothing_reaches_the_provider_before_the_move_in_draft_is_sent(pinned: None) -> None:
    from payments.models import Job, JobKind

    transfer_model, transfer, tt = _transfer_imports()
    client, admin = _admin()
    row = tt.ready(admin)
    outcome = transfer.confirm(fresh(row), admin, confirmed=True, today=TODAY)
    assert outcome.ok, outcome.message
    run_queued_jobs()
    provider_kinds = {
        JobKind.INVOICE_CREATE,
        JobKind.INVOICE_SEND,
        JobKind.SCHEDULE_CREATE,
        JobKind.INVOICE_PAY,
    }
    assert not Job.objects.filter(kind__in=provider_kinds).exists()
    from ledger.models import InvoiceDraft, OneOffCharge

    draft_row = InvoiceDraft.objects.get(operator_id=fresh(row).incoming_operator_id)
    assert draft_row.status == "draft"
    assert not OneOffCharge.objects.filter(
        lease__store=fresh(row).store, lease__started_on=tt.STARTS
    ).exists()
