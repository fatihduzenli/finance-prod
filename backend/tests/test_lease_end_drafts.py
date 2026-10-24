"""A lease end left at the blockers step, the final rent charge made by the wizards and an
unpaid deposit at lease end (docs/qa/PLAN-012b-b-catch-up-rent-lines-wizard-fixes.md D9 to
D11; §6 "A", the implementer's cases; PLAN-012f-a D): showing the statement step writes
nothing and lists the rent charges it needs as preview lines with the same totals; saving
the step creates them, saves the deductions and marks the draft in progress; only then the
store page shows the banner with Continue and Cancel and the transfer start page the two
links; the daily job deletes term-less drafts after seven days; the end month's charge is
a real open line with its link once saved, never listed as "not charged yet"; an unpaid
deposit blocks and the statement says what was charged and collected. The adversarial
cases are the QA owner's.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.apps import apps
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    DeductionKind,
    Lease,
    LeaseEnd,
    LeaseEndDeduction,
    LeaseEndReason,
    LeaseTransfer,
    OneOffChargeStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import lease_end, one_off_charges, rent_run, statements, transfer
from ledger.services.lease_end import DeductionInput
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


def save(end: LeaseEnd, *lines: DeductionInput) -> lease_end.Outcome:
    """The statement step saved ("Save and continue") with ``lines``."""
    return lease_end.save_statement(fresh(end), list(lines), actor=None, today=TODAY)


def row_counts() -> dict[str, int]:
    """The number of rows of every table (the many-to-many ones included)."""
    return {
        model._meta.label: model._base_manager.count()
        for model in apps.get_models(include_auto_created=True)
        if model._meta.managed and not model._meta.proxy
    }


def the_figures(found: statements.Final) -> tuple[Any, ...]:
    """What the statement shows, the charges' ids aside."""
    return (
        [(line.description, line.due_on, line.balance) for line in found.open_lines],
        found.open_total,
        found.offset_total,
        found.offset_applied,
        found.withheld,
        found.deposit_refund,
        found.refund_due,
        found.debt,
        found.still_open,
        found.amount_owed,
    )


# --- D10: the final rent charge -----------------------------------------------------------


def test_saving_the_statement_creates_the_end_months_prorated_charge_once() -> None:
    lease = an_october_lease()
    deposit(lease, "2000.00")
    end = draft(lease)
    assert not RentCharge.objects.exists()
    assert save(end).ok
    made = RentCharge.objects.get(lease=lease)
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
    saved = AuditLog.objects.get(action="lease_end.statement_saved")
    assert saved.after is not None and saved.after["charges"] == [str(made.pk)]
    # Saved again: nothing new is created and the marker and its audit row stay.
    assert save(end).ok
    assert RentCharge.objects.filter(lease=lease).count() == 1
    assert fresh(end).statement_opened_at == opened
    assert AuditLog.objects.filter(action="lease_end.statement_saved").count() == 1
    assert AuditLog.objects.filter(action="rent_charge.created").count() == 1
    assert AuditLog.objects.filter(action="lease_end.deductions_set").count() == 2
    # The statement lists the charge as open, with its page, offset by the deposit.
    found = figures(end)
    [line] = found.open_lines
    assert (line.charge_id, line.balance) == (made.pk, PRORATED)
    assert line.url == reverse("ledger:charges:detail", kwargs={"pk": made.pk})
    [offset] = lease_end.default_deductions(fresh(end), today=TODAY)
    assert offset.amount == PRORATED


def test_the_statement_page_previews_the_charge_and_its_save_creates_and_links_it() -> None:
    lease = an_october_lease()
    end = draft(lease)
    client, _ = client_for(ADMIN)
    page = client.get(statement_url(end)).content.decode()
    assert not RentCharge.objects.exists()
    assert f"Rent Oct 2026: {TEXT}" in page and "not created yet" in page
    assert fresh(end).statement_opened_at is None
    response = client.post(statement_url(end), {})
    assert response["Location"] == reverse("ledger:lease_ends:settlement", kwargs={"pk": end.pk})
    charge = RentCharge.objects.get(lease=lease)
    assert fresh(end).statement_opened_at is not None
    page = client.get(statement_url(end)).content.decode()
    assert reverse("ledger:charges:detail", kwargs={"pk": charge.pk}) in page
    assert "not created yet" not in page.split('id="open-lines"')[1].split("</table>")[0]


@pytest.mark.parametrize("method", ["get", "head"])
def test_showing_the_statement_step_writes_no_row(method: str) -> None:
    # A missing earlier month and the end month's prorated charge: both would be created
    # by the save, none by the page.
    lease = a_store_lease(started_on=date(2026, 8, 13))
    deposit(lease, "2000.00")
    end = draft(lease)
    client, _ = client_for(ADMIN)
    before = row_counts()
    audits = list(AuditLog.objects.values_list("pk", flat=True))
    state = LeaseEnd.objects.filter(pk=end.pk).values().get()
    response = getattr(client, method)(statement_url(end))
    assert response.status_code == 200
    assert row_counts() == before
    assert list(AuditLog.objects.values_list("pk", flat=True)) == audits
    assert LeaseEnd.objects.filter(pk=end.pk).values().get() == state
    assert not RentCharge.objects.filter(lease=lease).exists()


def test_the_preview_reads_the_same_totals_as_the_saved_statement() -> None:
    lease = a_store_lease(started_on=date(2026, 8, 13))
    deposit(lease, "5000.00")
    end = draft(lease)
    shown = statements.final(fresh(end), today=TODAY)
    # August (prorated from the start), September, and October to the end date.
    assert [(line.charge_id, line.description) for line in shown.open_lines] == [
        (None, "Rent Aug 2026: 13–31 Aug, 19 of 31 days of $2,000.00 = $1,225.81"),
        (None, "Rent Sep 2026"),
        (None, f"Rent Oct 2026: {TEXT}"),
    ]
    assert all(line.preview and line.url == "" for line in shown.open_lines)
    assert shown.open_total == Decimal("4451.62")
    lines = [
        DeductionInput(DeductionKind.UNPAID_BALANCE, "", Decimal("4451.62")),
        DeductionInput(DeductionKind.CLEANING, "Cleaning", Decimal("150.00")),
    ]
    LeaseEndDeduction.objects.bulk_create(
        LeaseEndDeduction(
            lease_end=end,
            kind=item.kind,
            description=item.description,
            amount=item.amount,
            position=position,
        )
        for position, item in enumerate(lines, start=1)
    )
    previewed = statements.final(fresh(end), today=TODAY)
    LeaseEndDeduction.objects.filter(lease_end=end).delete()
    assert save(end, *lines).ok
    saved = statements.final(fresh(end), today=TODAY)
    assert the_figures(saved) == the_figures(previewed)
    assert not any(line.preview for line in saved.open_lines)
    assert saved.offset_applied == Decimal("4451.62") and saved.still_open == 0


def test_the_save_refuses_and_writes_nothing_when_a_blocker_appears_after_the_page() -> None:
    lease = an_october_lease()
    end = draft(lease)
    client, _ = client_for(ADMIN)
    assert client.get(statement_url(end)).status_code == 200
    deposit(lease, "500.00", paid="0.00")
    before = row_counts()
    audits = AuditLog.objects.count()
    response = client.post(
        statement_url(end), {"kind_1": "cleaning", "description_1": "", "amount_1": "50.00"}
    )
    assert response["Location"] == reverse("ledger:lease_ends:blockers", kwargs={"pk": end.pk})
    refused = save(end, DeductionInput(DeductionKind.CLEANING, "", Decimal("50.00")))
    assert not refused.ok
    assert refused.message == lease_end.BLOCKED.format(count=1)
    assert row_counts() == before and AuditLog.objects.count() == audits
    assert fresh(end).statement_opened_at is None


def test_an_offset_above_the_open_balance_refuses_the_save_and_creates_no_charge() -> None:
    lease = a_store_lease(started_on=date(2026, 8, 13))
    deposit(lease, "9000.00")
    end = draft(lease)
    before = row_counts()
    audits = AuditLog.objects.count()
    refused = save(end, DeductionInput(DeductionKind.UNPAID_BALANCE, "", Decimal("4451.63")))
    assert not refused.ok
    assert refused.message == lease_end.OFFSET_ABOVE_OPEN.format(open="$4,451.62")
    assert row_counts() == before and AuditLog.objects.count() == audits
    assert fresh(end).statement_opened_at is None
    assert save(end, DeductionInput(DeductionKind.UNPAID_BALANCE, "", Decimal("4451.62"))).ok
    assert RentCharge.objects.filter(lease=lease).count() == 3


def test_the_save_refuses_a_draft_without_terms_and_an_ended_lease() -> None:
    bare = lease_end.start(an_october_lease(), None)
    assert save(bare).message == lease_end.TERMS_REQUIRED
    lease = an_october_lease()
    end = draft(lease)
    Lease.objects.filter(pk=lease.pk).update(ended_on=ENDED)
    assert save(end).message == lease_end.ALREADY_ENDED
    assert not RentCharge.objects.exists()
    assert not AuditLog.objects.filter(action="lease_end.statement_saved").exists()


def a_lease_with_two_missing_months() -> Lease:
    """Started 13 Aug with no charge: August (prorated) and September are missing and the
    end month's prorated charge is not created yet."""
    lease = a_store_lease(started_on=date(2026, 8, 13))
    deposit(lease, "5000.00")
    return lease


def test_confirm_without_a_save_creates_what_the_save_would_and_the_same_figures() -> None:
    saved_end = draft(a_lease_with_two_missing_months())
    assert save(saved_end).ok
    assert settle(fresh(saved_end)).ok
    assert confirm(fresh(saved_end)).ok
    lease = a_lease_with_two_missing_months()
    end = draft(lease)
    assert settle(end).ok
    before = AuditLog.objects.filter(action="rent_charge.created").count()
    assert confirm(fresh(end)).ok
    assert [
        (row.period_month, row.amount_due, row.due_on)
        for row in RentCharge.objects.filter(lease=lease).order_by("period_month")
    ] == [
        (date(2026, 8, 1), Decimal("1225.81"), date(2026, 8, 13)),
        (date(2026, 9, 1), Decimal("2000.00"), date(2026, 9, 1)),
        (OCTOBER, PRORATED, OCTOBER),
    ]
    created = AuditLog.objects.filter(action="rent_charge.created").order_by("id")[before:]
    assert [bool(row.after and row.after.get("catch_up")) for row in created] == [
        True,
        True,
        False,
    ]
    kept = statements.from_json(fresh(end).statement_json)
    # Confirm creates every charge before its figures: the kept statement has real ones only.
    assert [line.charge_id for line in kept.open_lines if line.charge_id is None] == []
    assert the_figures(kept) == the_figures(statements.from_json(fresh(saved_end).statement_json))
    assert kept.open_total == Decimal("4451.62") and kept.amount_owed == Decimal("4451.62")


def test_confirm_after_a_save_creates_no_charge() -> None:
    lease = a_lease_with_two_missing_months()
    end = draft(lease)
    assert save(end).ok
    assert settle(fresh(end)).ok
    charges = set(RentCharge.objects.filter(lease=lease).values_list("pk", flat=True))
    created = AuditLog.objects.filter(action="rent_charge.created").count()
    assert confirm(fresh(end)).ok
    assert set(RentCharge.objects.filter(lease=lease).values_list("pk", flat=True)) == charges
    assert AuditLog.objects.filter(action="rent_charge.created").count() == created
    kept = statements.from_json(fresh(end).statement_json)
    assert {line.charge_id for line in kept.open_lines} == charges


def test_a_transfer_confirm_without_a_save_creates_the_missing_months_too() -> None:
    from tests.test_transfer import begin, incoming
    from tests.test_transfer import confirm as confirm_transfer

    _, user = client_for(ADMIN)
    lease = a_lease_with_two_missing_months()
    row = begin(lease, user)
    end = fresh(row).lease_end
    # The months not created yet block the transfer as owed (PLAN-012f-a D) until the
    # offset covers them; the deductions alone are saved, no charge is created.
    offset = DeductionInput(DeductionKind.UNPAID_BALANCE, "", Decimal("4451.62"))
    assert lease_end.set_deductions(end, [offset], actor=user, today=TODAY).ok
    assert not RentCharge.objects.filter(lease=lease).exists()
    assert settle(fresh(end), actor=user).ok
    outcome = incoming(row, user)
    assert outcome.ok, outcome.message
    done = confirm_transfer(row, user)
    assert done.ok, done.message
    assert [
        row.period_month for row in RentCharge.objects.filter(lease=lease).order_by("period_month")
    ] == [date(2026, 8, 1), date(2026, 9, 1), OCTOBER]
    kept = statements.from_json(fresh(end).statement_json)
    assert [line.charge_id for line in kept.open_lines if line.charge_id is None] == []
    assert kept.open_total == Decimal("4451.62") and kept.offset_applied == Decimal("4451.62")
    assert all(
        charge.status == RentChargeStatus.PAID for charge in RentCharge.objects.filter(lease=lease)
    )


def test_the_catch_up_plan_is_what_the_catch_up_creates() -> None:
    lease = a_store_lease(started_on=date(2026, 6, 10), second_debit_day=15)
    # July's two debits exist; June and August are the two gaps.
    for seq, debit_day in ((1, 1), (2, 15)):
        RentCharge.objects.create(
            lease=lease,
            period_month=date(2026, 7, 1),
            seq=seq,
            due_on=date(2026, 7, debit_day),
            amount_due=Decimal("1000.00"),
            status=RentChargeStatus.DUE,
        )
    day = date(2026, 8, 31)
    planned = rent_run.months_catch_up_would_create(lease, day)
    assert not RentCharge.objects.filter(period_month__in=[date(2026, 6, 1), date(2026, 8, 1)])
    assert [(plan.period_month, plan.seq) for plan in planned] == [
        (date(2026, 6, 1), 1),
        (date(2026, 6, 1), 2),
        (date(2026, 8, 1), 1),
        (date(2026, 8, 1), 2),
    ]
    made = rent_run.catch_up_lease(lease, day, None)
    assert [
        rent_run.Planned(
            row.period_month, row.seq, row.due_on, row.amount_due, row.proration_text or ""
        )
        for row in made
    ] == planned
    assert rent_run.months_catch_up_would_create(lease, day) == []


def test_saving_the_statement_catches_up_the_earlier_months() -> None:
    lease = a_store_lease(started_on=date(2026, 8, 13))
    end = draft(lease)
    assert save(end).ok
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
    assert not save(end).ok
    assert fresh(end).statement_opened_at is None
    bare = lease_end.start(an_october_lease(), None)
    assert not save(bare).ok
    assert not RentCharge.objects.exists()


def test_an_existing_charge_is_left_for_confirm_and_confirms_fallback_still_works() -> None:
    lease = an_october_lease()
    charge = october(lease)
    end = draft(lease)
    assert save(end).ok
    assert RentCharge.objects.filter(lease=lease).count() == 1
    assert fresh(charge).amount_due == charge.amount_due
    # Confirm without the statement ever saved still creates the month (its fallback).
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
    # The end month's charge not created yet is owed like an existing one (PLAN-012f-a D),
    # linked to the lease until it exists; the wording never says "not charged yet".
    [planned] = transfer.blockers(fresh(row), TODAY)
    assert "not charged yet" not in planned.text and "$1,225.81 is still owed" in planned.text
    assert planned.url == reverse("ledger:leases:detail", kwargs={"pk": lease.pk})
    assert save(end).ok
    [blocker] = transfer.blockers(fresh(row), TODAY)
    charge = RentCharge.objects.get(lease=lease)
    assert "not charged yet" not in blocker.text and "$1,225.81 is still owed" in blocker.text
    assert blocker.url == reverse("ledger:charges:detail", kwargs={"pk": charge.pk})


def test_months_not_created_yet_block_a_transfer_with_the_existing_charges_sentence() -> None:
    lease = a_lease_with_two_missing_months()
    row = transfer.start(lease.store, None, today=TODAY)
    end = fresh(row).lease_end
    assert lease_end.set_terms(end, ENDED, "transfer", True, actor=None, today=TODAY).ok
    planned = transfer.balance_blockers(fresh(row), TODAY)
    assert not RentCharge.objects.filter(lease=lease).exists()
    assert len(planned) == 3
    assert save(end).ok
    existing = transfer.balance_blockers(fresh(row), TODAY)
    # The same sentence (charge, amount, owed by the outgoing operator) before and after
    # the charges exist; only the link moves from the lease to the charge.
    assert [item.text for item in planned] == [item.text for item in existing]
    assert {item.kind for item in planned} == {transfer.OPEN_BALANCE}


def test_months_not_created_yet_stop_blocking_once_the_offset_covers_them() -> None:
    lease = a_lease_with_two_missing_months()
    row = transfer.start(lease.store, None, today=TODAY)
    end = fresh(row).lease_end
    assert lease_end.set_terms(end, ENDED, "transfer", True, actor=None, today=TODAY).ok
    partly = DeductionInput(DeductionKind.UNPAID_BALANCE, "", Decimal("4000.00"))
    assert lease_end.set_deductions(fresh(end), [partly], actor=None, today=TODAY).ok
    [left] = transfer.balance_blockers(fresh(row), TODAY)
    assert "$451.62 is still owed" in left.text
    whole = DeductionInput(DeductionKind.UNPAID_BALANCE, "", Decimal("4451.62"))
    assert lease_end.set_deductions(fresh(end), [whole], actor=None, today=TODAY).ok
    assert transfer.balance_blockers(fresh(row), TODAY) == []
    assert not RentCharge.objects.filter(lease=lease).exists()


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
    assert save(end).ok
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
    assert save(end).ok
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
    assert save(end).ok
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
    assert save(end).ok
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
    assert save(end).ok
    client, _ = client_for(ADMIN)
    page = client.get(statement_url(end)).content.decode()
    assert "Deposit charged $500.00, collected $200.00" in page
    # Kept in the confirmed figures and read back from them.
    assert settle(fresh(end)).ok
    assert confirm(fresh(end)).ok
    kept = statements.from_json(fresh(end).statement_json)
    assert kept.deposits[0].charged == line.charged
