"""Adversarial cases for PLAN-012b-b Batch A (rent run catch-up, the lease-end draft's
lifecycle, the final rent charge made by the wizards, the unpaid deposit), by the QA owner
(plan §6 A). The catch-up never creates a charge for another lease's month or past the end;
two runs at the same moment create each charge once; an adopted draft is bound to its own
store; a viewer sees the banner without the Cancel link; the final charge never exceeds the
prorated amount whatever the deductions post; the deposit blocker is not cleared by a part
payment."""

from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection, connections
from django.urls import reverse

from accounts.roles import Role
from ledger.models import (
    Lease,
    LeaseEnd,
    LeaseEndReason,
    LeaseTransfer,
    PaymentChannel,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import lease_end, rent_run, transfer
from ledger.services.lease_end import DeductionInput
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_lease_end import TODAY, deposit, fresh
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="row locks: PostgreSQL only"
)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    return TODAY


def _wire_lease(started: date, *, rent: str = "1000.00", debit_day: int = 1) -> Lease:
    """A lease built directly (no service, so no first charge on save)."""
    return a_lease(
        a_store(),
        an_operator(),
        started=started,
        channel=PaymentChannel.WIRE,
        debit_day=debit_day,
        rent=rent,
    )


def _months(lease: Lease) -> list[tuple[date, int, Decimal, date]]:
    return [
        (row.period_month, row.seq, row.amount_due, row.due_on)
        for row in RentCharge.objects.filter(lease=lease).order_by("period_month", "seq")
    ]


# --- the catch-up stays in its lane ----------------------------------------------------------


def test_the_catch_up_never_creates_a_charge_for_another_lease_or_past_the_end() -> None:
    ended = _wire_lease(date(2026, 8, 13))
    Lease.objects.filter(pk=ended.pk).update(ended_on=date(2026, 9, 30))
    ended.refresh_from_db()
    fresh_start = _wire_lease(date(2026, 10, 13), rent="2000.00")
    future = _wire_lease(date(2026, 11, 2))
    report = rent_run.create_charges(TODAY, actor=None)
    assert len(report.caught_up) == 3 and report.created == ()
    assert _months(ended) == [
        (date(2026, 8, 1), 1, D("612.90"), date(2026, 8, 13)),  # 13–31 Aug, 19 of 31
        (date(2026, 9, 1), 1, D("1000.00"), date(2026, 9, 1)),
    ]
    assert _months(fresh_start) == [(date(2026, 10, 1), 1, D("1225.81"), date(2026, 10, 13))]
    assert _months(future) == []
    # Nothing twice, nothing new.
    again = rent_run.create_charges(TODAY, actor=None)
    assert again.caught_up == () and again.created == ()
    assert RentCharge.objects.count() == 3
    assert all(c.status == RentChargeStatus.DUE for c in RentCharge.objects.all())


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_runs_at_the_same_moment_create_each_catch_up_charge_once() -> None:
    lease = _wire_lease(date(2026, 9, 13))
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            barrier.wait(timeout=10)
            rent_run.create_charges(TODAY, actor=None)
        except BaseException as error:
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert [(m, s) for m, s, _, _ in _months(lease)] == [
        (date(2026, 9, 1), 1),
        (date(2026, 10, 1), 1),
    ]


# --- the draft belongs to its store ---------------------------------------------------------------


def _draft_at_terms(lease: Lease) -> LeaseEnd:
    admin = client_for(Role.ADMIN)[1]
    end = lease_end.start(lease, admin)
    outcome = lease_end.set_terms(
        end, TODAY, LeaseEndReason.MOVED_OUT, True, actor=admin, today=TODAY
    )
    assert outcome.ok, outcome.message
    return LeaseEnd.objects.get(pk=end.pk)


def test_an_adopted_draft_is_bound_to_its_own_store_and_blocks_no_transfer() -> None:
    admin = client_for(Role.ADMIN)[1]
    lease_s, lease_t = _wire_lease(date(2026, 9, 1)), _wire_lease(date(2026, 9, 1))
    draft = _draft_at_terms(lease_s)
    assert draft.statement_opened_at is None
    # Store T's transfer never touches S's draft.
    started_t = transfer.start(lease_t.store, admin, today=TODAY)
    assert started_t.lease_end is not None and started_t.lease_end.pk != draft.pk
    assert fresh(draft).reason == LeaseEndReason.MOVED_OUT
    # Store S's transfer adopts it: same row, reason transfer, terms kept, one transfer.
    started_s = transfer.start(lease_s.store, admin, today=TODAY)
    assert started_s.lease_end is not None and started_s.lease_end.pk == draft.pk
    adopted = fresh(draft)
    assert adopted.reason == LeaseEndReason.TRANSFER and adopted.ended_on == TODAY
    assert LeaseEnd.objects.filter(lease=lease_s).count() == 1
    assert LeaseTransfer.objects.filter(store=lease_s.store).count() == 1
    assert transfer.start(lease_s.store, admin, today=TODAY).pk == started_s.pk
    # The plain wizard now defers to the transfer.
    client, _ = client_for(Role.ADMIN)
    response = client.get(reverse("ledger:lease_ends:terms", kwargs={"pk": draft.pk}))
    assert response.status_code == 302 and "/transfer" in response["Location"]


def test_a_viewer_sees_the_banner_without_the_cancel_link() -> None:
    admin = client_for(Role.ADMIN)[1]
    lease = _wire_lease(date(2026, 9, 1))
    draft = _draft_at_terms(lease)
    store_page = reverse("ledger:stores:detail", kwargs={"pk": lease.store_id})
    cancel = reverse("ledger:lease_ends:cancel", kwargs={"pk": draft.pk})
    statement = reverse("ledger:lease_ends:statement", kwargs={"pk": draft.pk})
    viewer, _ = client_for(Role.VIEWER)
    # Not yet at the statement: nobody sees a banner.
    assert "in progress" not in viewer.get(store_page).content.decode()
    assert lease_end.save_statement(fresh(draft), [], actor=admin).ok
    assert fresh(draft).statement_opened_at is not None
    page = viewer.get(store_page).content.decode()
    assert "in progress" in page and cancel not in page
    admin_client, _ = client_for(Role.ADMIN)
    admin_page = admin_client.get(store_page).content.decode()
    assert "in progress" in admin_page and cancel in admin_page and statement in admin_page
    assert viewer.post(cancel, {}).status_code == 403
    assert LeaseEnd.objects.filter(pk=draft.pk).exists()


# --- the final charge and the deposit -------------------------------------------------------------


def test_the_final_charge_never_exceeds_the_prorated_amount_whatever_the_deductions_post() -> None:
    admin = client_for(Role.ADMIN)[1]
    # Started and ended on the same day (the owner's Test Store Three): 1 of 31 days.
    lease = _wire_lease(TODAY)
    deposit(lease, "500.00")
    draft = _draft_at_terms(lease)
    assert lease_end.save_statement(
        fresh(draft),
        [
            DeductionInput("damage", "Door", D("99999.00")),
            DeductionInput("other", "Keys", D("40.00")),
        ],
        actor=admin,
    ).ok
    [(month, seq, amount, due)] = _months(lease)
    assert (month, seq, amount, due) == (TODAY.replace(day=1), 1, D("32.26"), TODAY)
    # Again, with other deductions: the same single charge, the same amount.
    assert lease_end.save_statement(
        fresh(draft), [DeductionInput("cleaning", "Floors", D("1.00"))], actor=admin
    ).ok
    assert _months(lease) == [(TODAY.replace(day=1), 1, D("32.26"), TODAY)]
    charge = RentCharge.objects.get(lease=lease)
    assert charge.proration_text and "1 of 31" in charge.proration_text
    assert not any(
        blocker.text.startswith("Rent") and "not charged yet" in blocker.text
        for blocker in lease_end.blockers(lease, TODAY)
    )


def test_the_deposit_blocker_is_not_cleared_by_a_part_payment() -> None:
    lease = _wire_lease(date(2026, 9, 1))
    charge = deposit(lease, "500.00", paid="0.00")
    kinds = [b.kind for b in lease_end.blockers(lease, TODAY)]
    assert kinds.count(lease_end.DEPOSIT_UNPAID) == 1
    [blocker] = [b for b in lease_end.blockers(lease, TODAY) if b.kind == lease_end.DEPOSIT_UNPAID]
    assert "$500.00 not collected" in blocker.text
    assert blocker.url == reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk})
    from ledger.models import PaymentMethod
    from ledger.services import one_off_charges

    assert one_off_charges.record_payment(
        charge.pk,
        method=PaymentMethod.WIRE,
        amount=D("200.00"),
        paid_on=TODAY,
        note="Wire received.",
        actor=None,
        today=TODAY,
    ).ok
    [blocker] = [b for b in lease_end.blockers(lease, TODAY) if b.kind == lease_end.DEPOSIT_UNPAID]
    assert "$300.00 not collected" in blocker.text
    assert one_off_charges.record_payment(
        charge.pk,
        method=PaymentMethod.WIRE,
        amount=D("300.00"),
        paid_on=TODAY,
        note="Wire received.",
        actor=None,
        today=TODAY,
    ).ok
    assert not [b for b in lease_end.blockers(lease, TODAY) if b.kind == lease_end.DEPOSIT_UNPAID]
