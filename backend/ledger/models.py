"""Business tables of the Rents ledger (docs/qa/PLAN-004-schema.md §4.1).

Our database owns these rows. Payment state is copied from the payment provider and
never decided here: ``Invoice.provider_status`` is written only by the sync layer
(``payments.sync_writer``), and no provider identifier is stored in this app; those live
in ``payments.ExternalRef`` (docs/CLAUDE.md Rules 2 and 8).

Money is exact: every amount is a :class:`MoneyField` (``numeric(12,2)``) that takes
``Decimal``, ``int`` or a numeric string and refuses anything else. Every enumerated
column has a database CHECK besides its choices. Free text is guarded against card and
account numbers by :mod:`ledger.validators` on save and, on PostgreSQL, by a trigger.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.expressions import Combinable
from django.db.models.functions import Lower, Trim

from ledger.validators import validate_json_has_no_payment_data, validate_no_payment_data

MONEY_MAX_DIGITS = 12
MONEY_DECIMAL_PLACES = 2
ZERO = Decimal("0.00")
#: Half a cent. Equality of sums is checked as "closer than half a cent": on PostgreSQL
#: (exact numeric with two decimal places) that is exact equality; on sqlite, where
#: numbers are stored as inexact binary fractions, it absorbs representation error.
HALF_CENT = Decimal("0.005")

NAME_MAX_LENGTH = 200
CHOICE_MAX_LENGTH = 32
_NOT_BLANK = r"\S"
_PAYMENT_DATA_VALIDATORS = (validate_no_payment_data, validate_json_has_no_payment_data)


# --- Fields and bases --------------------------------------------------------------


if TYPE_CHECKING:
    _DecimalFieldBase = models.DecimalField[Any, Any]
else:  # Model fields are only generic in the type stubs.
    _DecimalFieldBase = models.DecimalField


class StrictDecimalField(_DecimalFieldBase):
    """A DecimalField that accepts only exact input: Decimal, int or a numeric string.

    Django's DecimalField silently converts inexact binary input; this field
    refuses it, and refuses NaN and infinities, which PostgreSQL ``numeric`` would store.
    """

    def to_python(self, value: Any) -> Any:
        if value is None or hasattr(value, "resolve_expression"):
            return value
        if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
            raise ValidationError(
                "Amounts must be Decimal, int or a numeric string.", code="inexact"
            )
        converted = super().to_python(value)
        if converted is not None and not converted.is_finite():
            raise ValidationError("Amounts must be finite.", code="not_finite")
        return converted


class MoneyField(StrictDecimalField):
    """``numeric(12,2)``, the only money type in this schema."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["max_digits"] = MONEY_MAX_DIGITS
        kwargs["decimal_places"] = MONEY_DECIMAL_PLACES
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> Any:
        name, path, args, kwargs = super().deconstruct()
        del kwargs["max_digits"]
        del kwargs["decimal_places"]
        return name, path, args, kwargs


class PaymentDataGuardedModel(models.Model):
    """Runs the payment-data guard on save.

    The guard is declared once, as a validator on each free-text or JSON field; ``save``
    runs exactly those validators, so ``QuerySet.update``, ``bulk_create`` and raw SQL
    bypass it and meet the PostgreSQL trigger instead.
    """

    class Meta:
        abstract = True

    def check_payment_data(self) -> None:
        for field in self._meta.concrete_fields:
            for validator in field.validators:
                if validator in _PAYMENT_DATA_VALIDATORS:
                    validator(getattr(self, field.attname))

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.check_payment_data()
        super().save(*args, **kwargs)


class GuardedModel(PaymentDataGuardedModel):
    """Base for every table here with a UUID primary key."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


def free_text(**kwargs: Any) -> models.TextField[Any, Any]:
    """A free-text column, covered by the card-number guard (PLAN-004 §4.3)."""
    kwargs.setdefault("blank", True)
    return models.TextField(validators=[validate_no_payment_data], **kwargs)


def guarded_char(max_length: int, **kwargs: Any) -> models.CharField[str | int | Combinable, str]:
    """A short text column, covered by the card-number guard (PLAN-004 §4.6 point 1).

    Every text column without choices that can hold 13 characters is guarded, except the
    identifier columns named in §4.6, which are plain ``CharField``/``TextField``.
    """
    return models.CharField(max_length=max_length, validators=[validate_no_payment_data], **kwargs)


def guarded_json(**kwargs: Any) -> models.JSONField[Any, Any]:
    """A JSON column whose serialised text is covered by the card-number guard."""
    return models.JSONField(validators=[validate_json_has_no_payment_data], **kwargs)


def _choice(choices: type[models.TextChoices], **kwargs: Any) -> models.CharField[Any, Any]:
    return models.CharField(max_length=CHOICE_MAX_LENGTH, choices=choices.choices, **kwargs)


def _valid_choice(
    table: str, field: str, choices: type[models.TextChoices], *, blank: bool = False
) -> models.CheckConstraint:
    values = [*choices.values, *([""] if blank else [])]
    return models.CheckConstraint(
        condition=Q(**{f"{field}__in": values}), name=f"{table}_{field}_valid"
    )


def _not_blank(table: str, field: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=Q(**{f"{field}__regex": _NOT_BLANK}), name=f"{table}_{field}_not_blank"
    )


def _positive(table: str, field: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=Q(**{f"{field}__gt": 0}), name=f"{table}_{field}_positive"
    )


def _not_negative(table: str, field: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=Q(**{f"{field}__gte": 0}), name=f"{table}_{field}_not_negative"
    )


def _day_of_month(table: str, field: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=Q(**{f"{field}__isnull": True}) | Q(**{f"{field}__gte": 1, f"{field}__lte": 31}),
        name=f"{table}_{field}_range",
    )


# --- Operators and stores ----------------------------------------------------------


class EnrolmentStatus(models.TextChoices):
    NOT_ENROLLED = "not_enrolled", "Not enrolled"
    ENROLLED = "enrolled", "Enrolled"


class SyncStatus(models.TextChoices):
    """Where an operator stands with the payment provider (PLAN-006 D7)."""

    NOT_APPLICABLE = "not_applicable", "Not applicable"
    AWAITING_LINK = "awaiting_link", "Awaiting link"
    PENDING = "pending", "Pending"
    LINKED = "linked", "Linked"
    FAILED = "failed", "Failed"


class SyncFailure(models.TextChoices):
    """The class name of the last failure, never a message (PLAN-006 D7).

    A closed list, so the column cannot hold free text: the payment-provider error
    classes (``payments.errors``), plus the one local refusal the sync can meet.
    """

    PROVIDER_ERROR = "ProviderError", "Provider error"
    VALIDATION_FAILED = "ValidationFailed", "Refused before sending"
    NOT_PERMITTED_HERE = "ProviderNotPermittedHere", "Not permitted from this process"
    FEATURE_DISABLED = "FeatureDisabled", "Feature disabled"
    CIRCUIT_OPEN = "CircuitOpen", "Calls paused after repeated failures"
    FAILURE_BUDGET_EXHAUSTED = "FailureBudgetExhausted", "Failure budget exhausted"
    RATE_LIMIT_WAIT_EXCEEDED = "RateLimitWaitExceeded", "Rate limit wait exceeded"
    PROVIDER_REJECTED = "ProviderRejected", "Rejected by the provider"
    PROVIDER_UNAVAILABLE = "ProviderUnavailable", "Provider unavailable"
    TRANSPORT_ERROR = "TransportError", "Provider unreachable"
    IDENTITY_MISMATCH = "IdentityMismatch", "Wrong merchant"
    ALREADY_LINKED_ELSEWHERE = "AlreadyLinkedElsewhere", "Linked to another operator"


class Operator(GuardedModel):
    """The business that runs a store and pays its rent ("customer" in older prompts)."""

    display_name = guarded_char(NAME_MAX_LENGTH)
    legal_name = guarded_char(NAME_MAX_LENGTH, blank=True)
    company = guarded_char(NAME_MAX_LENGTH, blank=True)
    email = models.EmailField(null=True, blank=True, validators=[validate_no_payment_data])
    phone = guarded_char(50, blank=True)
    address_line1 = guarded_char(NAME_MAX_LENGTH, blank=True)
    address_line2 = guarded_char(NAME_MAX_LENGTH, blank=True)
    city = guarded_char(100, blank=True)
    state = guarded_char(50, blank=True)
    postal_code = guarded_char(20, blank=True)
    country = models.CharField(max_length=2, default="US")
    notes = free_text()
    enrolment_status = _choice(EnrolmentStatus, default=EnrolmentStatus.NOT_ENROLLED)
    sync_status = _choice(SyncStatus, default=SyncStatus.NOT_APPLICABLE)
    sync_failure = _choice(SyncFailure, blank=True, default="")
    #: Comma-separated key names of a ``ProviderRejected`` body, never values (PLAN-006a).
    sync_failure_fields = free_text(default="")
    sync_attempted_at = models.DateTimeField(null=True, blank=True)
    #: Set once by the create form; a repeated submission finds the row (PLAN-006 D6).
    creation_key = models.UUIDField(unique=True, null=True, blank=True, editable=False)
    #: Set when the provider held several customers with this e-mail (PLAN-006 D3).
    needs_review = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "operators"
        constraints = [
            _not_blank("operators", "display_name"),
            models.CheckConstraint(
                condition=Q(email=Lower(Trim("email"))), name="operators_email_normalised"
            ),
            models.CheckConstraint(condition=~Q(email=""), name="operators_email_not_empty"),
            models.UniqueConstraint(
                fields=["email"],
                condition=Q(email__isnull=False),
                name="operators_email_unique",
            ),
            _valid_choice("operators", "enrolment_status", EnrolmentStatus),
            _valid_choice("operators", "sync_status", SyncStatus),
            _valid_choice("operators", "sync_failure", SyncFailure, blank=True),
        ]

    def __str__(self) -> str:
        return self.display_name

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Stored stripped and lower-case; an empty address is NULL, never ''.
        if self.email is not None:
            self.email = self.email.strip().lower() or None
        super().save(*args, **kwargs)


class StoreStatus(models.TextChoices):
    PROSPECTIVE = "prospective", "Prospective"
    ACTIVE = "active", "Active"
    CLOSED = "closed", "Closed"
    TERMINATED = "terminated", "Terminated"


class Store(GuardedModel):
    external_label = guarded_char(NAME_MAX_LENGTH, db_index=True)
    store_number = models.IntegerField(null=True, blank=True, db_index=True)
    city = guarded_char(100, blank=True)
    state = guarded_char(50, blank=True)
    open_date = models.DateField(null=True, blank=True)
    open_date_note = free_text()
    needs_open_date = models.BooleanField(default=True)
    closed_at = models.DateField(null=True, blank=True)
    status = _choice(StoreStatus)
    needs_review = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stores"
        constraints = [
            models.CheckConstraint(
                condition=Q(needs_open_date=True, open_date__isnull=True)
                | Q(needs_open_date=False, open_date__isnull=False),
                name="stores_needs_open_date_matches",
            ),
            _valid_choice("stores", "status", StoreStatus),
        ]

    def __str__(self) -> str:
        return self.external_label

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.needs_open_date = self.open_date is None
        super().save(*args, **kwargs)


# --- Leases and schedules ----------------------------------------------------------


class CollectionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    PROSPECTIVE = "prospective", "Prospective"
    ENDED = "ended", "Ended"


class ReviewReason(models.TextChoices):
    WIRE = "wire", "Pays by wire"
    DORMANT = "dormant", "Dormant"
    CLOSED = "closed", "Closed"
    UNKNOWN = "unknown", "Unknown"


class PaymentChannel(models.TextChoices):
    PROVIDER_ACH = "stax", "ACH debit through the payment provider"
    WIRE = "wire", "Wire"
    ZELLE = "zelle", "Zelle"
    UNKNOWN = "unknown", "Unknown"
    OTHER = "other", "Other"


class Lease(GuardedModel):
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="leases")
    operator = models.ForeignKey(
        Operator, on_delete=models.PROTECT, null=True, blank=True, related_name="leases"
    )
    debit_day = models.SmallIntegerField(null=True, blank=True)
    second_debit_day = models.SmallIntegerField(null=True, blank=True)
    collection_status = _choice(CollectionStatus)
    review_reason = _choice(ReviewReason, blank=True, default="")
    payment_channel = _choice(PaymentChannel)
    started_on = models.DateField(null=True, blank=True)
    ended_on = models.DateField(null=True, blank=True)
    end_reason = free_text()
    needs_review = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "leases"
        constraints = [
            _day_of_month("leases", "debit_day"),
            _day_of_month("leases", "second_debit_day"),
            models.CheckConstraint(
                condition=Q(second_debit_day__isnull=True)
                | (Q(debit_day__isnull=False) & ~Q(second_debit_day=F("debit_day"))),
                name="leases_second_debit_day_valid",
            ),
            models.CheckConstraint(
                condition=~Q(collection_status="active", payment_channel="unknown"),
                name="leases_active_channel_known",
            ),
            models.CheckConstraint(
                condition=~Q(collection_status="active")
                | Q(debit_day__isnull=False, operator__isnull=False),
                name="leases_active_has_day_and_operator",
            ),
            models.CheckConstraint(
                condition=Q(ended_on__isnull=True)
                | Q(started_on__isnull=True)
                | Q(ended_on__gte=F("started_on")),
                name="leases_ended_after_started",
            ),
            _valid_choice("leases", "collection_status", CollectionStatus),
            _valid_choice("leases", "review_reason", ReviewReason, blank=True),
            _valid_choice("leases", "payment_channel", PaymentChannel),
        ]

    def __str__(self) -> str:
        return f"Lease {self.pk}"


class RentSource(models.TextChoices):
    SHEET_CURRENT = "sheet_current", "Sheet, current rent"
    SHEET_INFERRED = "sheet_inferred", "Sheet, inferred"
    STAFF = "staff", "Staff"


class LeaseRentHistory(GuardedModel):
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name="rent_history")
    amount = MoneyField()
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    source = _choice(RentSource)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "lease_rent_history"
        constraints = [
            _positive("lease_rent_history", "amount"),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True) | Q(effective_to__gt=F("effective_from")),
                name="lease_rent_history_effective_order",
            ),
            models.UniqueConstraint(
                fields=["lease", "effective_from"], name="lease_rent_history_lease_from_unique"
            ),
            models.UniqueConstraint(
                fields=["lease"],
                condition=Q(effective_to__isnull=True),
                name="lease_rent_history_one_open_row",
            ),
            _valid_choice("lease_rent_history", "source", RentSource),
        ]


class ScheduleKind(models.TextChoices):
    RENT = "rent", "Rent"
    INSURANCE = "insurance", "Insurance"


class ScheduleState(models.TextChoices):
    NONE = "none", "Not created"
    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    FAILED = "failed", "Failed"
    ENDED = "ended", "Ended"


class CollectionSchedule(GuardedModel):
    """One row per provider-side collection schedule (PLAN-004 decision D4)."""

    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="schedules")
    kind = _choice(ScheduleKind)
    seq = models.SmallIntegerField()
    debit_day = models.SmallIntegerField(null=True, blank=True)
    amount = MoneyField()
    state = _choice(ScheduleState, default=ScheduleState.NONE)
    #: Set when the lease changed under a provider schedule (rent, debit day, operator):
    #: the schedule is never edited for an amount; staff pause and re-enable (PLAN-007 D5).
    needs_review = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "collection_schedules"
        constraints = [
            models.CheckConstraint(
                condition=Q(seq__in=[1, 2]), name="collection_schedules_seq_valid"
            ),
            _positive("collection_schedules", "amount"),
            _day_of_month("collection_schedules", "debit_day"),
            models.UniqueConstraint(
                fields=["lease", "kind", "seq"],
                condition=~Q(state="ended"),
                name="collection_schedules_live_unique",
            ),
            _valid_choice("collection_schedules", "kind", ScheduleKind),
            _valid_choice("collection_schedules", "state", ScheduleState),
        ]


# --- Charges -----------------------------------------------------------------------


class RentChargeStatus(models.TextChoices):
    DUE = "due", "Due"
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    PARTIAL = "partial", "Partially paid"
    UNPAID = "unpaid", "Unpaid"
    WAIVED = "waived", "Waived"


class RentCharge(GuardedModel):
    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="rent_charges")
    period_month = models.DateField()
    seq = models.SmallIntegerField(default=1)
    due_on = models.DateField()
    amount_due = MoneyField()
    amount_paid = MoneyField(default=ZERO)
    balance = models.GeneratedField(
        expression=F("amount_due") - F("amount_paid"),
        output_field=models.DecimalField(
            max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
        ),
        db_persist=True,
    )
    status = _choice(RentChargeStatus)
    legacy = models.BooleanField(default=False)
    needs_review = models.BooleanField(default=False)
    #: Why the payment provider's invoice was not updated after a manual payment
    #: (PLAN-009 D2, D3): a fixed text written by the rent run, or empty.
    review_message = free_text(default="")
    local_ref = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rent_charges"
        constraints = [
            models.UniqueConstraint(
                fields=["lease", "period_month", "seq"], name="rent_charges_period_unique"
            ),
            models.CheckConstraint(
                condition=Q(period_month__day=1), name="rent_charges_period_first_of_month"
            ),
            models.CheckConstraint(condition=Q(seq__in=[1, 2]), name="rent_charges_seq_valid"),
            _not_negative("rent_charges", "amount_due"),
            _not_negative("rent_charges", "amount_paid"),
            _valid_choice("rent_charges", "status", RentChargeStatus),
        ]


class InsurancePremium(GuardedModel):
    policy_year = models.IntegerField(unique=True)
    amount = MoneyField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "insurance_premiums"
        constraints = [
            models.CheckConstraint(
                condition=Q(policy_year__gte=2000, policy_year__lte=2100),
                name="insurance_premiums_policy_year_range",
            ),
            _positive("insurance_premiums", "amount"),
        ]


class LegacyStatus(models.TextChoices):
    PAID = "paid", "Paid"
    UNPAID = "unpaid", "Unpaid"


class InsuranceCharge(GuardedModel):
    """A lease's yearly insurance charge. Its paid state is derived, never stored (D11)."""

    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="insurance_charges")
    policy_year = models.IntegerField()
    due_on = models.DateField()
    amount = MoneyField(null=True, blank=True)
    needs_premium = models.BooleanField(default=True)
    needs_review = models.BooleanField(default=False)
    legacy = models.BooleanField(default=False)
    legacy_status = _choice(LegacyStatus, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "insurance_charges"
        constraints = [
            models.UniqueConstraint(
                fields=["lease", "policy_year"], name="insurance_charges_lease_year_unique"
            ),
            models.CheckConstraint(
                condition=Q(needs_premium=True, amount__isnull=True)
                | Q(needs_premium=False, amount__isnull=False),
                name="insurance_charges_needs_premium_matches",
            ),
            models.CheckConstraint(
                condition=Q(amount__isnull=True) | Q(amount__gt=0),
                name="insurance_charges_amount_positive",
            ),
            _valid_choice("insurance_charges", "legacy_status", LegacyStatus, blank=True),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.needs_premium = self.amount is None
        super().save(*args, **kwargs)


class ChargeNote(GuardedModel):
    rent_charge = models.ForeignKey(
        RentCharge, on_delete=models.CASCADE, null=True, blank=True, related_name="notes"
    )
    insurance_charge = models.ForeignKey(
        InsuranceCharge, on_delete=models.CASCADE, null=True, blank=True, related_name="notes"
    )
    body = free_text(blank=False)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    author_label = guarded_char(NAME_MAX_LENGTH, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "charge_notes"
        constraints = [
            models.CheckConstraint(
                condition=Q(rent_charge__isnull=False, insurance_charge__isnull=True)
                | Q(rent_charge__isnull=True, insurance_charge__isnull=False),
                name="charge_notes_exactly_one_charge",
            ),
            _not_blank("charge_notes", "body"),
        ]


class AdjustmentReason(models.TextChoices):
    CROSS_STORE_BALANCE = "cross_store_balance", "Cross-store balance"
    CREDIT = "credit", "Credit"
    DISCOUNT = "discount", "Discount"
    CARRY_FORWARD = "carry_forward", "Carry forward"


class ChargeAdjustment(GuardedModel):
    """A one-off difference between charges, invoiced on its own (decision D10)."""

    from_charge = models.ForeignKey(
        RentCharge,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="adjustments_out",
    )
    to_charge = models.ForeignKey(
        RentCharge, on_delete=models.PROTECT, related_name="adjustments_in"
    )
    amount = MoneyField()
    reason = _choice(AdjustmentReason)
    note = free_text(blank=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "charge_adjustments"
        constraints = [
            _positive("charge_adjustments", "amount"),
            _not_blank("charge_adjustments", "note"),
            models.CheckConstraint(
                condition=Q(from_charge__isnull=True) | ~Q(from_charge=F("to_charge")),
                name="charge_adjustments_distinct_charges",
            ),
            models.CheckConstraint(
                condition=~Q(reason="cross_store_balance") | Q(from_charge__isnull=False),
                name="charge_adjustments_cross_store_has_source",
            ),
            _valid_choice("charge_adjustments", "reason", AdjustmentReason),
        ]

    def clean(self) -> None:
        super().clean()
        if self.reason != AdjustmentReason.CROSS_STORE_BALANCE:
            return
        if self.from_charge_id is None or self.to_charge_id is None:
            return  # the database constraint reports a missing source charge
        from_operator = self.from_charge.lease.operator_id if self.from_charge else None
        to_operator = self.to_charge.lease.operator_id
        if from_operator is None or from_operator != to_operator:
            raise ValidationError(
                "A cross-store balance moves between charges of the same operator.",
                code="different_operator",
            )


class StoreBalanceKind(models.TextChoices):
    INSURANCE_OPENING = "insurance_opening", "Insurance opening balance"
    SIGN = "sign", "Sign"
    DEPOSIT_OR_ACCESS = "deposit_or_access", "Deposit or access"


class StoreBalance(GuardedModel):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="balances")
    kind = _choice(StoreBalanceKind)
    #: Signed: positive is owed by the operator, negative is owed to them.
    amount = MoneyField()
    needs_review = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "store_balances"
        constraints = [
            models.UniqueConstraint(fields=["store", "kind"], name="store_balances_kind_unique"),
            _valid_choice("store_balances", "kind", StoreBalanceKind),
        ]


class NoteEntityType(models.TextChoices):
    STORE = "store", "Store"
    OPERATOR = "operator", "Operator"
    LEASE = "lease", "Lease"


class Note(GuardedModel):
    entity_type = _choice(NoteEntityType)
    #: Generic reference, no foreign key; checked by reconciliation (PLAN-004 §7).
    entity_id = models.UUIDField()
    body = free_text(blank=False)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    author_label = guarded_char(NAME_MAX_LENGTH, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notes"
        indexes = [models.Index(fields=["entity_type", "entity_id"], name="notes_entity_idx")]
        constraints = [
            _not_blank("notes", "body"),
            _valid_choice("notes", "entity_type", NoteEntityType),
        ]


# --- Invoices and payments ---------------------------------------------------------

PROVIDER_STATUS_NOTE = (
    "Copied from the payment provider. Written only by the sync layer, never by staff or views."
)


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    READY = "ready", "Ready"
    SENT = "sent", "Sent"
    CLOSED = "closed", "Closed"
    VOID = "void", "Void"


class Invoice(GuardedModel):
    invoice_number = guarded_char(32, unique=True)
    operator = models.ForeignKey(Operator, on_delete=models.PROTECT, related_name="invoices")
    rent_charge = models.ForeignKey(
        RentCharge, on_delete=models.PROTECT, null=True, blank=True, related_name="invoices"
    )
    insurance_charge = models.ForeignKey(
        InsuranceCharge, on_delete=models.PROTECT, null=True, blank=True, related_name="invoices"
    )
    charge_adjustment = models.ForeignKey(
        ChargeAdjustment, on_delete=models.PROTECT, null=True, blank=True, related_name="invoices"
    )
    issue_date = models.DateField()
    due_date = models.DateField()
    memo = free_text()
    subtotal = MoneyField()
    tax = MoneyField(default=ZERO)
    total = MoneyField()
    amount_paid = MoneyField(default=ZERO)
    internal_status = _choice(InvoiceStatus, default=InvoiceStatus.DRAFT)
    # No CHECK on the values: unknown provider strings are stored as they are (Rule 2).
    # The same note is the column comment on PostgreSQL (migration 0002; sqlite has none).
    provider_status = free_text(
        default="",
        editable=False,
        help_text=PROVIDER_STATUS_NOTE,
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    viewed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    allow_card = models.BooleanField(default=False)
    allow_ach = models.BooleanField(default=True)
    creation_key = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    legacy = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "invoices"
        constraints = [
            models.CheckConstraint(
                condition=Q(invoice_number__regex=r"^INV-[0-9]{4}-[0-9]{4,}$"),
                name="invoices_number_format",
            ),
            # One invoice per rent charge (PLAN-008a §2 rules 3, 4).
            models.UniqueConstraint(
                fields=["rent_charge"],
                condition=Q(rent_charge__isnull=False),
                name="invoices_one_per_rent_charge",
            ),
            models.CheckConstraint(
                condition=Q(rent_charge__isnull=True, insurance_charge__isnull=True)
                | Q(rent_charge__isnull=True, charge_adjustment__isnull=True)
                | Q(insurance_charge__isnull=True, charge_adjustment__isnull=True),
                name="invoices_at_most_one_source",
            ),
            models.CheckConstraint(
                condition=Q(total__gt=F("subtotal") + F("tax") - HALF_CENT)
                & Q(total__lt=F("subtotal") + F("tax") + HALF_CENT),
                name="invoices_total_is_subtotal_plus_tax",
            ),
            _not_negative("invoices", "subtotal"),
            _not_negative("invoices", "tax"),
            _not_negative("invoices", "total"),
            _not_negative("invoices", "amount_paid"),
            models.CheckConstraint(
                condition=Q(due_date__gte=F("issue_date")), name="invoices_due_after_issue"
            ),
            models.CheckConstraint(condition=Q(allow_card=False), name="invoices_no_card"),
            _valid_choice("invoices", "internal_status", InvoiceStatus),
        ]

    def __str__(self) -> str:
        return self.invoice_number


class InvoiceLineItem(GuardedModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="line_items")
    position = models.IntegerField()
    description = free_text()
    quantity = StrictDecimalField(max_digits=12, decimal_places=4)
    unit_price = MoneyField()
    line_total = MoneyField()

    class Meta:
        db_table = "invoice_line_items"
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "position"], name="invoice_line_items_position_unique"
            ),
            models.CheckConstraint(
                condition=Q(position__gte=1), name="invoice_line_items_position_positive"
            ),
            _positive("invoice_line_items", "quantity"),
        ]


class PaymentKind(models.TextChoices):
    RENT = "rent", "Rent"
    INSURANCE = "insurance", "Insurance"
    ADJUSTMENT = "adjustment", "Adjustment"


class PaymentMethod(models.TextChoices):
    ACH = "ach", "ACH"
    ZELLE = "zelle", "Zelle"
    WIRE = "wire", "Wire"
    OTHER = "other", "Other"


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SETTLED = "settled", "Settled"
    RETURNED = "returned", "Returned"
    VOIDED = "voided", "Voided"


class Payment(GuardedModel):
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
    )
    rent_charge = models.ForeignKey(
        RentCharge, on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
    )
    insurance_charge = models.ForeignKey(
        InsuranceCharge, on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
    )
    charge_adjustment = models.ForeignKey(
        ChargeAdjustment, on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
    )
    kind = _choice(PaymentKind)
    policy_year = models.IntegerField(null=True, blank=True)
    method = _choice(PaymentMethod)
    amount = MoneyField()
    status = _choice(PaymentStatus)
    is_refund = models.BooleanField(default=False)
    parent_payment = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="refunds"
    )
    initiated_on = models.DateField(null=True, blank=True)
    settled_on = models.DateField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    return_code = guarded_char(16, blank=True)
    return_reason = free_text()
    provider_created_at = models.DateTimeField(null=True, blank=True)
    reference = free_text()
    note = free_text()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    raw_summary = guarded_json(default=dict, blank=True)
    #: Returns queue (PLAN-008 D6): set together, with a note, when staff resolve a
    #: returned payment. Nothing here re-debits.
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    resolution_note = free_text()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payments"
        constraints = [
            _positive("payments", "amount"),
            models.CheckConstraint(
                condition=Q(is_refund=True, parent_payment__isnull=False)
                | Q(is_refund=False, parent_payment__isnull=True),
                name="payments_refund_has_parent",
            ),
            models.CheckConstraint(
                condition=Q(kind="insurance", policy_year__isnull=False)
                | (~Q(kind="insurance") & Q(policy_year__isnull=True)),
                name="payments_policy_year_iff_insurance",
            ),
            # Zelle, wire and other are recorded by hand; the note says what was received.
            models.CheckConstraint(
                condition=~Q(method__in=["zelle", "wire", "other"]) | Q(note__regex=_NOT_BLANK),
                name="payments_manual_method_has_note",
            ),
            models.CheckConstraint(
                condition=Q(resolved_at__isnull=True)
                | (Q(status="returned") & Q(resolution_note__regex=_NOT_BLANK)),
                name="payments_resolved_return_has_note",
            ),
            _valid_choice("payments", "kind", PaymentKind),
            _valid_choice("payments", "method", PaymentMethod),
            _valid_choice("payments", "status", PaymentStatus),
        ]


# --- Reserved: documents and outbound messages (PLAN-006 D8, no UI) -----------------


class DocumentSource(models.TextChoices):
    UPLOAD = "upload", "Upload"
    DOCUSIGN = "docusign", "DocuSign"


class Document(GuardedModel):
    """Reserved for a later prompt: a document held for an operator. No UI, no admin.

    A signing service's envelope identifier is not a column here: provider identifiers
    live in ``payments.ExternalRef`` (docs/CLAUDE.md Rule 8).
    """

    operator = models.ForeignKey(Operator, on_delete=models.PROTECT, related_name="documents")
    kind = free_text(blank=False)
    source = _choice(DocumentSource)
    file_reference = free_text(blank=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "documents"
        constraints = [_valid_choice("documents", "source", DocumentSource)]


class OutboundMessage(GuardedModel):
    """Reserved for a later prompt: a message sent to an operator. No UI, no admin."""

    operator = models.ForeignKey(
        Operator, on_delete=models.PROTECT, related_name="outbound_messages"
    )
    template = free_text(blank=False)
    subject = free_text()
    #: Identifiers of ``documents`` rows attached to the message.
    document_refs = guarded_json(default=list, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    delivery_status = free_text()

    class Meta:
        db_table = "outbound_messages"


# --- Audit and import --------------------------------------------------------------


class AuditLog(PaymentDataGuardedModel):
    """Append-only. On PostgreSQL a trigger rejects every UPDATE and DELETE."""

    id = models.BigAutoField(primary_key=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    action = guarded_char(100)
    entity_type = guarded_char(100)
    #: An identifier, not free text: outside the card-number guard (PLAN-004 §4.6 point 1).
    entity_id = models.TextField()
    before = guarded_json(null=True, blank=True)
    after = guarded_json(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_log"


class LegacyRecord(GuardedModel):
    source_tab = guarded_char(100)
    row_number = models.IntegerField()
    raw = guarded_json()
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "legacy_records"
        constraints = [
            models.UniqueConstraint(
                fields=["source_tab", "row_number"], name="legacy_records_row_unique"
            ),
        ]


class ImportAudit(GuardedModel):
    sheet_row = models.IntegerField()
    sheet_col = guarded_char(16)
    raw_value = free_text()
    target_table = guarded_char(64)
    #: An identifier, not free text: outside the card-number guard (PLAN-004 §4.6 point 1).
    target_id = models.CharField(max_length=64)
    rule_applied = guarded_char(NAME_MAX_LENGTH)
    needs_review = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "import_audit"
        indexes = [
            models.Index(fields=["target_table", "target_id"], name="import_audit_target_idx")
        ]
