"""What the wizard frame of End lease and Transfer reads besides the step's own values
(docs/qa/PLAN-012f-a-design-follow-up.md D8; DESIGN-SPEC §4 Transfer wizard).

:func:`frame` gives the header's "Step N of M" and the footer's "← Back" (the step before
the current one, or the store's page on the first step) from the step links the views
already build. :func:`statement_notes` gives the statement tiles' notes and the SETTLEMENT
box's derived rows from the figures (``statements.final`` or the stored statement): read
only, one query for the day the deposit was collected. Every value is text the template
escapes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.db.models import Max
from django.urls import reverse

from finance.dates import day_text
from ledger.models import Payment, PaymentKind, PaymentStatus, Store
from ledger.services import statements
from ledger.services import stores as store_service


def frame(steps: Sequence[tuple[str, str, bool]], store: Store) -> dict[str, Any]:
    """The step's number and count, the Back link and the store's title."""
    number = next((index for index, step in enumerate(steps, 1) if step[2]), 0)
    back = steps[number - 2][1] if number > 1 else ""
    return {
        "step_number": number,
        "step_count": len(steps),
        "step_back": back or reverse("ledger:stores:detail", kwargs={"pk": store.pk}),
        "store_title": store_service.title(store),
    }


def statement_notes(figures: statements.Final) -> dict[str, Any]:
    """The tiles' notes ("rent 1–22 Oct, 22 of 31 days"; "collected Oct 14, 2026", or the
    deposit's "charged …, collected …"; "1 line") and the SETTLEMENT box's rows the figures
    do not hold as such: the part of the deposit kept for the deductions other than the
    unpaid-balance offset, and the credits paid back with the refund."""
    last = figures.last_month
    open_note = ""
    if last is not None and last.prorate and last.text:
        days, found, _ = last.text.partition(" days of ")
        open_note = f"rent {days} days" if found else f"rent {last.text}"
    # A deposit collected below what it billed reads "Deposit charged $500.00, collected
    # $200.00" (PLAN-012b-b D11); else the day it was collected, when a payment gives it.
    deposit_note = "; ".join(line.text for line in figures.deposits if line.charged != line.held)
    charges = [line.charge_id for line in figures.deposits]
    if charges and not deposit_note:
        collected = Payment.objects.filter(
            one_off_charge_id__in=charges,
            kind=PaymentKind.ONE_OFF,
            status=PaymentStatus.SETTLED,
            is_refund=False,
        ).aggregate(day=Max("settled_on"))["day"]
        if collected is not None:
            deposit_note = f"collected {day_text(collected)}"
    count = len(figures.deductions)
    return {
        "open_note": open_note,
        "deposit_note": deposit_note,
        "deductions_note": f"{count} line" if count == 1 else f"{count} lines",
        "kept_for_deductions": figures.withheld - figures.offset_applied,
        "credit_paid_back": figures.credit + figures.lease_credit,
    }
