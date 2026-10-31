"""Deposit events (PLAN-008 D4).

The provider has no endpoint that reads one deposit by its ID alone, so the deposit is
re-fetched from the list of the last :data:`WINDOW_DAYS` days and found there by ID.
``deposits`` holds the provider's own record, keyed on ``(provider, provider_deposit_id)``:
a new deposit is inserted, a known one is updated when the provider's figures changed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

import payments
from payments.lease_sync import PROVIDER_KEY
from payments.models import Deposit, WebhookOutcome
from payments.provider import DateRange, DepositRecord, mask_identifier
from payments.webhooks.events import ignored
from payments.webhooks.handlers.common import (
    ENTITY_DEPOSIT,
    NO_OBJECT_ID,
    audit,
)

#: How far back the deposit list is read.
WINDOW_DAYS = 31
#: TODO-VERIFY-052: the keys of a deposit in ``GET /query/deposit``: its date in
#: ``batched_at``, gross in ``sum``, fees in ``fees`` and net in ``net`` are assumed. A
#: missing net is gross minus fees; missing fees are zero.
DATE_KEY = "batched_at"
GROSS_KEY = "sum"
FEES_KEY = "fees"
NET_KEY = "net"
REPORTED = "reported"
_CENT = Decimal("0.01")

NOT_RECENT = ignored("deposit not recent")
UNREADABLE = ignored("deposit unreadable")


def handle(event_type: str, provider_object_id: str) -> str:
    if not provider_object_id:
        return NO_OBJECT_ID
    today = timezone.localdate()
    records = payments.get_provider().list_deposits(
        DateRange(today - timedelta(days=WINDOW_DAYS), today)
    )
    record = next((item for item in records if item.external_id == provider_object_id), None)
    if record is None:
        return NOT_RECENT
    values = deposit_values(record)
    if values is None:
        return UNREADABLE
    deposit = (
        Deposit.objects.select_for_update()
        .filter(provider=PROVIDER_KEY, provider_deposit_id=provider_object_id)
        .first()
    )
    masked = mask_identifier(provider_object_id)
    if deposit is None:
        deposit = Deposit.objects.create(
            provider=PROVIDER_KEY, provider_deposit_id=provider_object_id, **values
        )
        audit(
            "deposit.recorded",
            ENTITY_DEPOSIT,
            deposit.pk,
            None,
            _shown(values) | {"deposit": masked},
        )
        return WebhookOutcome.PROCESSED
    before = {name: getattr(deposit, name) for name in values}
    if before != values:
        for name, value in values.items():
            setattr(deposit, name, value)
        deposit.save()
        audit("deposit.updated", ENTITY_DEPOSIT, deposit.pk, _shown(before), _shown(values))
    return WebhookOutcome.PROCESSED


def _money(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount != amount.quantize(_CENT):
        return None
    return amount


def _day(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError:
        return None


def deposit_values(record: DepositRecord) -> dict[str, Any] | None:
    """The ``deposits`` columns of a provider deposit, or ``None`` if unreadable."""
    summary = record.raw_summary
    day = _day(summary.get(DATE_KEY))
    gross = _money(summary.get(GROSS_KEY))
    fees = _money(summary.get(FEES_KEY, 0))
    if day is None or gross is None or fees is None:
        return None
    net = _money(summary.get(NET_KEY, gross - fees))
    if net is None or net != gross - fees:
        return None
    return {
        "deposit_date": day,
        "gross": gross,
        "fees": fees,
        "net": net,
        "status": record.raw_status or REPORTED,
    }


def _shown(values: dict[str, Any]) -> dict[str, str]:
    return {name: str(value) for name, value in values.items()}
