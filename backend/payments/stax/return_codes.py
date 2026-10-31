"""The ACH return codes the processor reports, in plain words
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md F1).

:func:`plain_words` is what template F says as ``{return_reason}``: the operator reads why
the bank returned the debit, never the code's Nacha wording. A code not listed here, or no
code at all, reads :data:`FALLBACK`. :data:`RETRYABLE` are the codes whose debit is retried
automatically on the third business day (insufficient and uncollected funds); every other
code needs a new payment method. The rest of the app reads both through
:func:`payments.return_reason_words` and :func:`payments.retryable_return` (Rule 8).
"""

from __future__ import annotations

#: Each code's plain words (the owner's table of F1).
PLAIN_WORDS: dict[str, str] = {
    "R01": "Insufficient funds",
    "R02": "The bank account is closed",
    "R03": "The account number could not be found",
    "R04": "The account number could not be found",
    "R08": "You asked your bank to stop this payment",
    "R09": "Uncollected funds",
    "R10": "Your bank reports this debit was not authorized",
    "R16": "The account is frozen",
    "R29": "Your bank reports this debit was not authorized",
}
#: Any other code, or none.
FALLBACK = "Your bank returned the payment; please contact your bank for details"
#: The codes retried automatically.
RETRYABLE: frozenset[str] = frozenset({"R01", "R09"})


def normalized(code: str | None) -> str:
    """The code as the table keys it: trimmed and upper case (``" r01"`` reads ``R01``)."""
    return (code or "").strip().upper()


def plain_words(code: str | None) -> str:
    """The code's plain words, or :data:`FALLBACK`."""
    return PLAIN_WORDS.get(normalized(code), FALLBACK)


def is_retryable(code: str | None) -> bool:
    """Whether a debit returned with ``code`` is re-debited automatically."""
    return normalized(code) in RETRYABLE
