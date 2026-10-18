"""What the screens call things (docs/qa/PLAN-012c-design-pass.md D9, copy list §4).

**Amounts.** :func:`format_amount` is the one way an amount is written for a person to read:
``$2,000.00``, a thousands separator and two decimals, a leading minus for a negative
(``-$2,000.00``, never ``$-``), and a negative that rounds to zero written as ``$0.00``
(§4.8). The template filter ``money`` (``finance/templatetags/money.py``) is this function.
CSV exports keep plain numbers (``ledger.services.csv_export``).

**The processor's name.** The app never names its payment processor in code (docs/CLAUDE.md
Rule 8): a sentence that names it carries :data:`PROVIDER` where the name goes, and the name
is the company settings row's (``CompanySettings.provider_name``, seeded from
``ledger.company_defaults``), so it follows a change of processor. :func:`provider_name`
reads it from the request's row when a request is being answered
(``finance.middleware.ProviderNameMiddleware`` points :func:`reading_from` at it, so the
shell and the sentences share one read) and with one query otherwise (a job, a management
command). :func:`with_provider` fills the name in at once; :func:`provider_sentence` is the
same, lazily, for module-level sentence constants: the name is read when the sentence is
shown, compared or formatted, never at import.

**Choice labels.** A stored choice keeps its value and its label in the migrations; the
screens show the copy list's wording (§4.5), :data:`_SHOWN` per choice set, with the
processor's name filled in: :func:`label` for one value (the models' ``get_<field>_display``
read it), :func:`choices` for a form's list. The Django admin keeps the stored labels (its
look is out of scope).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

from django.db import models
from django.utils.functional import lazy

#: Where a sentence names the payment processor.
PROVIDER = "{provider}"

_reader: ContextVar[Callable[[], str] | None] = ContextVar("provider_name_reader", default=None)


def format_amount(amount: Decimal) -> str:
    """``$1,234.56``; ``-$1,234.56`` for a negative; ``$0.00`` for a negative that rounds
    to zero."""
    digits = f"{abs(amount):,.2f}"
    if amount < 0 and digits != "0.00":
        return f"-${digits}"
    return f"${digits}"


def provider_name() -> str:
    """The processor's display name from the company settings row."""
    reader = _reader.get()
    if reader is not None:
        return reader()
    from ledger.models import CompanySettings

    return CompanySettings.read().provider_name


@contextmanager
def reading_from(reader: Callable[[], str]) -> Iterator[None]:
    """:func:`provider_name` asks ``reader`` while the block runs (one request)."""
    token = _reader.set(reader)
    try:
        yield
    finally:
        _reader.reset(token)


def with_provider(text: str) -> str:
    """``text`` with the processor's name for each :data:`PROVIDER`. When the rest of
    ``text`` still has ``{field}`` places for a later ``str.format``, braces in the name are
    doubled so that the format call writes them as typed."""
    if PROVIDER not in text:
        return text
    name = provider_name()
    if "{" in text.replace(PROVIDER, ""):
        name = name.replace("{", "{{").replace("}", "}}")
    return text.replace(PROVIDER, name)


#: :func:`with_provider`, evaluated when the sentence is used, not when it is defined.
provider_sentence = lazy(with_provider, str)


#: The on-screen label of a stored value where it differs from its choice set's label
#: (copy list §4.5), by choice set (its class name) and stored value; where the stored value
#: is the processor's name (``PaymentChannel``), by the member's name instead (Rule 8).
_SHOWN: dict[str, dict[str, str]] = {
    "ScheduleState": {"none": "Not set up", "active": "On", "ended": "Off"},
    "SyncStatus": {
        "not_applicable": "Not needed",
        "awaiting_link": "Waiting to connect",
        "pending": "In progress",
        "linked": "Connected",
    },
    "SyncFailure": {
        "ProviderError": f"{PROVIDER} error",
        "ValidationFailed": "Not sent",
        "ProviderNotPermittedHere": "Not allowed here",
        "FeatureDisabled": "Turned off",
        "CircuitOpen": "Paused after repeated errors",
        "FailureBudgetExhausted": "Too many errors today",
        "RateLimitWaitExceeded": f"{PROVIDER} is busy, try later",
        "ProviderRejected": f"Rejected by {PROVIDER}",
        "ProviderUnavailable": f"{PROVIDER} unavailable",
        "TransportError": f"Cannot reach {PROVIDER}",
        "IdentityMismatch": f"Wrong {PROVIDER} account",
        "AlreadyLinkedElsewhere": "Connected to another operator",
    },
    "PaymentMethod": {"ach": "Bank debit (ACH)"},
    "MoneyMethod": {"ach": "Bank debit (ACH)"},
    "PaymentChannel": {"PROVIDER_ACH": f"Bank debit through {PROVIDER}"},
    "OneOffCategory": {"nsf_fee": "Returned payment (NSF) fee"},
    "InstallmentCollection": {"email": "Send by e-mail"},
    "CollectionMethod": {"email": "Send by e-mail"},
    "MessageStatus": {"queued": "Waiting", "catch_all": "Sent to the test inbox"},
    "DisputeEvidenceStatus": {"queued": "Waiting"},
    "RentSource": {
        "sheet_current": "From the spreadsheet",
        "sheet_inferred": "From the spreadsheet (inferred)",
        "staff": "Entered by staff",
    },
    "JobKind": {
        "operator_sync": f"Updating the operator in {PROVIDER}",
        "operator_retry": f"Updating the operator in {PROVIDER} again",
        "operator_link": f"Connecting the operator to {PROVIDER}",
        "autopay_enable": f"Setting up autopay in {PROVIDER}",
        "autopay_pause": f"Pausing autopay in {PROVIDER}",
        "autopay_resume": f"Resuming autopay in {PROVIDER}",
        "payment_mark": f"Marking the invoice paid in {PROVIDER}",
        "payment_retry_mark": f"Marking the invoice paid in {PROVIDER} again",
        "payment_refund": f"Refunding the payment through {PROVIDER}",
        "payment_void": f"Voiding the payment through {PROVIDER}",
        "operator_methods_refresh": f"Updating bank methods from {PROVIDER}",
        "run_match": f"Updating invoices from {PROVIDER}",
        "webhook_replay": f"Processing a message from {PROVIDER} again",
        "reconcile_run": "Running the nightly check",
        "one_off_payment_mark": f"Marking the one-off charge's invoice paid in {PROVIDER}",
        "one_off_payment_retry_mark": (
            f"Marking the one-off charge's invoice paid in {PROVIDER} again"
        ),
        "invoice_create": f"Creating the invoice in {PROVIDER}",
        "invoice_send": f"Sending the invoice by e-mail through {PROVIDER}",
        "invoice_delete": f"Cancelling the invoice in {PROVIDER}",
        "schedule_create": f"Setting up autopay in {PROVIDER}",
        "schedule_delete": f"Cancelling autopay in {PROVIDER}",
        "invoice_pay": f"Charging the invoice through {PROVIDER}",
        "message_send": "Sending a collection e-mail",
        "calendar_email": "Sending a morning e-mail",
        "disputes_sync": f"Updating disputes from {PROVIDER}",
        "dispute_evidence_upload": f"Sending dispute evidence to {PROVIDER}",
        "lease_end_documents": "Making the final statement and its e-mail",
        "autopay_stop": f"Turning off autopay in {PROVIDER}",
    },
    "JobStatus": {"queued": "Waiting", "running": "In progress", "dead": "Gave up"},
    "WebhookOutcome": {"dead_letter": "Gave up"},
    "MismatchKind": {
        "deposit_unmatched": "Bank deposit not matched",
        "return_missed_by_webhook": "Return not received",
    },
}


def label(choice_set: type[models.Choices], value: object) -> str:
    """The on-screen label of ``value``: the copy list's wording, else the choice set's
    label, else the value as stored (one the set no longer knows)."""
    shown_for = _SHOWN.get(choice_set.__name__, {})
    shown = shown_for.get(str(value))
    if shown is None:
        try:
            member = choice_set(value)
        except ValueError:
            return str(value)
        shown = shown_for.get(member.name, str(member.label))
    return with_provider(shown)


def choices(choice_set: type[models.Choices]) -> list[tuple[str, str]]:
    """``(value, on-screen label)`` of every choice of ``choice_set``, in its order."""
    return [(str(value), label(choice_set, value)) for value in choice_set.values]
