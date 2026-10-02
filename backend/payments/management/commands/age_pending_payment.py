"""``age_pending_payment``: a test fixture for staging (docs/qa/PLAN-010 D11).

Moves a pending payment's ``initiated_on`` back by ``--days`` days, so the next
reconciliation flags it ``pending_too_old`` and the alert can be seen end to end. Only a
pending payment; anything else is refused with a sentence and nothing changes. Refused
outright when ``APP_ENV=production``. Writes an audit row, and prints only the new date.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from ledger.models import AuditLog, Payment, PaymentStatus

ENTITY_PAYMENT = "payment"
#: The most days a payment may be moved back.
MAX_DAYS = 3650


class Command(BaseCommand):
    help = "Test fixture: move a pending payment's initiated date back N days (not production)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--payment", required=True, help="The payment's ID.")
        parser.add_argument("--days", required=True, type=int, help="Days to move it back.")

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.APP_ENV == "production":
            raise CommandError("This test fixture is refused in production.")
        days: int = options["days"]
        if not 1 <= days <= MAX_DAYS:
            raise CommandError(f"--days must be from 1 to {MAX_DAYS}.")
        try:
            payment_id = uuid.UUID(str(options["payment"]))
        except ValueError:
            raise CommandError("--payment must be a payment ID.") from None
        with transaction.atomic():
            payment = Payment.objects.select_for_update().filter(pk=payment_id).first()
            if payment is None:
                raise CommandError("No payment has this ID.")
            if payment.status != PaymentStatus.PENDING:
                raise CommandError(
                    f"Only a pending payment can be aged; this one is {payment.status}. "
                    "Nothing was changed."
                )
            if payment.initiated_on is None:
                raise CommandError("This payment has no initiated date. Nothing was changed.")
            before = payment.initiated_on
            payment.initiated_on = before - timedelta(days=days)
            payment.save(update_fields=["initiated_on", "updated_at"])
            AuditLog.objects.create(
                actor=None,
                action="payment.aged_for_test",
                entity_type=ENTITY_PAYMENT,
                entity_id=str(payment.pk),
                before={"initiated_on": before.isoformat()},
                after={"initiated_on": payment.initiated_on.isoformat(), "days": days},
            )
        self.stdout.write(payment.initiated_on.isoformat())
