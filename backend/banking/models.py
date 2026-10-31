"""The Bank deposits tables (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md I1).

- :class:`BankConnection`: the business's linked bank account, one active row at most. Plaid's
  identifiers of the item and the account, the institution's name and ``mask`` (Plaid's last
  four of the **business's** own account: the only account digits the app keeps, Rule 1), the
  access token **encrypted** with ``PLAID_TOKEN_KEY`` (:mod:`banking.crypto`; never logged,
  never in the Django admin, never on a page, never in ``repr``), the sync cursor, who linked
  it and when, the last sync and the last error's class name. Disconnecting clears the token.
- :class:`BankTransaction`: one credit read from the bank (debits are never kept), by its
  source's ``external_id`` (unique: a second sync adds nothing). Its kind (wire, Zelle, ACH
  credit, check, other), its state (new, confirmed, split, ignored), the suggestion
  :mod:`banking.matching` made, who confirmed or ignored it and why, and the
  ``allocation_group`` of the payments it was recorded as (unique: applied once).
- :class:`SenderAlias`: a sender's name as it appears on deposits, remembered for a lease
  (upper-cased with its whitespace collapsed, unique per lease), for the strong match.

Every free-text column is covered by the card-number guard on save and, on PostgreSQL, by
the trigger of ``ledger.0002_postgresql_guards`` (``banking.0001_initial``). The identifiers
(Plaid's ids, the cursor and the ciphertext) are plain columns, as PLAN-004 §4.6 point 1 has
them.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from ledger.models import (
    NAME_MAX_LENGTH,
    GuardedModel,
    Lease,
    MoneyField,
    free_text,
    guarded_char,
)

CHOICE_MAX_LENGTH = 32
#: The longest description and sender name kept of a bank transaction.
DESCRIPTION_MAX_LENGTH = 500
COUNTERPARTY_MAX_LENGTH = NAME_MAX_LENGTH
ALIAS_MAX_LENGTH = NAME_MAX_LENGTH
#: The exception's class name of the last failed sync, never its message.
ERROR_MAX_LENGTH = 100
IDENTIFIER_MAX_LENGTH = 128
_NOT_BLANK = r"\S"


class Source(models.TextChoices):
    PLAID = "plaid", "Plaid"
    #: Tests and local development: transactions from a fixture.
    FAKE = "fake", "Fake"


class Kind(models.TextChoices):
    """What a credit was, from Plaid's payment channel, payment method and wording."""

    WIRE = "wire", "Wire"
    ZELLE = "zelle", "Zelle"
    ACH_CREDIT = "ach_credit", "ACH credit"
    CHECK = "check", "Check"
    OTHER = "other", "Other"


class Status(models.TextChoices):
    NEW = "new", "New"
    CONFIRMED = "confirmed", "Confirmed"
    #: Recorded over several charges by Split.
    SPLIT = "split", "Split"
    IGNORED = "ignored", "Ignored"


#: The states of a transaction recorded as payments.
APPLIED_STATUSES: tuple[str, ...] = (Status.CONFIRMED.value, Status.SPLIT.value)


class Confidence(models.TextChoices):
    EXACT = "exact", "Exact"
    STRONG = "strong", "Strong"
    WEAK = "weak", "Weak"
    NONE = "none", "None"


class ChargeType(models.TextChoices):
    """The kinds of :func:`ledger.services.receivables.open_balances`."""

    RENT = "rent", "Rent"
    INSURANCE = "insurance", "Insurance"
    ONE_OFF = "one_off", "One-off"


class IgnoreReason(models.TextChoices):
    INTERNAL_TRANSFER = "internal_transfer", "Internal transfer"
    NOT_RENT = "not_rent", "Not rent"
    OTHER = "other", "Other"


def _choice(choices: type[models.TextChoices], **kwargs: object) -> models.CharField[str, str]:
    return models.CharField(
        max_length=CHOICE_MAX_LENGTH,
        choices=choices.choices,
        **kwargs,  # type: ignore[arg-type]
    )


def _valid(
    table: str, field: str, choices: type[models.TextChoices], *, blank: bool = False
) -> models.CheckConstraint:
    values = [*choices.values, *([""] if blank else [])]
    return models.CheckConstraint(
        condition=Q(**{f"{field}__in": values}), name=f"{table}_{field}_valid"
    )


class BankConnection(GuardedModel):
    item_id = models.CharField(max_length=IDENTIFIER_MAX_LENGTH)
    account_id = models.CharField(max_length=IDENTIFIER_MAX_LENGTH)
    institution = guarded_char(NAME_MAX_LENGTH, blank=True, default="")
    #: Plaid's ``mask``: the last (up to) four characters of the business's own account.
    mask = models.CharField(max_length=4, blank=True, default="")
    #: The access token, Fernet-encrypted with ``PLAID_TOKEN_KEY``; empty once disconnected.
    access_token_encrypted = models.TextField(blank=True, default="")
    #: ``/transactions/sync``'s cursor after the last sync; empty before the first.
    cursor = models.TextField(blank=True, default="")
    source = _choice(Source, default=Source.PLAID)
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    linked_at = models.DateTimeField()
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = guarded_char(ERROR_MAX_LENGTH, blank=True, default="")
    active = models.BooleanField(default=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "bank_connections"
        constraints = [
            models.UniqueConstraint(
                fields=["active"], condition=Q(active=True), name="bank_connections_one_active"
            ),
            models.CheckConstraint(
                condition=Q(mask__regex=r"^[0-9A-Za-z]{0,4}$"), name="bank_connections_mask_shape"
            ),
            models.CheckConstraint(
                condition=Q(active=False) | Q(access_token_encrypted__regex=_NOT_BLANK),
                name="bank_connections_active_has_token",
            ),
            _valid("bank_connections", "source", Source),
        ]

    def __str__(self) -> str:
        # Never the token, the cursor or Plaid's identifiers.
        ending = f" ending {self.mask}" if self.mask else ""
        return f"{self.institution or 'Bank account'}{ending}"

    def __repr__(self) -> str:
        return f"<BankConnection {self.pk}>"


class BankTransaction(GuardedModel):
    source = _choice(Source)
    external_id = models.CharField(max_length=IDENTIFIER_MAX_LENGTH, unique=True)
    posted_on = models.DateField()
    #: What came in: always above zero (debits are never kept).
    amount = MoneyField()
    description = guarded_char(DESCRIPTION_MAX_LENGTH, blank=True, default="")
    counterparty = guarded_char(COUNTERPARTY_MAX_LENGTH, blank=True, default="")
    kind = _choice(Kind, default=Kind.OTHER)
    status = _choice(Status, default=Status.NEW)
    confidence = _choice(Confidence, default=Confidence.NONE)
    suggested_lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    suggested_charge_type = _choice(ChargeType, blank=True, default="")
    suggested_charge_id = models.UUIDField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    ignored_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    ignored_at = models.DateTimeField(null=True, blank=True)
    ignore_reason = _choice(IgnoreReason, blank=True, default="")
    #: The words staff gave with "Other" (required then), or with another reason.
    ignore_note = free_text(default="")
    #: The ``allocation_group`` of the payments (and the credit kept) it was recorded as.
    allocation_group = models.UUIDField(null=True, blank=True)
    #: The last time a confirmation or an ignore was undone.
    undone_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "bank_transactions"
        indexes = [
            models.Index(fields=["status", "posted_on"], name="bank_transactions_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="bank_transactions_amount_positive"
            ),
            # Applied once: one transaction per allocation group, and only while it is applied.
            models.UniqueConstraint(
                fields=["allocation_group"],
                condition=Q(allocation_group__isnull=False),
                name="bank_transactions_allocation_group_once",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=APPLIED_STATUSES,
                    allocation_group__isnull=False,
                    confirmed_at__isnull=False,
                )
                | (
                    ~Q(status__in=APPLIED_STATUSES)
                    & Q(allocation_group__isnull=True, confirmed_at__isnull=True)
                ),
                name="bank_transactions_applied_has_group",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=Status.IGNORED) & Q(ignored_at__isnull=False) & ~Q(ignore_reason="")
                )
                | (~Q(status=Status.IGNORED) & Q(ignored_at__isnull=True, ignore_reason="")),
                name="bank_transactions_ignored_has_reason",
            ),
            models.CheckConstraint(
                condition=~Q(ignore_reason=IgnoreReason.OTHER) | Q(ignore_note__regex=_NOT_BLANK),
                name="bank_transactions_other_reason_has_note",
            ),
            models.CheckConstraint(
                condition=Q(suggested_charge_type="", suggested_charge_id__isnull=True)
                | (~Q(suggested_charge_type="") & Q(suggested_charge_id__isnull=False)),
                name="bank_transactions_suggested_charge_pair",
            ),
            _valid("bank_transactions", "source", Source),
            _valid("bank_transactions", "kind", Kind),
            _valid("bank_transactions", "status", Status),
            _valid("bank_transactions", "confidence", Confidence),
            _valid("bank_transactions", "suggested_charge_type", ChargeType, blank=True),
            _valid("bank_transactions", "ignore_reason", IgnoreReason, blank=True),
        ]

    def __str__(self) -> str:
        return f"Bank transaction {self.pk}"


def normalise(text: str) -> str:
    """``text`` upper-cased with every run of whitespace one space, trimmed: how an alias and
    the text it is looked for in are compared."""
    return " ".join(text.split()).upper()


class SenderAlias(GuardedModel):
    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="sender_aliases")
    text = guarded_char(ALIAS_MAX_LENGTH)
    normalised = guarded_char(ALIAS_MAX_LENGTH)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sender_aliases"
        constraints = [
            models.UniqueConstraint(
                fields=["lease", "normalised"], name="sender_aliases_unique_per_lease"
            ),
            models.CheckConstraint(
                condition=Q(normalised__regex=_NOT_BLANK),
                name="sender_aliases_normalised_not_blank",
            ),
        ]

    def __str__(self) -> str:
        return str(self.text)
