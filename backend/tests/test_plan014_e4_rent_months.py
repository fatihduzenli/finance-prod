"""PLAN-014 E4 (Fable): rules A and B for rent keyed to the lease and the month, the owner's
decision of 2026-10-30 — one A per lease per month when the job runs twice; no A when autopay
is paused (B instead) or the lease ends before the debit date; no A for a month paid in advance
through a rent charge that already exists; B for rent without autopay three days before its due
date, once; the rent charge created on its day then takes C while the month's row stays one; a
lease debited twice a month gets one A; the row carries the lease, the month and no charge; the
send re-checks and suppresses; the Outbox and the timeline link the store, the charge log names
the month; the 0062 constraint on PostgreSQL and sqlite."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from accounts.roles import Role
from ledger.models import (
    ChargeEntityType,
    CollectionSchedule,
    Lease,
    LeaseBilling,
    MessageStatus,
    OutboundMessage,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import messaging, reminders
from payments.models import Job, JobKind
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_ledger_models import new_schedule, saved
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 20)  # a Tuesday; the debit day 23 is three days ahead
MONTH = date(2026, 10, 1)


def billed_lease(number: int, *, debit_day: int = 23, email: str = "store@example.com") -> Lease:
    lease = a_lease(
        a_store(str(number), number=number),
        an_operator(f"Operator {number}"),
        started=date(2026, 1, 1),
        debit_day=debit_day,
        rent="3500.00",
    )
    LeaseBilling.objects.update_or_create(
        lease=lease,
        defaults={"email": email, "company_name": f"Store {number} LLC", "bank_last4": "4821"},
    )
    return lease


def autopay(lease: Lease, state: str = ScheduleState.ACTIVE, amount: str = "3500.00") -> Any:
    found: CollectionSchedule = saved(
        new_schedule(
            lease=lease,
            kind=ScheduleKind.RENT,
            seq=1,
            debit_day=lease.debit_day,
            state=state,
            amount=Decimal(amount),
        )
    )
    return found


def month_rows(lease: Lease) -> list[tuple[str, date | None, str]]:
    return list(
        OutboundMessage.objects.filter(lease=lease)
        .order_by("queued_at", "id")
        .values_list("rule", "period_month", "charge_entity_type")
    )


# --- The owner's four cases ---------------------------------------------------------------------


def test_one_a_per_lease_per_month_even_when_the_job_runs_twice() -> None:
    lease = billed_lease(2001)
    autopay(lease)
    first = reminders.queue_due(TODAY)
    second = reminders.queue_due(TODAY)
    assert month_rows(lease) == [("A", MONTH, ChargeEntityType.LEASE_MONTH)]
    assert first.queued_by_rule["A"] == 1 and second.queued == 0
    assert Job.objects.filter(kind=JobKind.MESSAGE_SEND).count() == 1
    message = OutboundMessage.objects.get(lease=lease)
    assert message.charge_entity_id == lease.pk
    assert "$3,500.00" in message.rendered_body and "4821" in message.rendered_body
    assert "Oct 23, 2026" in message.rendered_body


@pytest.mark.parametrize("state", [ScheduleState.PAUSED, ScheduleState.FAILED, ScheduleState.ENDED])
def test_no_a_when_autopay_is_not_active_b_goes_instead(state: str) -> None:
    lease = billed_lease(2002)
    autopay(lease, state)
    reminders.queue_due(TODAY)
    assert month_rows(lease) == [("B", MONTH, ChargeEntityType.LEASE_MONTH)]


def test_no_a_when_the_lease_ends_before_the_debit_date() -> None:
    lease = billed_lease(2003)
    autopay(lease)
    Lease.objects.filter(pk=lease.pk).update(ended_on=TODAY + timedelta(days=1))
    summary = reminders.queue_due(TODAY)
    assert month_rows(lease) == []
    assert summary.queued == 0


def test_no_a_for_a_month_already_paid_in_advance() -> None:
    lease = billed_lease(2004)
    autopay(lease)
    # The invoice builder made October's rent charge early and it was paid.
    a_rent_charge(lease, MONTH, RentChargeStatus.PAID, "3500.00", "3500.00", day=23)
    reminders.queue_due(TODAY)
    assert month_rows(lease) == []


# --- B for rent without autopay; C, D and E on the charge once it exists ---------------------


def test_rent_without_autopay_gets_b_once_three_days_before_its_due_date() -> None:
    lease = billed_lease(2101)
    reminders.queue_due(TODAY)
    reminders.queue_due(TODAY)
    assert month_rows(lease) == [("B", MONTH, ChargeEntityType.LEASE_MONTH)]
    message = OutboundMessage.objects.get(lease=lease)
    assert "Rent October 2026" in message.rendered_body
    assert "Oct 23, 2026" in message.rendered_body


def test_the_rent_charge_created_on_its_day_takes_c_while_the_months_row_stays_one() -> None:
    lease = billed_lease(2102)
    reminders.queue_due(TODAY)  # B for October, keyed to the month
    charge = a_rent_charge(lease, MONTH, day=23)  # the rent run, on the 23rd
    reminders.queue_due(TODAY + timedelta(days=3))  # the debit day: nothing
    reminders.queue_due(TODAY + timedelta(days=8))  # +5: C on the charge
    rows = month_rows(lease)
    assert rows == [
        ("B", MONTH, ChargeEntityType.LEASE_MONTH),
        ("C", None, ChargeEntityType.RENT_CHARGE),
    ]
    assert OutboundMessage.objects.get(rule="C").charge_entity_id == charge.pk


def test_a_lease_debited_twice_a_month_gets_one_a_for_the_first_debit() -> None:
    lease = billed_lease(2103, debit_day=23)
    Lease.objects.filter(pk=lease.pk).update(second_debit_day=28)
    lease.refresh_from_db()
    autopay(lease)
    reminders.queue_due(TODAY)  # 20th: three days before the 23rd
    reminders.queue_due(TODAY + timedelta(days=5))  # 25th: three days before the 28th
    assert month_rows(lease) == [("A", MONTH, ChargeEntityType.LEASE_MONTH)]


def test_november_gets_its_own_a() -> None:
    lease = billed_lease(2104)
    autopay(lease)
    reminders.queue_due(TODAY)
    reminders.queue_due(date(2026, 11, 20))
    assert month_rows(lease) == [
        ("A", MONTH, ChargeEntityType.LEASE_MONTH),
        ("A", date(2026, 11, 1), ChargeEntityType.LEASE_MONTH),
    ]


# --- The send re-checks -------------------------------------------------------------------------


def test_a_queued_a_is_suppressed_when_the_schedule_stops_before_the_send() -> None:
    lease = billed_lease(2201)
    schedule = autopay(lease)
    reminders.queue_due(TODAY)
    CollectionSchedule.objects.filter(pk=schedule.pk).update(state=ScheduleState.PAUSED)
    message = OutboundMessage.objects.get(lease=lease)
    with override_settings(MAIL_ENABLED=False):
        messaging.send(message.pk)
    message.refresh_from_db()
    assert message.status == MessageStatus.SUPPRESSED
    assert message.error == "ScheduleStopped"


def test_a_queued_b_is_suppressed_when_the_month_is_settled_before_the_send() -> None:
    lease = billed_lease(2202)
    reminders.queue_due(TODAY)
    a_rent_charge(lease, MONTH, RentChargeStatus.PAID, "3500.00", "3500.00", day=23)
    message = OutboundMessage.objects.get(lease=lease)
    with override_settings(MAIL_ENABLED=False):
        messaging.send(message.pk)
    message.refresh_from_db()
    assert message.status == MessageStatus.SUPPRESSED
    assert message.error == "MonthSettled"


def test_a_queued_a_is_suppressed_when_the_lease_ends_before_the_send() -> None:
    lease = billed_lease(2203)
    autopay(lease)
    reminders.queue_due(TODAY)
    Lease.objects.filter(pk=lease.pk).update(ended_on=TODAY + timedelta(days=1))
    message = OutboundMessage.objects.get(lease=lease)
    with override_settings(MAIL_ENABLED=False):
        messaging.send(message.pk)
    message.refresh_from_db()
    assert message.status == MessageStatus.SUPPRESSED
    assert message.error == "LeaseEnded"


# --- The row and the pages ----------------------------------------------------------------------


def test_the_constraint_keeps_one_row_per_lease_rule_and_month() -> None:
    lease = billed_lease(2301)
    autopay(lease)
    reminders.queue_due(TODAY)
    existing = OutboundMessage.objects.get(lease=lease)
    clone = OutboundMessage(
        operator=existing.operator,
        lease=lease,
        template=existing.template,
        kind=existing.kind,
        channel=existing.channel,
        rule="A",
        occurrence=1,
        charge_entity_type=ChargeEntityType.LEASE_MONTH,
        charge_entity_id=lease.pk,
        period_month=MONTH,
        to_address=existing.to_address,
        rendered_subject="x",
        rendered_body="y",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        clone.save()


def test_a_lease_month_row_needs_its_month_and_a_charge_row_has_none() -> None:
    lease = billed_lease(2302)
    autopay(lease)
    reminders.queue_due(TODAY)
    existing = OutboundMessage.objects.get(lease=lease)
    with pytest.raises(IntegrityError), transaction.atomic():
        OutboundMessage.objects.filter(pk=existing.pk).update(period_month=None)
    # A charge row (of a lease without autopay, which takes C) carries no month.
    manual = billed_lease(2304)
    charge = a_rent_charge(manual, date(2026, 9, 1), day=23)
    reminders.queue_due(date(2026, 9, 28))  # +5: C on September's charge
    c_row = OutboundMessage.objects.get(rule="C", charge_entity_id=charge.pk)
    with pytest.raises(IntegrityError), transaction.atomic():
        OutboundMessage.objects.filter(pk=c_row.pk).update(period_month=date(2026, 9, 1))


def test_the_outbox_and_the_timeline_link_the_store_and_the_charge_log_names_the_month() -> None:
    lease = billed_lease(2303)
    autopay(lease)
    reminders.queue_due(TODAY)
    message = OutboundMessage.objects.get(lease=lease)
    admin, _ = client_for(Role.ADMIN)
    outbox = admin.get("/admin-settings/outbox/").content.decode()
    row_start = outbox.index(f'id="message-{message.pk}"')
    row = outbox[row_start : outbox.index("</tr>", row_start)]
    assert f"/stores/{lease.store_id}/" in row
    assert "/charges/" not in row
    timeline = admin.get(f"/stores/{lease.store_id}/?tab=contacts&show=emails").content.decode()
    assert f'id="message-{message.pk}"' in timeline
    operator_page = admin.get(f"/operators/{lease.operator_id}/").content.decode()
    assert "Rent October 2026" in operator_page
    assert f"/leases/{lease.pk}/" in operator_page
    assert f"/charges/{lease.pk}/" not in operator_page


# --- The rework: B's amount is the first debit's, and the charge page shows the month's row -----


def test_b_names_the_first_debits_amount_for_a_twice_monthly_lease() -> None:
    lease = billed_lease(2401, debit_day=23)
    Lease.objects.filter(pk=lease.pk).update(second_debit_day=28)
    reminders.queue_due(TODAY)
    message = OutboundMessage.objects.get(lease=lease, rule="B")
    assert "$1,750.00" in message.rendered_body  # half of $3,500.00: the month's first debit
    assert "$3,500.00" not in message.rendered_body


def test_b_names_the_prorated_amount_in_the_leases_first_month() -> None:
    # Started on the 13th with the debit on the 23rd: the first month's rent is prorated, and B
    # names what the rent run will charge on the 23rd, not the full month.
    lease = a_lease(
        a_store("2402", number=2402),
        an_operator("Operator 2402"),
        started=date(2026, 10, 13),
        debit_day=23,
        rent="3500.00",
    )
    LeaseBilling.objects.update_or_create(lease=lease, defaults={"email": "store@example.com"})
    reminders.queue_due(TODAY)
    message = OutboundMessage.objects.get(lease=lease, rule="B")
    assert "$3,500.00" not in message.rendered_body
    from ledger.services import rent_run

    charge, _ = rent_run.create_month(lease, MONTH, actor=None)
    from ledger.wording import format_amount

    assert format_amount(charge.amount_due) in message.rendered_body


def test_the_rent_charge_page_lists_the_months_b_and_septembers_does_not() -> None:
    lease = billed_lease(2403)
    reminders.queue_due(TODAY)  # B for October
    october = a_rent_charge(lease, MONTH, day=23)
    september = a_rent_charge(lease, date(2026, 9, 1), day=23)
    billing, _ = client_for(Role.BILLING)
    page = billing.get(f"/charges/{october.pk}/").content.decode()
    assert "Rent October 2026" in page and "B — payment due" in page
    other = billing.get(f"/charges/{september.pk}/").content.decode()
    assert "B — payment due" not in other
