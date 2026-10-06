"""``tz_audit``: list the stored dates the New York time zone would change
(docs/qa/PLAN-011b D1). Read only.

Prints, per column, each row whose stored date is the UTC date of its source timestamp
and not its New York date, as ``table.column <masked id> <old> -> <new>``, then the
counts, the rows that differ for another reason (never rewritten), the rows without a
source timestamp, and the columns unchanged by design with the reason. The last line
names the ``tz_apply`` command for this audit's total. See :mod:`payments.timezone_audit`.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from payments import timezone_audit


class Command(BaseCommand):
    help = "List the stored dates whose New York date differs from their UTC date (read only)."

    def handle(self, *args: Any, **options: Any) -> None:
        for line in timezone_audit.report_lines(timezone_audit.audit()):
            self.stdout.write(line)
