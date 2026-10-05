"""Import the owner's store costs template (docs/qa/PLAN-010b D10).

``import_store_costs <path> [--dry-run]`` reads a ``.csv`` or ``.xlsx`` file with the
template's header and sets each filled row's landlord rent through
:mod:`ledger.services.store_costs`. Prints the counts and each refused row as
"row N: reason" (a reason never repeats a cell's value). Exits 1 when the file is
refused as a whole or any row was refused; the other rows are applied. ``--dry-run``
reads and checks everything and writes nothing. Makes no payment-provider call.

After a real run that created or updated a row, the planner statistics of
``store_costs`` are refreshed (:func:`ledger.services.maintenance.analyze`, PostgreSQL
only; PLAN-010c D11); not after a dry run, and not when nothing changed.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from ledger.models import StoreCost
from ledger.services import maintenance, store_costs


class Command(BaseCommand):
    help = "Import landlord rents from the store costs template (.csv or .xlsx)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("path", help="The .csv or .xlsx file to import.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Check every row and print what would happen; write nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = bool(options["dry_run"])
        try:
            parsed = store_costs.parse_file(options["path"])
        except store_costs.FileRefused as refused:
            raise CommandError(refused.message, returncode=1) from None
        report = store_costs.import_rows(parsed, dry_run=dry_run, actor=None)
        if not dry_run and report.created + report.updated > 0:
            maintenance.analyze(StoreCost)
        prefix = "dry run, nothing written: " if dry_run else ""
        self.stdout.write(prefix + report.counts())
        for number, reason in report.refusals:
            self.stdout.write(f"row {number}: {reason}")
        if report.refusals:
            raise CommandError(f"{report.refused} row(s) refused.", returncode=1)
