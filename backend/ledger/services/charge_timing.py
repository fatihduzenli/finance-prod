"""When a New lease's move-in charge is collected (docs/qa/15/PLAN-015j-move-in-charge-timing.md
§4, §9, §10; PLAN-015d §3 (3) for the deposit, generalised to every setup charge).

Each priced row of the New lease page's Move-in charges chooses :class:`When`:

- ``with_invoice`` (the default, the behaviour before PLAN-015j): the charge is a line of the
  move-in draft and is due on the lease's start date;
- ``on_date``: its own charge, due on the chosen date and marked ``dated``, not a line of the
  move-in draft (a non-deposit charge has no invoice of its own: staff record the payment or
  put it on an invoice, §9.5);
- ``installments``: 2 to 12 payments from a first date, monthly, every two weeks or weekly,
  through the installment-plan engine (:func:`ledger.services.installments.create_plan`):
  origin ``deposit`` for the deposit, ``split`` for any other charge, both collected as the
  lease's channel (:func:`ledger.services.installments.lease_channel_collection`); not a line
  of the move-in draft.

A date (the due date, the first installment's) is any day from today on, before the lease's
start included (§10). Nothing about a charge's timing is stored beyond its ``dated`` flag, its
due date and its plan.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from finance.dates import day_text
from ledger.models import (
    FeeType,
    InstallmentCadence,
    InstallmentOrigin,
    InstallmentPlanStatus,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
)
from ledger.services import deposits, installments, leases, one_off_charges
from ledger.services.one_off_charges import ChargeInput, SetupRefused


class When(StrEnum):
    """When a move-in charge is collected."""

    WITH_INVOICE = "with_invoice"
    ON_DATE = "on_date"
    INSTALLMENTS = "installments"


WHEN_CHOICES = [
    (When.WITH_INVOICE.value, "With the first invoice"),
    (When.ON_DATE.value, "Due on a date"),
    (When.INSTALLMENTS.value, "In installments"),
]
#: The cadences a move-in plan offers (Split's three; yearly is the settlement's).
CADENCES = [
    (InstallmentCadence.MONTHLY.value, "Monthly"),
    (InstallmentCadence.BIWEEKLY.value, "Every two weeks"),
    (InstallmentCadence.WEEKLY.value, "Weekly"),
]
DUE_REQUIRED = "Enter the date the charge is due."
DUE_BEFORE_TODAY = "The due date cannot be in the past."
#: §9.5: beside "Due on a date" on a row that is not the deposit.
NOT_ON_INVOICE = (
    "Not on the first invoice. Record the payment or add it to an invoice when it is due."
)
#: The lease page's words of a charge on a plan (§4 "Lease page").
PLAN_PROGRESS = "{paid} of {count} installments paid"
DUE_ON = "due {day}"


@dataclass(frozen=True, slots=True)
class ChargeTiming:
    """One row's choice: with the first invoice (a move-in line), on a date (``due_on``; its
    own charge), or in ``count`` installments from ``first_due`` every ``cadence``."""

    when: str = When.WITH_INVOICE
    due_on: date | None = None
    count: int = 0
    first_due: date | None = None
    cadence: str = InstallmentCadence.MONTHLY

    @property
    def on_invoice(self) -> bool:
        return self.when == When.WITH_INVOICE

    @property
    def due_date(self) -> date | None:
        """The charge's own due date: the chosen date, or the first installment's."""
        if self.when == When.ON_DATE:
            return self.due_on
        if self.when == When.INSTALLMENTS:
            return self.first_due
        return None


WITH_INVOICE = ChargeTiming()


def _field(fee_type: FeeType | None, part: str | None) -> str | None:
    """The New lease form's field of the row (``setup_<code>_<part>``), for a refusal."""
    if fee_type is None or not part:
        return None
    return f"setup_{fee_type.code}_{part}"


def timing_of(charge: OneOffCharge, timings: Mapping[Any, ChargeTiming]) -> ChargeTiming:
    """The row's timing of a charge the save made (with the first invoice when none)."""
    return timings.get(charge.fee_type_id, WITH_INVOICE)


def on_first_invoice(
    charges: Iterable[OneOffCharge], timings: Mapping[Any, ChargeTiming]
) -> list[OneOffCharge]:
    """The charges that are lines of the move-in draft: those ``with_invoice`` (§4)."""
    return [charge for charge in charges if timing_of(charge, timings).on_invoice]


def create_lease(
    fields: Any,
    *,
    rent: Decimal,
    creation_key: Any,
    items: Sequence[ChargeInput],
    actor: User | None,
    timings: Mapping[Any, ChargeTiming],
    billing: Any = None,
    account: str = "",
    today: date | None = None,
) -> tuple[Lease, bool, list[OneOffCharge]]:
    """The New lease save (:func:`ledger.services.one_off_charges.create_lease_with_checklist`,
    unchanged; ``items`` carry each row's due date and ``dated``) and, in the same transaction,
    one plan per charge whose timing (``timings``, per fee type id) is ``installments``. A
    refused plan raises :class:`~ledger.services.one_off_charges.SetupRefused` naming the row,
    and nothing is saved, the lease included. A replayed ``creation_key`` returns the first
    lease and makes nothing."""
    day = today or timezone.localdate()
    with transaction.atomic():
        lease, created, charges = one_off_charges.create_lease_with_checklist(
            fields,
            rent=rent,
            creation_key=creation_key,
            items=items,
            actor=actor,
            billing=billing,
            account=account or leases.ACCOUNT_LATER,
        )
        if not created:
            return lease, created, charges
        for charge in charges:
            timing = timing_of(charge, timings)
            if timing.when == When.INSTALLMENTS:
                _plan(charge, timing, actor, day)
    return lease, created, charges


def _plan(charge: OneOffCharge, timing: ChargeTiming, actor: User | None, day: date) -> None:
    """The charge's plan, or :class:`SetupRefused` with the engine's sentence."""
    fee_type = charge.fee_type
    if timing.first_due is None:
        raise SetupRefused(_field(fee_type, "first_due"), installments.FIRST_REQUIRED, fee_type)
    lines, refused = installments.preview(
        charge, timing.count, timing.first_due, timing.cadence, day
    )
    made = refused or installments.create_plan(
        charge,
        timing.count,
        timing.first_due,
        timing.cadence,
        lines,
        actor,
        confirmed=True,
        today=day,
        collection=installments.lease_channel_collection(charge),
        origin=(
            InstallmentOrigin.DEPOSIT if deposits.is_deposit(charge) else InstallmentOrigin.SPLIT
        ),
    )
    if not made.ok:
        raise SetupRefused(_field(fee_type, made.field), made.message, fee_type)


def lease_words(charge: OneOffCharge) -> str:
    """What the lease's charges list says of a setup charge's timing (§4 "Lease page", §9.2),
    ``""`` when nothing (the row's usual words apply):

    - on a plan (not cancelled): "1 of 3 installments paid";
    - owed, on no plan and on no invoice nor draft not yet sent
      (:func:`ledger.services.deposits.riding_invoice`), set "Due on a date" (``dated``) or
      whose stored due date is not its lease's start date (an older "More → Due date"): "due
      Dec 15, 2026".

    A charge on an invoice or a draft (the first invoice's lines) has no words of its own,
    whatever its due date: "Activate now" or Change start date moves the start, not the
    charge's stored due date (§13)."""
    if charge.category != OneOffCategory.SETUP:
        return ""
    plan = (
        charge.installment_plans.exclude(status=InstallmentPlanStatus.CANCELLED)
        .order_by("pk")
        .first()
    )
    if plan is not None:
        progress = installments.progress(plan)
        return PLAN_PROGRESS.format(paid=progress.paid_count, count=progress.count)
    if charge.status not in (OneOffChargeStatus.OWED, OneOffChargeStatus.PARTIAL):
        return ""
    if charge.balance <= 0:
        return ""
    if deposits.riding_invoice(charge):
        return ""
    start = charge.lease.started_on if charge.lease is not None else None
    if charge.dated or (start is not None and charge.due_date != start):
        return DUE_ON.format(day=day_text(charge.due_date))
    return ""
