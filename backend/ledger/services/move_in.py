"""The move-in invoice of a new lease (docs/qa/PLAN-014b item 1, Fable's decision 6).

When the New lease page saves an ``active`` lease with an operator,
:func:`create_draft` makes one invoice-builder **draft** (local only; nothing reaches the
payment provider until staff send it): one Rent line for the start month (``rent_lease``,
``rent_month``; its amount left to the builder, which takes the rent run's charge's when the
run or the lease's catch-up already made it, else :func:`ledger.services.rent_run.first_month`'s,
prorated for a partial first month) and one existing-charge line per setup charge the page
created. Its one-time key is the form's creation key with :data:`KEY_SUFFIX`, so a replayed
form finds the same draft.

**How the rent run recognises the Rent line.** The run's "already created" rule is
``rent_charges``' uniqueness on ``(lease, period_month, seq)``
(:func:`ledger.services.rent_run.create_month`, ``get_or_create``). A draft's Rent line holds
no charge; sending the draft makes the start month's charge through the same ``create_month``
(or takes the one the run made meanwhile, unpaid and at the line's amount) and marks it
``billed_by_invoice``. The run then finds that charge and counts it as already created; a run
before the send makes the charge itself and the send takes it. Either way the month has one
charge and is billed once.

The draft cannot be made, and the lease stays saved, when the lease has no operator or no
start date, or the builder refuses a line (a lease debited twice a month: its months are the
rent run's): :func:`create_draft` returns the reason as one sentence.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from django.contrib.auth.models import User
from django.utils import timezone

from ledger.models import CollectionStatus, InvoiceDraft, Lease, OneOffCharge
from ledger.services import invoice_builder

#: Appended to the New lease form's creation key for the draft's own key (uuid5).
KEY_SUFFIX = "move-in-invoice"
MEMO = "Move-in invoice"
NO_OPERATOR = "The lease has no operator, so no move-in invoice was made."
NOT_ACTIVE = "The lease is not active, so no move-in invoice was made."
NO_START = "The lease has no start date, so no move-in invoice was made."
REFUSED = "The move-in invoice could not be made: {reason}"
_LINE_PREFIX = re.compile(r"\ALine \d+: ")


@dataclass(frozen=True, slots=True)
class MoveIn:
    """The draft made, or why none was made (one sentence)."""

    draft: InvoiceDraft | None
    message: str


def draft_key(creation_key: uuid.UUID) -> uuid.UUID:
    """The draft's one-time key: the lease form's key with :data:`KEY_SUFFIX`."""
    return uuid.uuid5(creation_key, KEY_SUFFIX)


def create_draft(
    lease: Lease,
    charges: Sequence[OneOffCharge],
    *,
    creation_key: uuid.UUID,
    actor: User | None,
    today: date | None = None,
) -> MoveIn:
    """The move-in draft of ``lease`` (see the module docstring): due on the start date, or
    today when the lease started earlier (the builder refuses a due date in the past)."""
    operator = lease.operator
    if operator is None:
        return MoveIn(None, NO_OPERATOR)
    if lease.collection_status != CollectionStatus.ACTIVE:
        return MoveIn(None, NOT_ACTIVE)
    start = lease.started_on
    if start is None:
        return MoveIn(None, NO_START)
    day = today or timezone.localdate()
    lines = [
        invoice_builder.LineInput(
            "", "", None, None, rent_lease=lease.pk, rent_month=start.replace(day=1)
        ),
        *(
            invoice_builder.LineInput("", "", None, None, existing_charge=charge.pk)
            for charge in charges
        ),
    ]
    outcome = invoice_builder.create_draft(
        operator,
        lease,
        MEMO,
        max(start, day),
        lines,
        actor,
        today=day,
        creation_key=draft_key(creation_key),
    )
    if not outcome.ok or outcome.draft is None:
        reason = _LINE_PREFIX.sub("", outcome.message)
        return MoveIn(None, REFUSED.format(reason=reason[:1].lower() + reason[1:]))
    return MoveIn(outcome.draft, outcome.message)
