"""What is sent when a manual payment is marked paid at the provider (PLAN-009 D1 to D3).

The ledger records a wire, Zelle or other payment locally first. When it covers the
charge's whole amount and the charge has a provider invoice, the rent run marks that
invoice paid through ``mark_invoice_paid_cash_or_check`` with :func:`offline_payment`:
method ``cash`` (the provider has no wire or Zelle method; this is what staff do by hand
today, owner Q19), a memo saying how and when the money really arrived, our reference
as the check number, and no receipt e-mail. ``apply_balance`` is never sent
(TODO-VERIFY-028), so a partial payment is not sent at all.

The review texts below are what the charge stores in ``review_message``; they name
the provider, so the ledger never shows them as they are: :func:`shown` gives the text
for the screen.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from payments.provider import OfflinePayment
from payments.stax.validation import CHECK_NUMBER_MAX_LENGTH, PAYMENT_NOTE_MAX_LENGTH

#: How the memo names each local method; ``credit`` is a credit held on the lease applied
#: to the charge (PLAN-012d D6a), never a method staff choose.
METHOD_WORDS: dict[str, str] = {
    "wire": "wire",
    "zelle": "Zelle",
    "other": "other",
    "credit": "credit",
}

_NOT_CHECK_NUMBER = re.compile(r"[^A-Za-z0-9-]")
_WHITESPACE = re.compile(r"\s+")
_TODO_MARK = re.compile(r"\s*\(TODO-VERIFY-\d+\)")

#: Stored on the charge (``review_message``) when the provider invoice was not updated.
PARTIAL_AMOUNT = "Stax invoice not updated: partial amount (TODO-VERIFY-028)"
TOTAL_DIFFERS = "Stax invoice not updated: the payment differs from the invoice total"
ALREADY_PAID = (
    "Stax invoice already showed paid before marking; not marked again. Check for a double payment"
)
MARK_FAILED = "Stax invoice not marked paid: {failure}"
REREAD_FAILED = "Stax invoice marked paid but not re-read: {failure}"


def memo(method: str, paid_on: date, note: str) -> str:
    """``Paid by <wire|Zelle|other> on <MM/DD/YYYY>. <staff note>``, on one line, cut to
    the provider's 255 characters."""
    text = f"Paid by {METHOD_WORDS[method]} on {paid_on:%m/%d/%Y}. {note}"
    return _WHITESPACE.sub(" ", text).strip()[:PAYMENT_NOTE_MAX_LENGTH]


def check_number(reference: str) -> str:
    """Our reference reduced to letters, digits and hyphens, cut to 25; may be empty."""
    return _NOT_CHECK_NUMBER.sub("", reference)[:CHECK_NUMBER_MAX_LENGTH]


def offline_payment(
    *, method: str, amount: Decimal, paid_on: date, note: str, reference: str
) -> OfflinePayment:
    """The mark-paid request for a local manual payment (PLAN-009 D1)."""
    return OfflinePayment(
        method="cash",
        amount=amount,
        check_number=check_number(reference),
        note=memo(method, paid_on, note),
        email_receipt=False,
    )


def failure_text(template: str, failure: str, fields: str) -> str:
    """A failure text naming the error class and, for a rejection, the key names."""
    text = template.format(failure=failure)
    return f"{text} (fields: {fields})" if fields else text


def shown(stored: str) -> str:
    """A stored review text as staff see it: the provider is not named, nor the TODO."""
    return _TODO_MARK.sub("", stored).replace("Stax", "Provider")
