"""A lease end left at the blockers step, the final rent charge made by the wizards and an
unpaid deposit at lease end (docs/qa/PLAN-012b-b-catch-up-rent-lines-wizard-fixes.md D9 to
D11; §6 "A", the implementer's cases): the statement step marks the draft in progress and
creates the rent charges the statement needs; only then the store page shows the banner
with Continue and Cancel and the transfer start page the two links; the daily job deletes
term-less drafts after seven days; the end month's charge is a real open line with its
link, never listed as "not charged yet"; an unpaid deposit blocks and the statement says
what was charged and collected. The adversarial cases are the QA owner's.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Lease,
    LeaseEnd,
    LeaseEndReason,
    LeaseTransfer,
    OneOffChargeStatus,
    RentCharge,
)
from ledger.services import lease_end, one_off_charges, statements, transfer
from tests.test_lease_end import (
    ENDED,
    OCTOBER,
    PRORATED,
    TEXT,
    TODAY,
    a_store_lease,
    confirm,
    deposit,
    draft,
    figures,
    fresh,
    october,
    settle,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

ADMIN = Role.ADMIN


@pytest.fixture(autouse=True)
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)


def an_october_lease(**fields: Any) -> Lease:
    return a_store_lease(started_on=OCTOBER, **fields)


def statement_url(end: LeaseEnd) -> str:
    return reverse("ledger:lease_ends:statement", kwargs={"pk": end.pk})


def store_url(end: LeaseEnd) -> str:
    return reverse("ledger:stores:detail", kwargs={"pk": end.lease.store_id})


# --- D10: the final rent charge -----------------------------------------------------------


def test_opening_the_statement_creates_the_end_months_prorated_charge_once() -> None:
    lease = an_october_lease()
    deposit(lease, "2000.00")
    end = draft(lease)
    assert not RentCharge.objects.exists()
    [made] = lease_end.open_statement(end, None, today=TODAY)
    assert (made.period_month, made.seq, made.amount_due, made.due_on, made.proration_text) == (
        OCTOBER,
        1,
        PRORATED,
        OCTOBER,
        TEXT,
    )
    audit = AuditLog.objects.get(action="rent_charge.created")
    assert audit.after is not None and audit.after["lease_end"] == str(end.pk)
    opened = fresh(end).statement_opened_at
    assert opened is not None
    assert lease_end.open_statement(fresh(end), None, today=TODAY) == []
    assert fresh(end).statement_opened_at == opened
    assert AuditLog.objects.filter(action="lease_end.statement_opened").count() == 1
    # The statement lists the charge as open, with its page, offset by the deposit.
    found = figures(end)
    [line] = found.open_lines
    assert (line.charge_id, line.balance) == (made.pk, PRORATED)
    assert line.url == reverse("ledger:charges:detail", kwargs={"pk": made.pk})
    [offset] = lease_end.default_deductions(fresh(end), today=TODAY)
    assert offset.amount == PRORATED


def test_the_statement_page_creates_the_charge_and_links_it() -> None:
    lease = an_october_lease()
    end = draft(lease)
    client, _ = client_for(ADMIN)
    page = client.get(statement_url(end)).content.decode()
    charge = RentCharge.objects.get(lease=lease)
    assert reverse("ledger:charges:detail", kwargs={"pk": charge.pk}) in page
    assert fresh(end).statement_opened_at is not None


def test_opening_the_statement_catches_up_the_earlier_months() -> None:
    lease = a_store_lease(started_on=date(2026, 8, 13))
    end = draft(lease)
    lease_end.open_statement(end, None, today=TODAY)
    assert [
        (row.period_month, row.amount_due, row.due_on)
        for row in RentCharge.objects.filter(lease=lease).order_by("period_month")
    ] == [
        # 13–31 Aug, 19 of 31 days of $2,000.00; due on the start date.
        (date(2026, 8, 1), Decimal("1225.81"), date(2026, 8, 13)),
        (date(2026, 9, 1), Decimal("2000.00"), date(2026, 9, 1)),
        (OCTOBER, PRORATED, OCTOBER),
    ]
    audits = AuditLog.objects.filter(action="rent_charge.created").order_by("id")
    assert [row.after["catch_up"] for row in audits[:2] if row.after] == [True, True]


def test_nothing_is_created_while_a_blocker_is_listed_or_before_the_terms() -> None:
    lease = an_october_lease()
    deposit(lease, "500.00", paid="0.00")
    end = draft(lease)
    assert lease_end.open_statement(end, None, today=TODAY) == []
    assert fresh(end).statement_opened_at is None
    bare = lease_end.start(an_october_lease(), None)
    assert lease_end.open_statement(bare, None, today=TODAY) == []
    assert not RentCharge.objects.exists()


def test_an_existing_charge_is_left_for_confirm_and_confirms_fallback_still_works() -> None:
    lease = an_october_lease()
    charge = october(lease)
    end = draft(lease)
    assert lease_end.open_statement(end, None, today=TODAY) == []
    assert fresh(charge).amount_due == charge.amount_due
    # Confirm without the statement ever opened still creates the month (its fallback).
    other = an_october_lease()
    other_end = draft(other)
    assert confirm(other_end).ok
    assert RentCharge.objects.get(lease=other).amount_due == PRORATED


def test_the_transfers_blockers_never_say_not_charged_yet() -> None:
    lease = an_october_lease()
    deposit(lease, "2000.00")
    row = transfer.start(lease.store, None, today=TODAY)
    end = fresh(row).lease_end
    assert lease_end.set_terms(end, ENDED, "transfer", True, actor=None, today=TODAY).ok
    assert transfer.blockers(fresh(row), TODAY) == []
    lease_end.open_statement(fresh(end), None, today=TODAY)
    [blocker] = transfer.blockers(fresh(row), TODAY)
    charge = RentCharge.objects.get(lease=lease)
    assert "not charged yet" not in blocker.text and "$1,225.81 is still owed" in blocker.text
    assert blocker.url == reverse("ledger:charges:detail", kwargs={"pk": charge.pk})


# --- D9: the draft's lifecycle -----------------------------------------------------------


def test_a_draft_at_the_blockers_step_shows_no_banner_and_a_transfer_adopts_it() -> None:
    lease = an_october_lease()
    end = draft(lease)
    client, _ = client_for(ADMIN)
    assert 'id="lease-end-in-progress"' not in client.get(store_url(end)).content.decode()
    response = client.post(reverse("ledger:stores:transfer", kwargs={"pk": lease.store_id}))
    row = LeaseTransfer.objects.get(store=lease.store)
    assert response["Location"] == reverse("ledger:transfers:operator", kwargs={"pk": row.pk})
    assert row.lease_end_id == end.pk and fresh(end).reason == LeaseEndReason.TRANSFER
    assert fresh(end).ended_on == ENDED


def test_a_draft_at_the_statement_shows_the_banner_and_cancel_from_it_deletes_it() -> None:
    lease = an_october_lease()
    end = draft(lease)
    lease_end.open_statement(end, None, today=TODAY)
    client, _ = client_for(ADMIN)
    page = client.get(store_url(end)).content.decode()
    assert "A lease end is in progress (statement step)." in page
    assert statement_url(end) in page
    cancel = reverse("ledger:lease_ends:cancel", kwargs={"pk": end.pk})
    assert f'action="{cancel}"' in page
    response = client.post(cancel)
    assert response["Location"] == store_url(end)
    assert not LeaseEnd.objects.filter(pk=end.pk).exists()
    assert 'id="lease-end-in-progress"' not in client.get(store_url(end)).content.decode()


def test_the_transfer_start_page_shows_continue_and_cancel_for_a_draft_in_progress() -> None:
    lease = an_october_lease()
    end = draft(lease)
    client, _ = client_for(ADMIN)
    start = reverse("ledger:stores:transfer", kwargs={"pk": lease.store_id})
    assert 'id="start-transfer"' in client.get(start).content.decode()
    lease_end.open_statement(end, None, today=TODAY)
    page = client.get(start).content.decode()
    assert 'id="start-transfer"' not in page
    assert statement_url(end) in page
    assert reverse("ledger:lease_ends:cancel", kwargs={"pk": end.pk}) in page
    with pytest.raises(ValueError, match="lease end is in progress"):
        transfer.start(lease.store, None, today=TODAY)


def test_a_transfers_draft_in_progress_shows_the_banner_with_the_transfers_cancel() -> None:
    lease = an_october_lease()
    deposit(lease, "2000.00")
    row = transfer.start(lease.store, None, today=TODAY)
    end = fresh(row).lease_end
    assert lease_end.set_terms(end, ENDED, "transfer", True, actor=None, today=TODAY).ok
    lease_end.open_statement(fresh(end), None, today=TODAY)
    client, _ = client_for(ADMIN)
    page = client.get(store_url(end)).content.decode()
    assert "A transfer is in progress (statement step)." in page
    assert reverse("ledger:transfers:cancel", kwargs={"pk": row.pk}) in page


def test_the_daily_job_deletes_drafts_without_terms_after_seven_days() -> None:
    old = lease_end.start(an_october_lease(), None)
    recent = lease_end.start(an_october_lease(), None)
    with_terms = draft(an_october_lease())
    held = transfer.start(an_october_lease().store, None, today=TODAY)
    week_ago = timezone.now() - timedelta(days=lease_end.STALE_DRAFT_DAYS, minutes=1)
    LeaseEnd.objects.exclude(pk=recent.pk).update(created_at=week_ago)
    out = StringIO()
    call_command("collections_tick", stdout=out)
    assert "Lease-end drafts without terms removed: 1." in out.getvalue()
    assert set(LeaseEnd.objects.values_list("pk", flat=True)) == {
        recent.pk,
        with_terms.pk,
        fresh(held).lease_end_id,
    }
    audit = AuditLog.objects.get(action="lease_end.swept")
    assert audit.entity_id == str(old.pk) and audit.actor is None
    assert lease_end.sweep_drafts() == 0


# --- D11: an unpaid deposit -------------------------------------------------------------


def test_an_unpaid_deposit_blocks_until_collected_or_waived() -> None:
    lease = an_october_lease()
    charge = deposit(lease, "500.00", paid="0.00")
    [blocker] = lease_end.blockers(lease, TODAY)
    assert blocker.kind == lease_end.DEPOSIT_UNPAID
    assert blocker.text == (
        f"{charge.label} $500.00 not collected: collect it, or waive or cancel the charge."
    )
    assert blocker.url == reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk})
    outcome = one_off_charges.waive(charge.pk, note="Deposit not required.", actor=None)
    assert outcome.ok, outcome.message
    assert fresh(charge).status == OneOffChargeStatus.WAIVED
    assert lease_end.blockers(lease, TODAY) == []
    assert AuditLog.objects.filter(action="one_off_charge.waived").count() == 1


def test_the_statement_says_what_the_deposit_charged_and_collected() -> None:
    lease = an_october_lease()
    held = deposit(lease, "500.00")
    end = draft(lease)
    [line] = figures(end).deposits
    assert (line.charged, line.held, line.text) == (
        held.price,
        held.price,
        "Deposit held $500.00",
    )
    lease_end.open_statement(end, None, today=TODAY)
    client, _ = client_for(ADMIN)
    assert "Deposit charged" not in client.get(statement_url(end)).content.decode()


def test_a_deposit_charged_above_what_was_collected_reads_charged_and_collected() -> None:
    lease = an_october_lease()
    charge = deposit(lease, "500.00", paid="200.00")
    # Collected in part and the rest written off (the waive action refuses a charge with
    # a payment, so the row is set directly): no blocker is left.
    type(charge).objects.filter(pk=charge.pk).update(
        status=OneOffChargeStatus.WAIVED, waived_at=timezone.now(), waive_note="Written off."
    )
    end = draft(lease)
    assert lease_end.blockers(lease, TODAY) == []
    found = figures(end)
    [line] = found.deposits
    assert line.text == "Deposit charged $500.00, collected $200.00"
    assert found.deposit_held == line.held
    rows = statements._lines(found)
    assert any("Deposit charged $500.00, collected $200.00" in label for _, label, _ in rows)
    lease_end.open_statement(end, None, today=TODAY)
    client, _ = client_for(ADMIN)
    page = client.get(statement_url(end)).content.decode()
    assert "Deposit charged $500.00, collected $200.00" in page
    # Kept in the confirmed figures and read back from them.
    assert settle(fresh(end)).ok
    assert confirm(fresh(end)).ok
    kept = statements.from_json(fresh(end).statement_json)
    assert kept.deposits[0].charged == line.charged
