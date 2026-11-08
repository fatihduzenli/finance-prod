"""Free rent by days (docs/qa/15/PLAN-015d §3 (2)).

A lease may carry ``free_rent_days`` (0 to 366) counted from its move-in date
(``free_rent_from = move_in``: the start date is the first free day) or from the 1st of the
month after its start month (``next_month``). :func:`period` is that span, both ends counted.

**The concession.** A rent charge keeps its amount; its ``concession`` nets the free days of its
month: the month's rent (the prorated first month's, when the lease starts after the 1st) times
the free days inside the days that month bills over those days. Each month is rounded to the
cent, half up, through the running total of the period (:func:`month_concessions`): a month's
concession is the rounded total up to and including it less the rounded total before it, so
the last month takes the remainder and the period's concessions sum exactly to the rounded sum
of its exact shares. A month entirely inside the period is free in full. A lease debited twice a
month splits the month's concession as its rent is split (the odd cent on the first debit).
Never more than the charge's amount.

The text, on the charge and the invoice's discount line: "Free rent per lease agreement
(Dec 10 – Jan 8)".

:func:`recompute` follows a change of the free period on Edit lease: the concession of each
charge not billed yet (due, nothing paid or pending, no invoice, no installment plan) is
recomputed; a billed charge keeps its own and is named in the result (the page shows the
warning). Local data only, never a provider call.

**Autopay** (Fable's finding 2 on PLAN-015d). A provider schedule debits a fixed amount every
month, so it never debits a month the free period touches: a month free in full has nothing
to debit, and a month partly free is collected at its net by the invoice and reminder path
(rule B, as for a lease not on autopay). :func:`autopay_from` is the first month a schedule
may debit — the month after the last month of the free period — and
:func:`ledger.services.leases.debit_plan` starts a new schedule (its ``DTSTART``) on that
month's debit day at the earliest; :func:`debited_by_autopay` is what the reminders read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth.models import User
from django.db import transaction

from finance.dates import day_short_text, month_text
from ledger.models import (
    ZERO,
    AuditLog,
    FreeRentFrom,
    Lease,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import proration

CENT = Decimal("0.01")
TEXT = "Free rent per lease agreement ({start} – {end})"
#: The LEASE card's row: "30 days, Dec 10 – Jan 8".
CARD_TEXT = "{days} days, {start} – {end}"
CARD_TEXT_ONE = "1 day, {start}"
RECOMPUTED_ACTION = "rent_charge.concession_recomputed"
SET_ACTION = "lease.free_rent_set"
#: The LEASE card's words while a partly free month is current on a lease on autopay.
NO_AUTOPAY_DEBIT = "Free rent: no autopay debit this month; the net is invoiced"
#: The Edit lease warning: billed months keep the concession they were billed with.
BILLED_KEPT = "{months} already billed: {it} keeps the free rent it was billed with."


def period(lease: Lease) -> tuple[date, date] | None:
    """The free days of ``lease``, first and last both counted; ``None`` without free days or
    a start date."""
    days = int(lease.free_rent_days or 0)
    start = lease.started_on
    if days <= 0 or start is None:
        return None
    if lease.free_rent_from == FreeRentFrom.NEXT_MONTH:
        first = _next_month(start.replace(day=1))
    else:
        first = start
    return first, first + timedelta(days=days - 1)


def text(lease: Lease) -> str:
    """The concession's words, or ``""`` without free days."""
    span = period(lease)
    if span is None:
        return ""
    return TEXT.format(start=day_short_text(span[0]), end=day_short_text(span[1]))


def card_text(lease: Lease) -> str:
    """The LEASE card's "Free rent" row: "30 days, Dec 10 – Jan 8", or ``""``."""
    span = period(lease)
    if span is None:
        return ""
    if span[0] == span[1]:
        return CARD_TEXT_ONE.format(start=day_short_text(span[0]))
    return CARD_TEXT.format(
        days=lease.free_rent_days,
        start=day_short_text(span[0]),
        end=day_short_text(span[1]),
    )


def autopay_from(lease: Lease) -> date | None:
    """The first month (its 1st) a rent schedule may debit: the month after the last month
    the free period touches; ``None`` without free days (any month)."""
    span = period(lease)
    if span is None:
        return None
    return _next_month(span[1].replace(day=1))


def debited_by_autopay(lease: Lease, month: date) -> bool:
    """Whether ``month``'s rent may be debited by a rent schedule (see the module docstring):
    not while the free period touches the month."""
    first = autopay_from(lease)
    return first is None or month >= first


def card_note(lease: Lease, today: date, *, autopay: bool) -> str:
    """:data:`NO_AUTOPAY_DEBIT` while ``today``'s month is partly free on a lease on autopay
    (its net is invoiced, not debited), else ``""``."""
    span = period(lease)
    month = today.replace(day=1)
    if not autopay or span is None or debited_by_autopay(lease, month):
        return ""
    free, billed = _free_days(lease, month, span)
    return NO_AUTOPAY_DEBIT if 0 < free < billed else ""


def first_month_concession(
    start: date | None, days: int, start_from: str, first_amount: Decimal, *, prorate: bool
) -> Decimal:
    """The free rent of the start month from the terms alone (the New lease Summary, before
    anything is saved): what :func:`concession_for` gives that month. Zero when the free days
    start the month after."""
    if start is None or days <= 0 or start_from == FreeRentFrom.NEXT_MONTH:
        return ZERO
    month = start.replace(day=1)
    last = proration.last_day(month)
    billed_from = start if prorate and start.day > 1 else month
    free_last = min(start + timedelta(days=days - 1), last)
    free = (free_last - start).days + 1
    billed = (last - billed_from).days + 1
    if free >= billed:
        return first_amount
    return (first_amount * free / billed).quantize(CENT, rounding=ROUND_HALF_UP)


def _next_month(month: date) -> date:
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def _billed_span(lease: Lease, month: date) -> tuple[date, date]:
    """The days ``month``'s charge bills: from the start date in the start month when the
    lease is prorated (PLAN-012e D2), else the whole month."""
    last = proration.last_day(month)
    start = lease.started_on
    if (
        lease.prorate_first_month
        and start is not None
        and start.replace(day=1) == month
        and start.day > 1
    ):
        return start, last
    return month, last


def _month_amount(lease: Lease, month: date) -> Decimal:
    """What ``month`` bills in all (both debits of a twice-monthly lease): the rent in effect
    on the first debit's date, prorated as :func:`ledger.services.rent_run.first_month`;
    zero when no rent is in effect."""
    from ledger.services import leases, rent_run  # rent_run uses this module

    debit_day = lease.debit_day or 1
    rent = leases.rent_on(lease, rent_run.debit_date(lease, month, debit_day))
    if rent is None:
        return ZERO
    return Decimal(rent_run.first_month(lease, month, Decimal(rent.amount)).amount)


def _free_days(lease: Lease, month: date, span: tuple[date, date]) -> tuple[int, int]:
    """The free days of ``month`` and the days it bills."""
    billed_from, billed_to = _billed_span(lease, month)
    first, last = max(billed_from, span[0]), min(billed_to, span[1])
    free = (last - first).days + 1 if first <= last else 0
    return free, (billed_to - billed_from).days + 1


def _exact_share(lease: Lease, month: date, span: tuple[date, date]) -> Decimal:
    """``month``'s exact (unrounded) concession: its amount times its free days over its
    billed days."""
    free, billed = _free_days(lease, month, span)
    if free == 0:
        return ZERO
    amount = _month_amount(lease, month)
    if free == billed:
        return amount
    return amount * free / billed


def month_concessions(lease: Lease) -> dict[date, Decimal]:
    """The concession of every month of the free period (the months' first days), each
    rounded through the running total (the last month takes the remainder)."""
    span = period(lease)
    if span is None:
        return {}
    found: dict[date, Decimal] = {}
    running = ZERO
    rounded_before = ZERO
    month = span[0].replace(day=1)
    while month <= span[1]:
        running += _exact_share(lease, month, span)
        rounded = running.quantize(CENT, rounding=ROUND_HALF_UP)
        found[month] = rounded - rounded_before
        rounded_before = rounded
        month = _next_month(month)
    return found


def concession_for(
    lease: Lease, period_month: date, amount_due: Decimal, *, seq: int = 1
) -> tuple[Decimal, str]:
    """The concession of the charge of ``period_month`` (debit ``seq``) and its text; zero
    and ``""`` for a month outside the free period. Never more than ``amount_due``."""
    from ledger.services.leases import debit_amounts

    span = period(lease)
    if span is None:
        return ZERO, ""
    free, billed = _free_days(lease, period_month, span)
    if free == 0:
        return ZERO, ""
    if free == billed:
        # A month inside the period is free in full, whatever its amount.
        return Decimal(amount_due), text(lease)
    month = month_concessions(lease).get(period_month, ZERO)
    if month <= 0:
        return ZERO, ""
    twice = lease.second_debit_day is not None
    shares = debit_amounts(month, twice_monthly=twice)
    share = shares[seq - 1] if 1 <= seq <= len(shares) else ZERO
    share = min(share, Decimal(amount_due))
    if share <= 0:
        return ZERO, ""
    return share, text(lease)


def kept(lease: Lease) -> tuple[RentCharge, ...]:
    """The charges whose concession differs from what the lease's free period now gives:
    after :func:`recompute`, the billed ones that kept their own (the Edit lease warning)."""
    found: list[RentCharge] = []
    for charge in (
        RentCharge.objects.filter(lease=lease)
        .exclude(status__in=(RentChargeStatus.WAIVED, RentChargeStatus.VOID))
        .order_by("period_month", "seq")
    ):
        amount, words = concession_for(
            lease, charge.period_month, Decimal(charge.amount_due), seq=charge.seq
        )
        if (Decimal(charge.concession), charge.concession_text) != (amount, words):
            found.append(charge)
    return tuple(found)


@dataclass(frozen=True, slots=True)
class Recomputed:
    """What :func:`recompute` changed and what it kept."""

    changed: tuple[RentCharge, ...]
    #: Charges already billed, which keep their concession.
    kept: tuple[RentCharge, ...]

    @property
    def warning(self) -> str:
        if not self.kept:
            return ""
        months = ", ".join(month_text(charge.period_month) for charge in self.kept)
        return BILLED_KEPT.format(months=months, it="it" if len(self.kept) == 1 else "each")


def status_for(amount_due: Decimal, concession: Decimal) -> str:
    """A new or recomputed charge's status: ``paid`` when free rent covers it all (nothing
    is owed), else ``due``."""
    return RentChargeStatus.PAID if concession >= amount_due else RentChargeStatus.DUE


def _billed(charge: RentCharge) -> bool:
    from ledger.services import rent_run

    covered = charge.status == RentChargeStatus.PAID and charge.net_due <= 0
    return (
        (charge.status != RentChargeStatus.DUE and not covered)
        or charge.amount_paid > 0
        or charge.billed_by_invoice_id is not None
        or charge.invoices.exists()
        or charge.invoice_lines.exists()
        or rent_run.on_live_plan(rent_charge=charge)
    )


def recompute(lease: Lease, actor: User | None) -> Recomputed:
    """Recompute the concession of ``lease``'s charges not billed yet after its free period
    changed (see the module docstring); audited per charge changed. Runs in the caller's
    transaction (or its own)."""
    changed: list[RentCharge] = []
    kept: list[RentCharge] = []
    with transaction.atomic():
        charges = (
            RentCharge.objects.select_for_update()
            .filter(lease=lease)
            .exclude(status__in=(RentChargeStatus.WAIVED, RentChargeStatus.VOID))
            .order_by("period_month", "seq")
        )
        for charge in charges:
            amount, words = concession_for(
                lease, charge.period_month, Decimal(charge.amount_due), seq=charge.seq
            )
            if (Decimal(charge.concession), charge.concession_text) == (amount, words):
                continue
            if _billed(charge):
                kept.append(charge)
                continue
            before = {
                "concession": str(charge.concession),
                "concession_text": charge.concession_text,
            }
            charge.concession = amount
            charge.concession_text = words
            charge.status = status_for(Decimal(charge.amount_due), amount)
            charge.save(update_fields=["concession", "concession_text", "status", "updated_at"])
            AuditLog.objects.create(
                actor=actor,
                action=RECOMPUTED_ACTION,
                entity_type="rent_charge",
                entity_id=str(charge.pk),
                before=before,
                after={"concession": str(amount), "concession_text": words},
            )
            changed.append(charge)
    return Recomputed(tuple(changed), tuple(kept))
