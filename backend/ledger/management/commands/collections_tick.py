"""``collections_tick``: the daily tick (docs/qa/PLAN-011b D8).

Run once a day by the scheduled job (09:00 New York). First it creates the day's rent
charges and catches up the missing ones (:func:`ledger.services.rent_run.create_charges`
with no actor, PLAN-012b-b D3, as the run page's button does), so that the collection
steps after it read them, and records what it created in one ``rent_run.ticked`` audit
row (:func:`ledger.services.rent_run.record_run`, PLAN-012d D11). Then, as a safety
sweep, it closes the open promises to pay that a settled payment keeps
(:func:`ledger.services.contacts.close_kept`, PLAN-012 D4), and queues the ladder's due
steps (:func:`ledger.services.collections.queue_due`, one ``message_send`` job each)
and the day's morning e-mails (:func:`ledger.services.calendar.queue_morning_emails`,
one ``calendar_email`` job each), which the worker sends. It also queues the day's read
of the dispute list (:func:`ledger.services.disputes.queue_daily`, one ``disputes_sync``
job, PLAN-012 D8), and deletes the lease-end drafts whose terms were never set after
seven days (:func:`ledger.services.lease_end.sweep_drafts`, PLAN-012b-b D9). After the
rent run it makes the autopay plans' due occurrences
(:func:`ledger.services.autopay_plans.run_due`, PLAN-012f-a D13: one one-off charge each,
whose ``schedule_create`` job the worker runs). It calls no external service, so it
runs whatever process role it is given. "Today" is the project's local date. Running it
twice the same day creates and queues nothing more (it writes a second
``rent_run.ticked`` row, with nothing created). It prints counts only.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from ledger.services import (
    autopay_plans,
    calendar,
    collections,
    contacts,
    disputes,
    lease_end,
    rent_run,
)


class Command(BaseCommand):
    help = (
        "Create the day's rent charges; queue the day's collection e-mails, morning e-mails "
        "and dispute list read for the worker."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        today = timezone.localdate()
        # First, so that the collection steps below read today's charges (PLAN-012b-b D3).
        charges = rent_run.create_charges(today, actor=None)
        # PLAN-012d D11: the run's report, for the run page and the dashboard's line.
        rent_run.record_run(charges, today, actor=None)
        # PLAN-012f-a D13: the plans' occurrences, after the rent run.
        occurrences = autopay_plans.run_due(today)
        # Before the queueing, so that a promise a payment kept gets no reminder.
        kept = contacts.close_kept()
        ladder = collections.queue_due(today)
        morning = calendar.queue_morning_emails(today)
        dispute_read = disputes.queue_daily(today)
        swept = lease_end.sweep_drafts()
        self.stdout.write(
            f"Day {today:%Y-%m-%d}. Rent charges created: {len(charges.created)}. "
            f"Created (catch-up): {len(charges.caught_up)}. "
            f"Already created: {len(charges.already)}. "
            f"Autopay plan debits created: {len(occurrences)}. "
            f"Collection e-mails queued: {ladder.queued} "
            f"(already logged: {ladder.already}). Morning e-mails queued: {morning.entries}. "
            f"Promise reminders queued: {morning.promises}. Promises kept by a payment: {kept}. "
            f"Dispute list read queued: {'yes' if dispute_read else 'no'}. "
            f"Lease-end drafts without terms removed: {swept}."
        )
