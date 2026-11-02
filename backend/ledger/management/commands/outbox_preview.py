"""``outbox_preview [--days 30]``: what the daily reminder job would queue on each of the next
days, from today's data, printed as tab-separated text (owner, 2026-11-01).

The same rows as Admin › Outbox › Preview next 30 days
(:func:`ledger.services.outbox_preview.preview`): a header line, then day, rule, template,
store, operator, charge, amount and outcome per message, then the automatic re-debits in the
window (rule F follows a return and is not predicted). No e-mail address and no message text
is printed. It writes nothing (its transaction is rolled back) and calls no external service,
so it can be run on staging to read what the job will do.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from ledger.services import outbox_preview


class Command(BaseCommand):
    help = "Print what the reminder job would queue in the next days (read only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--days", type=int, default=outbox_preview.DAYS)

    def handle(self, *args: Any, **options: Any) -> None:
        days = options["days"]
        if not 1 <= days <= outbox_preview.MAX_DAYS:
            raise CommandError(f"--days must be from 1 to {outbox_preview.MAX_DAYS}.")
        found = outbox_preview.preview(timezone.localdate(), days)
        for line in outbox_preview.as_lines(found):
            self.stdout.write(line)
