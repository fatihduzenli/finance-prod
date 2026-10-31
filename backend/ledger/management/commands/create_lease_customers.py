"""``create_lease_customers [--dry-run] [--limit N]`` (docs/qa/PLAN-014 Batch C, C9).

Every lease that shares another lease's processor account since ledger migration 0059
(``LeaseBilling.shares_account_of``) and holds no customer of its own, not ended and of an
operator not archived, in store-number order: one ``lease_sync`` job each with
``own_customer`` (:func:`ledger.services.leases.queue_own_customers`), which the worker
runs; the job creates the lease's own customer at the processor without adopting one that
another lease holds (:func:`payments.lease_sync.sync_lease`). A lease without a billing
e-mail, or whose sync already waits, is not queued. ``shares_account_of`` is not changed
and no bank method is touched: the lease keeps billing through the shared account until
staff switch it. ``--limit`` queues at most N; ``--dry-run`` queues nothing.

It prints ``leases: <found>, queued: <queued>`` and nothing else. Run on staging or
production it writes to the processor (through the worker): a Section 6 action that needs
its own authorisation (docs/RUNBOOK.md).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from ledger.services import leases


class Command(BaseCommand):
    help = (
        "Queue a processor customer of its own for every lease that shares another lease's "
        "account (no bank method is touched)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Count only; queue nothing.")
        parser.add_argument("--limit", type=int, default=None, help="Queue at most N leases.")

    def handle(self, *args: Any, **options: Any) -> None:
        limit = options["limit"]
        if limit is not None and limit < 0:
            limit = 0
        found = leases.queue_own_customers(dry_run=options["dry_run"], limit=limit)
        self.stdout.write(f"leases: {found.leases}, queued: {found.queued}")
