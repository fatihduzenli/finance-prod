"""``tz_apply --audited N``: rewrite the dates ``tz_audit`` lists (docs/qa/PLAN-011b D1).

Refuses, changing nothing, unless ``N`` equals the total of the audit run now, inside
the same transaction as the rewrite; so the rows rewritten are exactly the rows the
owner was shown. Each rewritten row gets an ``audit_log`` row, action
``timezone.date_realigned``, with the date before and after. A row that changed between
the audit and the rewrite rolls the whole run back. Run only on the owner's word (D10).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from payments import timezone_audit


class Command(BaseCommand):
    help = "Rewrite the dates tz_audit lists; --audited must equal the audit's total."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--audited",
            required=True,
            type=int,
            help="The total printed by tz_audit; the run is refused unless it still matches.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        audited: int = options["audited"]
        if audited < 0:
            raise CommandError("--audited must be zero or more. Nothing was changed.")
        try:
            applied = timezone_audit.apply(audited)
        except timezone_audit.AuditCountMismatch:
            current = timezone_audit.audit().total
            raise CommandError(
                f"--audited {audited} does not match the current audit's total of {current}. "
                "Run tz_audit again and review its list. Nothing was changed."
            ) from None
        except timezone_audit.RowChangedSinceAudit:
            raise CommandError(
                "A listed row changed during the run. Nothing was changed; run tz_audit again."
            ) from None
        for change in applied.changes:
            self.stdout.write(f"realigned {change.line()}")
        self.stdout.write(f"Rows realigned: {applied.total}")
