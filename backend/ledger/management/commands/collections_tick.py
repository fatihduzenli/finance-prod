"""``collections_tick``: the daily tick (docs/qa/PLAN-011b D8).

Run once a day by the scheduled job (09:00 New York). It only queues: the ladder's due
steps (:func:`ledger.services.collections.queue_due`, one ``message_send`` job each) and
the day's morning e-mails (:func:`ledger.services.calendar.queue_morning_emails`, one
``calendar_email`` job each); the worker sends them. It calls no external service, so it
runs whatever process role it is given. "Today" is the project's local date. Running it
twice the same day queues nothing more. It prints counts only.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from ledger.services import calendar, collections


class Command(BaseCommand):
    help = "Queue the day's collection e-mails and morning e-mails for the worker."

    def handle(self, *args: Any, **options: Any) -> None:
        today = timezone.localdate()
        ladder = collections.queue_due(today)
        morning = calendar.queue_morning_emails(today)
        self.stdout.write(
            f"Day {today:%Y-%m-%d}. Collection e-mails queued: {ladder.queued} "
            f"(already logged: {ladder.already}). Morning e-mails queued: {morning.entries}. "
            f"Promise reminders queued: {morning.promises}."
        )
