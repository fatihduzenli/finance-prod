"""Adversarial cases for PLAN-012e D14 (debt after a transfer: block by default, admin
override with a reason), by the QA owner (plan §6). An override cannot be forged onto
another transfer or by a lower role; it never bypasses another blocker; it writes nothing
when the statement leaves no debt; its reason never reaches the operator's statement or
mail; and the lease-end balance stays on the attention list until it is paid."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client

from accounts.roles import Role
from ledger.models import AuditLog, Lease, LeaseTransfer, OneOffCharge, PaymentMethod
from ledger.services import attention, installments, one_off_charges, transfer
from tests import test_lease_end as lease_end_tests
from tests.job_helpers import run_queued_jobs
from tests.test_lease_end import (
    OCTOBER,
    TODAY,
    a_store_lease,
    deductions,
    deposit,
    fresh,
    october,
    pdf_text,
    settle,
)
from tests.test_operator_views import client_for
from tests.test_plan012e_c_adversarial import FIRST
from tests.test_transfer import begin, confirm, incoming, ready

pytestmark = pytest.mark.django_db

D = Decimal
MARKER = "MARKER-D14-REASON"
# The lease-end file's fixtures, re-exported under their own names so pytest finds them here.
storage = lease_end_tests.storage
pinned = lease_end_tests.pinned
memory = lease_end_tests.memory


def _admin() -> tuple[Any, User]:
    return client_for(Role.ADMIN)


def _with_debt(user: User, amount: str = "2500.00") -> LeaseTransfer:
    """A transfer whose statement charges a lease-end balance: the $2,000 deposit held
    against ``amount`` of damage, everything else clean and the incoming lease set."""
    # Started in the end month: since PLAN-012f-a D a planned (not yet created) month blocks
    # the transfer like an existing one.
    lease = a_store_lease(started_on=OCTOBER)
    october(lease, paid="2000.00")
    deposit(lease, "2000.00")
    row = begin(lease, user)
    end = fresh(row).lease_end
    assert deductions(end, ("damage", "Door and frame", amount)).ok
    # The prorated last month leaves a credit to refund; the debt is charged besides.
    assert settle(fresh(end), actor=user).ok
    outcome = incoming(row, user)
    assert outcome.ok, outcome.message
    return LeaseTransfer.objects.get(pk=row.pk)


def _override_fields(row: LeaseTransfer) -> tuple[object, ...]:
    row = fresh(row)
    return (
        row.debt_override_amount,
        row.debt_override_reason,
        row.debt_override_by_id,
        row.debt_override_at,
    )


def _path(row: LeaseTransfer) -> str:
    return f"/transfers/{row.pk}/allow-balance/"


# --- forged target or role ----------------------------------------------------------------------


def test_an_override_cannot_be_forged_onto_another_transfer_or_by_a_lower_role(
    pinned: None,
) -> None:
    client, user = _admin()
    mine, theirs = _with_debt(user), _with_debt(user)
    untouched = _override_fields(theirs)
    for role in (Role.VIEWER, Role.BILLING):
        lower, _ = client_for(role)
        assert lower.post(_path(mine), {"reason": "Owner agreed."}).status_code == 403
        assert lower.get(_path(mine)).status_code == 403
    assert Client().post(_path(mine), {"reason": "Owner agreed."}).status_code == 401
    assert _override_fields(mine) == (None, "", None, None)
    # An admin's POST to one transfer with another's id in the body touches only its own.
    response = client.post(
        _path(mine),
        {
            "reason": "Owner agreed to carry the balance.",
            "transfer": str(theirs.pk),
            "pk": str(theirs.pk),
            "debt_override_amount": "0.01",
            "debt_override_by": "1",
        },
    )
    assert response.status_code == 302
    assert _override_fields(theirs) == untouched
    amount, reason, by, at = _override_fields(mine)
    assert amount == D("500.00") and reason == "Owner agreed to carry the balance."
    assert by == user.pk and at is not None
    # The GET of the route is not a page.
    assert client.get(_path(mine)).status_code == 405


# --- an override is not a pass ------------------------------------------------------------------


def test_an_override_never_bypasses_another_blocker(pinned: None) -> None:
    _, user = _admin()
    row = _with_debt(user)
    assert transfer.override_debt(row, "Owner agreed.", user).ok
    before = _override_fields(row)
    # A new blocker after the override: a live installment plan on a charge of the lease.
    lease: Lease = fresh(row).outgoing_lease
    assert lease.operator is not None
    charge = OneOffCharge.objects.create(
        operator=lease.operator,
        lease=lease,
        category="damage",
        collection="manual",
        description="Signage removal",
        price=D("300.00"),
        due_date=TODAY,
        status="owed",
    )
    lines, refused = installments.preview(charge, 2, FIRST, "monthly", today=TODAY)
    assert refused is None, refused
    assert installments.create_plan(charge, 2, FIRST, "monthly", lines, user, today=TODAY).ok
    audits = AuditLog.objects.count()
    outcome = confirm(row, user)
    assert not outcome.ok and outcome.message.startswith("The store cannot be transferred yet")
    assert fresh(lease).ended_on is None
    assert fresh(row).status != "confirmed"
    assert _override_fields(row) == before
    assert AuditLog.objects.count() == audits


def test_an_override_on_a_transfer_without_debt_is_refused_and_writes_nothing(
    pinned: None,
) -> None:
    client, user = _admin()
    row = ready(user)
    audits = AuditLog.objects.count()
    outcome = transfer.override_debt(row, "Just in case.", user)
    assert not outcome.ok
    assert _override_fields(row) == (None, "", None, None)
    assert AuditLog.objects.count() == audits
    response = client.post(_path(row), {"reason": "Just in case."})
    assert response.status_code == 302
    assert _override_fields(row) == (None, "", None, None)
    assert AuditLog.objects.count() == audits
    # Without debt the confirm needs no override.
    assert confirm(row, user).ok


def test_the_debt_cannot_grow_behind_an_override(pinned: None) -> None:
    _, user = _admin()
    row = _with_debt(user)
    assert transfer.override_debt(row, "Owner agreed to $500.", user).ok
    end = fresh(row).lease_end
    assert deductions(end, ("damage", "Door and frame", "2500.00"), ("other", "Keys", "40.00")).ok
    outcome = confirm(row, user)
    assert not outcome.ok and "grew" in outcome.message
    assert fresh(fresh(row).outgoing_lease).ended_on is None
    assert OneOffCharge.objects.filter(description="Lease-end balance").count() == 0


# --- the reason stays internal ------------------------------------------------------------------


def test_the_override_reason_never_reaches_the_operators_statement_or_mail(
    pinned: None, storage: Any, memory: Any
) -> None:
    _, user = _admin()
    row = _with_debt(user)
    assert transfer.override_debt(row, f"Owner agreed; {MARKER}.", user).ok
    outcome = confirm(row, user)
    assert outcome.ok, outcome.message
    run_queued_jobs()
    run_queued_jobs()
    end = fresh(fresh(row).lease_end)
    assert end.debt_charge is not None and end.debt_charge.price == D("500.00")
    assert end.debt_charge.operator_id == fresh(row).outgoing_lease.operator_id
    [blob] = storage.blobs.values()
    data = blob if isinstance(blob, bytes) else next(p for p in blob if isinstance(p, bytes))
    assert MARKER not in pdf_text(data)
    assert memory.sent, "nothing was sent"
    for message in memory.sent:
        assert MARKER not in message.subject and MARKER not in message.text
        assert MARKER not in message.html
        for attachment in message.attachments:
            assert MARKER not in pdf_text(attachment.data)
    # The audit keeps it.
    assert AuditLog.objects.filter(action="lease_transfer.debt_overridden").exists()
    dump = " ".join(str(r.after) for r in AuditLog.objects.filter(entity_type="lease_transfer"))
    assert MARKER in dump


# --- the balance stays on the list until paid -----------------------------------------------------


def test_the_lease_end_balance_stays_on_the_attention_list_until_paid(pinned: None) -> None:
    _, user = _admin()
    row = _with_debt(user)
    assert transfer.override_debt(row, "Owner agreed.", user).ok
    assert confirm(row, user).ok
    end = fresh(fresh(row).lease_end)
    assert end.debt_charge is not None
    charge = end.debt_charge

    def items() -> list[attention.Item]:
        return [
            i for i in attention.attention(TODAY).items if i.kind == attention.LEASE_END_BALANCE
        ]

    found = items()
    assert len(found) == 1
    assert f"/one-off-charges/{charge.pk}/" == found[0].url
    assert "allowed by" in found[0].text and "500.00" in found[0].text
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get("/").content.decode()
    assert "Lease-end balance" in page and "allowed by" in page
    # Partly paid: still listed; paid in full: gone.
    assert one_off_charges.record_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=D("200.00"),
        paid_on=TODAY,
        note="Paid by Zelle",
        actor=None,
        today=TODAY,
    ).ok
    assert len(items()) == 1 and "300.00" in items()[0].text
    assert one_off_charges.record_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=D("300.00"),
        paid_on=TODAY,
        note="Paid by Zelle",
        actor=None,
        today=TODAY,
    ).ok
    assert items() == []
    # PLAN-012f: the dashboard's Recent payments card may name the paid charge; the attention
    # card must not. The item itself is gone (asserted above).
    home = viewer.get("/").content.decode()
    start = home.index('id="attention')
    attention_card = home[start : home.index("</section>", start)]
    assert "Lease-end balance" not in attention_card
