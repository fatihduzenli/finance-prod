"""Create the annual insurance charges now in scope (PLAN-007 D9, ADR-0005).

Idempotent: a charge exists at most once per lease and policy year, so a second run
creates nothing. Prints the counts. Makes no payment-provider call.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from ledger.services.insurance import generate_insurance_charges


class Command(BaseCommand):
    help = "Create the annual insurance charges in scope today (idempotent)."

    def handle(self, *args: Any, **options: Any) -> None:
        report = generate_insurance_charges(timezone.localdate())
        self.stdout.write(
            f"created {report.created}, already existed {report.existing}, "
            f"missing premium {report.needs_premium}, "
            f"skipped (store has no open date) {report.needs_open_date}"
        )
