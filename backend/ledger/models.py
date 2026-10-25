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
from django.core.validators import RegexValidator
from django.db import DatabaseError, models, transaction
from django.db.models import F, Q
from django.db.models.expressions import Combinable
from django.db.models.functions import Length, Lower, Trim
from django.db.models.lookups import Exact
from django.utils import timezone

from ledger import merge
from ledger.validators import validate_json_has_no_payment_data, validate_no_payment_data

MONEY_MAX_DIGITS = 12
MONEY_DECIMAL_PLACES = 2
ZERO = Decimal("0.00")
#: Half a cent. Equality of sums is checked as "closer than half a cent": on PostgreSQL
#: (exact numeric with two decimal places) that is exact equality; on sqlite, where
#: numbers are stored as inexact binary fractions, it absorbs representation error.
HALF_CENT = Decimal("0.005")

NAME_MAX_LENGTH = 200
#: A first or a last name: "First Last" fits ``NAME_MAX_LENGTH`` (PLAN-012f-b D9).
PERSON_NAME_MAX_LENGTH = 99
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


def _at_most_one_set(*fields: str) -> Q:
    """At most one of ``fields`` is not null: every choice of all but one is null."""
    condition = Q()
    for kept in fields:
        condition |= Q(**{f"{field}__isnull": True for field in fields if field != kept})
    return condition


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

    NOT_APPLICABLE = "not_applicable", "Not needed"
    AWAITING_LINK = "awaiting_link", "Waiting to connect"
    PENDING = "pending", "In progress"
    LINKED = "linked", "Connected"
    FAILED = "failed", "Failed"


class SyncFailure(models.TextChoices):
    """The class name of the last failure, never a message (PLAN-006 D7).

    A closed list, so the column cannot hold free text: the payment-provider error
    classes (``payments.errors``), plus the one local refusal the sync can meet.
    """

    PROVIDER_ERROR = "ProviderError", "Error"
    VALIDATION_FAILED = "ValidationFailed", "Not sent"
    NOT_PERMITTED_HERE = "ProviderNotPermittedHere", "Not allowed here"
    FEATURE_DISABLED = "FeatureDisabled", "Turned off"
    CIRCUIT_OPEN = "CircuitOpen", "Paused after repeated errors"
    FAILURE_BUDGET_EXHAUSTED = "FailureBudgetExhausted", "Too many errors today"
    RATE_LIMIT_WAIT_EXCEEDED = "RateLimitWaitExceeded", "Busy, try later"
    PROVIDER_REJECTED = "ProviderRejected", "Rejected"
    PROVIDER_UNAVAILABLE = "ProviderUnavailable", "Unavailable"
    TRANSPORT_ERROR = "TransportError", "Could not be reached"
    IDENTITY_MISMATCH = "IdentityMismatch", "Wrong account"
    ALREADY_LINKED_ELSEWHERE = "AlreadyLinkedElsewhere", "Connected to another operator"


class OperatorKind(models.TextChoices):
    """Whether an operator is a company or a person (PLAN-012f-b D9): it picks the name
    fields the form asks for and the display name derived from them."""

    COMPANY = "company", "Company"
    PERSON = "person", "Person"


#: E.164: a plus sign, a country code that does not start with 0, at most 15 digits in all
#: (PLAN-012a D3). The form, the model and a CHECK hold ``operators.sms_phone`` to it.
SMS_PHONE_PATTERN = r"^\+[1-9][0-9]{7,14}$"
SMS_PHONE_MESSAGE = (
    "Enter the SMS phone in international format: a plus sign, the country code and the "
    "number, digits only, for example +14045551234."
)
CONSENT_IN_FUTURE_MESSAGE = "A consent time cannot be in the future."
#: The consent columns, in the order the form, the page and the CSV show them.
CONSENT_FIELDS: tuple[str, ...] = ("sms_consent_at", "email_consent_at")


class Operator(GuardedModel):
    """The business that runs a store and pays its rent ("customer" in older prompts)."""

    #: The stored, shown name. :meth:`save` derives it from the kind's name fields when
    #: they are filled (PLAN-012f-b D9); a row without them (one made before the kind
    #: existed) keeps the name it has.
    display_name = guarded_char(NAME_MAX_LENGTH)
    legal_name = guarded_char(NAME_MAX_LENGTH, blank=True)
    #: Existing operators are people until staff say otherwise (nothing guessed).
    kind = _choice(OperatorKind, default=OperatorKind.PERSON)
    #: The company name, for a company.
    company = guarded_char(NAME_MAX_LENGTH, blank=True)
    contact_person = guarded_char(NAME_MAX_LENGTH, blank=True, default="")
    first_name = guarded_char(PERSON_NAME_MAX_LENGTH, blank=True, default="")
    last_name = guarded_char(PERSON_NAME_MAX_LENGTH, blank=True, default="")
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
    #: The collection e-mails skip this operator: someone handles it in person
    #: (PLAN-011b D4); the note says who and why.
    handled_personally = models.BooleanField(default=False)
    handled_personally_note = free_text()
    #: Returned debits rescheduled so far (PLAN-011b D5): 0 means the next NSF fee is the
    #: first-return fee. Raised by the Reschedule action only, never by a webhook.
    nsf_return_count = models.IntegerField(default=0)
    #: Every signed statement link issued before this moment is refused (PLAN-011b D4).
    statement_links_revoked_at = models.DateTimeField(null=True, blank=True)
    #: The phone for SMS, in E.164 (PLAN-012a D3). ``phone`` stays what the payment
    #: provider holds. Nothing sends an SMS yet.
    sms_phone = models.CharField(
        max_length=20,
        blank=True,
        default="",
        validators=[
            validate_no_payment_data,
            RegexValidator(SMS_PHONE_PATTERN, SMS_PHONE_MESSAGE, code="sms_phone"),
        ],
    )
    #: When the operator agreed to SMS and to e-mail (PLAN-012a D3); no channel reads
    #: them yet.
    sms_consent_at = models.DateTimeField(null=True, blank=True)
    email_consent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "operators"
        constraints = [
            _not_blank("operators", "display_name"),
            _not_negative("operators", "nsf_return_count"),
            models.CheckConstraint(
                condition=Q(email=Lower(Trim("email"))), name="operators_email_normalised"
            ),
            models.CheckConstraint(condition=~Q(email=""), name="operators_email_not_empty"),
            models.UniqueConstraint(
                fields=["email"],
                condition=Q(email__isnull=False),
                name="operators_email_unique",
            ),
            _valid_choice("operators", "kind", OperatorKind),
            _valid_choice("operators", "enrolment_status", EnrolmentStatus),
            _valid_choice("operators", "sync_status", SyncStatus),
            _valid_choice("operators", "sync_failure", SyncFailure, blank=True),
            models.CheckConstraint(
                condition=Q(sms_phone="") | Q(sms_phone__regex=SMS_PHONE_PATTERN),
                name="operators_sms_phone_e164",
            ),
        ]

    def __str__(self) -> str:
        return self.display_name

    def clean(self) -> None:
        super().clean()
        now = timezone.now()
        errors = {
            name: CONSENT_IN_FUTURE_MESSAGE
            for name in CONSENT_FIELDS
            if (moment := getattr(self, name)) is not None and moment > now
        }
        if errors:
            raise ValidationError(errors, code="consent_in_future")

    def kind_name(self) -> str:
        """The name the kind's fields give: the company name for a company, "First Last"
        for a person; "" while they are empty."""
        if self.kind == OperatorKind.COMPANY:
            return " ".join(self.company.split())
        return " ".join(f"{self.first_name} {self.last_name}".split())

    def derive_display_name(self) -> None:
        """Set ``display_name`` from the kind's name fields when they are filled."""
        if name := self.kind_name():
            self.display_name = name

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.derive_display_name()
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
    #: The street and the ZIP (PLAN-012e-2 D7), guarded as the operator's address is.
    address_line1 = guarded_char(NAME_MAX_LENGTH, blank=True)
    city = guarded_char(100, blank=True)
    #: The two-letter code of :data:`finance.us_states.US_STATES` (PLAN-012f-b D11), or "".
    state = guarded_char(50, blank=True)
    postal_code = guarded_char(20, blank=True)
    open_date = models.DateField(null=True, blank=True)
    open_date_note = free_text()
    needs_open_date = models.BooleanField(default=True)
    closed_at = models.DateField(null=True, blank=True)
    status = _choice(StoreStatus)
    #: The kind of store (PLAN-012f-b D10), from the admin's list; none is shown as nothing.
    store_type = models.ForeignKey(
        "StoreType", on_delete=models.PROTECT, null=True, blank=True, related_name="stores"
    )
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
    PROVIDER_ACH = "stax", "Bank debit"
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
    #: PLAN-012e D2: the first rent charge of a lease that starts after the 1st is for
    #: the days used. Leases billed before the switch existed are never rewritten.
    prorate_first_month = models.BooleanField(default=True)
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
    SHEET_CURRENT = "sheet_current", "From the spreadsheet"
    SHEET_INFERRED = "sheet_inferred", "From the spreadsheet (inferred)"
    STAFF = "staff", "Entered by staff"


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
    NONE = "none", "Not set up"
    ACTIVE = "active", "On"
    PAUSED = "paused", "Paused"
    FAILED = "failed", "Failed"
    ENDED = "ended", "Off"


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
    #: PLAN-012e D1 to D3: the arithmetic of a prorated month ("13–31 Oct, 19 of 31 days
    #: of $2,000.00 = $1,225.81"); null for a full month.
    proration_text = free_text(null=True, default=None)
    #: PLAN-012b-b D5, D6: the invoice builder invoice whose Rent line made or took this
    #: month's charge (the grid names it); kept as history when that invoice is voided.
    billed_by_invoice = models.ForeignKey(
        "Invoice", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    local_ref = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rent_charges"
        indexes = [
            # PLAN-011 D6: the grid and the run page read a lease's charges by month through
            # the left prefix of the unique constraint (lease, period_month, seq).
            # PLAN-011 D6: arrears and the dashboard's overdue read charges by status and due date.
            models.Index(fields=["status", "due_on"], name="rent_charges_status_due_idx"),
        ]
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
    #: What the policy costs the business; null when unknown (PLAN-010b D9). ``amount``
    #: is the price the operator pays.
    cost = MoneyField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "insurance_premiums"
        constraints = [
            models.CheckConstraint(
                condition=Q(policy_year__gte=2000, policy_year__lte=2100),
                name="insurance_premiums_policy_year_range",
            ),
            _positive("insurance_premiums", "amount"),
            models.CheckConstraint(
                condition=Q(cost__isnull=True) | Q(cost__gte=0),
                name="insurance_premiums_cost_not_negative",
            ),
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
        indexes = [
            # PLAN-011 D6: arrears and aging read a lease's insurance charges by due date.
            models.Index(fields=["lease", "due_on"], name="insurance_lease_due_idx"),
        ]
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


# --- Setup fees and one-off charges (PLAN-010b D1 to D3, PLAN-010d D1) ---------------


class FeeType(GuardedModel):
    """One kind of one-time setup item charged to an operator (PLAN-010b D1).

    Edited by an admin in the Django admin. ``refundable`` marks a deposit: money held,
    never revenue. A null default amount means no amount is standard.
    ``show_in_setup`` (PLAN-012d D9) offers the item as a row of the setup checklist (the
    new lease form and the transfer's incoming step); an item saved from the invoice
    builder starts without it.
    """

    code = models.SlugField(max_length=64, unique=True, validators=[validate_no_payment_data])
    name = guarded_char(NAME_MAX_LENGTH)
    default_cost = MoneyField(null=True, blank=True)
    default_price = MoneyField(null=True, blank=True)
    refundable = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    show_in_setup = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fee_types"
        ordering = ("sort_order", "name")
        constraints = [
            _not_blank("fee_types", "name"),
            models.CheckConstraint(
                condition=Q(default_cost__isnull=True) | Q(default_cost__gte=0),
                name="fee_types_default_cost_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(default_price__isnull=True) | Q(default_price__gt=0),
                name="fee_types_default_price_positive",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class StoreType(GuardedModel):
    """A kind of store (PLAN-012f-b D10): Inline store, Kiosk, Cart, Pop-up, Warehouse,
    seeded by ledger migration 0051. Edited by an admin in the Django admin: add and
    change, never delete (a type no longer offered is made inactive). The store form
    offers the active ones; a store's type shows as a grey pill after its name."""

    name = guarded_char(NAME_MAX_LENGTH, unique=True)
    sort_order = models.SmallIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "store_types"
        ordering = ("sort_order", "name")
        constraints = [_not_blank("store_types", "name")]

    def __str__(self) -> str:
        return self.name


class OneOffChargeStatus(models.TextChoices):
    """Derived from settled payments; ``waived`` is set only by the waive action
    (PLAN-010b D3), ``voided`` only once the provider confirmed the cancel (PLAN-010d D6)."""

    OWED = "owed", "Owed"
    PARTIAL = "partial", "Partially paid"
    PAID = "paid", "Paid"
    WAIVED = "waived", "Waived"
    VOIDED = "voided", "Voided"


class OneOffCategory(models.TextChoices):
    """What a one-off charge is for (PLAN-010d D1); a closed list."""

    SETUP = "setup", "Setup"
    ARREARS = "arrears", "Arrears"
    ADJUSTMENT = "adjustment", "Adjustment"
    DAMAGE = "damage", "Damage"
    OTHER = "other", "Other"
    #: The re-collection of a returned debit (PLAN-011b D5); ``source_payment`` names it.
    REDEBIT = "redebit", "Re-debit"
    #: The fee for a returned debit, made with its re-debit (``companion_of``).
    NSF_FEE = "nsf_fee", "Returned payment (NSF) fee"
    #: One installment of a plan (PLAN-012e D9); ``plan`` and ``installment_no`` name it.
    INSTALLMENT = "installment", "Installment"
    #: One occurrence of an autopay plan (PLAN-012f-a D11); ``autopay_plan`` and
    #: ``period_no`` name it.
    AUTOPAY_PLAN = "autopay_plan", "Autopay plan"


class CollectionMethod(models.TextChoices):
    """How a one-off charge is collected (PLAN-010d D1, D2); a closed list. ``none`` is
    the setup charges' flow of PLAN-010b (no invoice is made by the app)."""

    NONE = "none", "None"
    AUTOPAY_DUE = "autopay_due", "Autopay on the due date"
    EMAIL = "email", "Send by e-mail"
    MANUAL = "manual", "Manual payment"
    CHARGE_NOW = "charge_now", "Charge now"


#: Longest description of a one-off charge: the invoice's line item (PLAN-010d D1).
DESCRIPTION_MAX_LENGTH = 120
#: The fewest and most installments of a plan (PLAN-012e D9).
INSTALLMENTS_MIN = 2
INSTALLMENTS_MAX = 12
#: Why a save that changes a stored ``charge_now_key`` is refused (PLAN-010d D4).
CHARGE_NOW_KEY_FIXED = "The Charge now identifier of a one-off charge cannot change once set."
#: The categories other than ``setup``: each CHECK below reads only valid categories, so
#: an unknown one fails its own CHECK (``one_off_charges_category_valid``) alone.
NON_SETUP_CATEGORIES: list[str] = [
    OneOffCategory.ARREARS.value,
    OneOffCategory.ADJUSTMENT.value,
    OneOffCategory.DAMAGE.value,
    OneOffCategory.OTHER.value,
    OneOffCategory.REDEBIT.value,
    OneOffCategory.NSF_FEE.value,
    OneOffCategory.INSTALLMENT.value,
    OneOffCategory.AUTOPAY_PLAN.value,
]
#: The fee types of the NSF fee (PLAN-011b D5): the first return of an operator, then
#: every later one. Seeded inactive, so the setup checklist never offers them.
NSF_FEE_FIRST = "nsf_fee_first"
NSF_FEE_LATER = "nsf_fee_later"
NSF_FEE_CODES: tuple[str, ...] = (NSF_FEE_FIRST, NSF_FEE_LATER)


class OneOffCharge(GuardedModel):
    """A one-time amount charged to an operator, on a lease or on the operator alone
    (PLAN-010b D3 as setup charges; PLAN-010d D1 for every category).

    ``operator`` is the operator billed: always set, copied from the lease when there is
    one, except on a setup charge of a lease that has no operator yet, where the lease's
    operator (when it gets one) is the charge's (:attr:`operator_id_billed`). ``fee_type`` is
    set exactly for category ``setup``; ``description`` is the invoice's line item and is
    required for every other category. ``amount_paid`` is recomputed from settled
    payments (minus settled refunds) like a rent charge's; ``balance`` is computed by
    the database. ``cost`` null means the cost is unknown. A legacy row (the sheet
    import) may sit beside a real charge of the same fee type; otherwise a lease has at
    most one charge per fee type. ``voided`` is written only after the provider
    confirmed the delete of the charge's invoice or schedule (D6).
    """

    operator = models.ForeignKey(
        Operator, on_delete=models.PROTECT, null=True, blank=True, related_name="one_off_charges"
    )
    lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, related_name="one_off_charges"
    )
    fee_type = models.ForeignKey(
        FeeType, on_delete=models.PROTECT, null=True, blank=True, related_name="one_off_charges"
    )
    category = _choice(OneOffCategory, default=OneOffCategory.SETUP)
    description = guarded_char(DESCRIPTION_MAX_LENGTH, blank=True)
    collection = _choice(CollectionMethod, default=CollectionMethod.NONE)
    cost = MoneyField(null=True, blank=True)
    price = MoneyField()
    vendor = guarded_char(NAME_MAX_LENGTH, blank=True)
    due_date = models.DateField()
    amount_paid = MoneyField(default=ZERO)
    balance = models.GeneratedField(
        expression=F("price") - F("amount_paid"),
        output_field=models.DecimalField(
            max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
        ),
        db_persist=True,
    )
    status = _choice(OneOffChargeStatus, default=OneOffChargeStatus.OWED)
    waived_at = models.DateTimeField(null=True, blank=True)
    waived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    waive_note = free_text()
    note = free_text()
    #: Why the payment provider's invoice was not updated after a manual payment, as on
    #: rent charges (PLAN-009 D2, D3): a fixed text, or empty.
    review_message = free_text(default="")
    legacy = models.BooleanField(default=False)
    needs_review = models.BooleanField(default=False)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    void_note = free_text()
    #: The idempotency identifier of the single "Charge now" attempt (D4), set once.
    charge_now_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    #: PLAN-011b D5: the returned payment a ``redebit`` charge collects again (one
    #: re-debit per return), and the ``redebit`` charge an ``nsf_fee`` charge goes with
    #: (one fee per re-debit). A payment on their shared invoice pays the re-debit first.
    source_payment = models.ForeignKey(
        "Payment", on_delete=models.PROTECT, null=True, blank=True, related_name="redebits"
    )
    companion_of = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="companions"
    )
    #: PLAN-012e D9: the plan an ``installment`` charge belongs to, and its place in it
    #: (1 to the plan's count); both set exactly for that category.
    plan = models.ForeignKey(
        "InstallmentPlan",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="installments",
    )
    installment_no = models.SmallIntegerField(null=True, blank=True)
    #: PLAN-012f-a D11: the autopay plan an ``autopay_plan`` charge is an occurrence of,
    #: and its period (1, 2, ...); both set exactly for that category.
    autopay_plan = models.ForeignKey(
        "AutopayPlan",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="occurrences",
    )
    period_no = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "one_off_charges"
        indexes = [
            # PLAN-011 D6: arrears and aging read an operator's open one-off charges.
            models.Index(fields=["operator", "status"], name="one_off_charges_op_status_idx"),
        ]
        constraints = [
            _positive("one_off_charges", "price"),
            models.CheckConstraint(
                condition=Q(cost__isnull=True) | Q(cost__gte=0),
                name="one_off_charges_cost_not_negative",
            ),
            _not_negative("one_off_charges", "amount_paid"),
            models.CheckConstraint(
                condition=Q(status="waived", waived_at__isnull=False, waive_note__regex=_NOT_BLANK)
                | (~Q(status="waived") & Q(waived_at__isnull=True)),
                name="one_off_charges_waived_has_note",
            ),
            models.CheckConstraint(
                condition=Q(status="voided", voided_at__isnull=False, void_note__regex=_NOT_BLANK)
                | (~Q(status="voided") & Q(voided_at__isnull=True)),
                name="one_off_charges_voided_has_note",
            ),
            models.CheckConstraint(
                condition=(~Q(category="setup") | Q(fee_type__isnull=False))
                & (~Q(category__in=NON_SETUP_CATEGORIES) | Q(fee_type__isnull=True)),
                name="one_off_charges_fee_type_iff_setup",
            ),
            models.CheckConstraint(
                condition=~Q(category__in=NON_SETUP_CATEGORIES) | Q(description__regex=_NOT_BLANK),
                name="one_off_charges_description_required",
            ),
            models.CheckConstraint(
                condition=~Q(collection="none", category__in=NON_SETUP_CATEGORIES),
                name="one_off_charges_collection_none_only_setup",
            ),
            models.CheckConstraint(
                condition=Q(operator__isnull=False)
                | (Q(lease__isnull=False) & ~Q(category__in=NON_SETUP_CATEGORIES)),
                name="one_off_charges_has_operator",
            ),
            models.UniqueConstraint(
                fields=["lease", "fee_type"],
                condition=Q(legacy=False),
                name="one_off_charges_lease_fee_type_unique",
            ),
            models.CheckConstraint(
                condition=Q(category="redebit", source_payment__isnull=False)
                | (~Q(category="redebit") & Q(source_payment__isnull=True)),
                name="one_off_charges_source_payment_iff_redebit",
            ),
            models.CheckConstraint(
                condition=Q(category="nsf_fee", companion_of__isnull=False)
                | (~Q(category="nsf_fee") & Q(companion_of__isnull=True)),
                name="one_off_charges_companion_iff_nsf_fee",
            ),
            models.UniqueConstraint(
                fields=["source_payment"], name="one_off_charges_one_redebit_per_payment"
            ),
            models.UniqueConstraint(
                fields=["companion_of"], name="one_off_charges_one_fee_per_redebit"
            ),
            models.CheckConstraint(
                condition=Q(
                    category="installment",
                    plan__isnull=False,
                    installment_no__gte=1,
                    installment_no__lte=INSTALLMENTS_MAX,
                )
                | (~Q(category="installment") & Q(plan__isnull=True, installment_no__isnull=True)),
                name="one_off_charges_plan_iff_installment",
            ),
            models.CheckConstraint(
                condition=Q(
                    category="autopay_plan",
                    collection="autopay_due",
                    autopay_plan__isnull=False,
                    period_no__isnull=False,
                    period_no__gte=1,
                )
                | (
                    ~Q(category="autopay_plan")
                    & Q(autopay_plan__isnull=True, period_no__isnull=True)
                ),
                name="one_off_charges_autopay_plan_iff_category",
            ),
            models.UniqueConstraint(
                fields=["autopay_plan", "period_no"],
                name="one_off_charges_one_per_autopay_period",
            ),
            _valid_choice("one_off_charges", "status", OneOffChargeStatus),
            _valid_choice("one_off_charges", "category", OneOffCategory),
            _valid_choice("one_off_charges", "collection", CollectionMethod),
        ]

    def __str__(self) -> str:
        return f"One-off charge {self.pk}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Refuse a change of ``charge_now_key`` once it is stored (PLAN-010d D4): the
        single "Charge now" attempt and every later one carry the same identifier."""
        update_fields = kwargs.get("update_fields")
        writes_key = update_fields is None or "charge_now_key" in update_fields
        if writes_key and not self._state.adding:
            stored = (
                type(self)
                .objects.filter(pk=self.pk)
                .values_list("charge_now_key", flat=True)
                .first()
            )
            if stored is not None and stored != self.charge_now_key:
                raise ValidationError(CHARGE_NOW_KEY_FIXED, code="charge_now_key_fixed")
        super().save(*args, **kwargs)

    @property
    def operator_id_billed(self) -> Any:
        """The operator billed: the charge's own, else (a setup charge of a lease that
        had no operator when it was made) the lease's."""
        if self.operator_id is not None:
            return self.operator_id
        return self.lease.operator_id if self.lease is not None else None

    @property
    def label(self) -> str:
        """What staff call the charge: its description, else its fee type's name."""
        if self.description:
            return self.description
        return self.fee_type.name if self.fee_type is not None else ""


# --- Installment plans (PLAN-012e D9 to D11) ----------------------------------------


class InstallmentPlanStatus(models.TextChoices):
    """``active`` while installments are open; ``attention`` while one of them has a
    returned payment not resolved; ``completed`` once every installment is paid;
    ``cancelled`` by an admin (the unpaid installments are cancelled)."""

    ACTIVE = "active", "Active"
    ATTENTION = "attention", "Needs attention"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


#: The plan statuses in which the plan, not its parent, carries the balance.
LIVE_PLAN_STATUSES: list[str] = [
    InstallmentPlanStatus.ACTIVE.value,
    InstallmentPlanStatus.ATTENTION.value,
]


class InstallmentCadence(models.TextChoices):
    WEEKLY = "weekly", "Weekly"
    BIWEEKLY = "biweekly", "Every two weeks"
    MONTHLY = "monthly", "Monthly"


class InstallmentCollection(models.TextChoices):
    """How each installment is collected: as its parent was (D9)."""

    AUTOPAY_DUE = "autopay_due", "Autopay on the due date"
    EMAIL = "email", "Send by e-mail"
    MANUAL = "manual", "Manual payment"


class InstallmentPlan(GuardedModel):
    """A plan that splits the open balance of one charge, a rent charge or a one-off
    charge (exactly one), into ``count`` installments (PLAN-012e D9). Each installment is
    a :class:`OneOffCharge` of category ``installment`` (``plan``, ``installment_no``).
    While the plan is live (:data:`LIVE_PLAN_STATUSES`) its parent is left out of every
    open balance and its installments are owed instead. At most one plan per parent
    that is not cancelled."""

    rent_charge = models.ForeignKey(
        RentCharge,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="installment_plans",
    )
    one_off_charge = models.ForeignKey(
        OneOffCharge,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="installment_plans",
    )
    operator = models.ForeignKey(
        Operator, on_delete=models.PROTECT, related_name="installment_plans"
    )
    lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, related_name="installment_plans"
    )
    count = models.SmallIntegerField()
    first_due = models.DateField()
    cadence = _choice(InstallmentCadence)
    collection = _choice(InstallmentCollection)
    status = _choice(InstallmentPlanStatus, default=InstallmentPlanStatus.ACTIVE)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    confirmed_at = models.DateTimeField()
    cancelled_at = models.DateTimeField(null=True, blank=True)
    #: The admin's note of the cancel (D11).
    note = free_text()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "installment_plans"
        constraints = [
            models.CheckConstraint(
                condition=Q(rent_charge__isnull=False, one_off_charge__isnull=True)
                | Q(rent_charge__isnull=True, one_off_charge__isnull=False),
                name="installment_plans_one_parent",
            ),
            models.CheckConstraint(
                condition=Q(count__gte=INSTALLMENTS_MIN, count__lte=INSTALLMENTS_MAX),
                name="installment_plans_count_range",
            ),
            models.CheckConstraint(
                condition=Q(status="cancelled", cancelled_at__isnull=False, note__regex=_NOT_BLANK)
                | (~Q(status="cancelled") & Q(cancelled_at__isnull=True)),
                name="installment_plans_cancelled_has_note",
            ),
            models.UniqueConstraint(
                fields=["rent_charge"],
                condition=~Q(status="cancelled"),
                name="installment_plans_one_per_rent_charge",
            ),
            models.UniqueConstraint(
                fields=["one_off_charge"],
                condition=~Q(status="cancelled"),
                name="installment_plans_one_per_one_off_charge",
            ),
            _valid_choice("installment_plans", "cadence", InstallmentCadence),
            _valid_choice("installment_plans", "collection", InstallmentCollection),
            _valid_choice("installment_plans", "status", InstallmentPlanStatus),
        ]

    def __str__(self) -> str:
        return f"Installment plan {self.pk}"

    @property
    def parent(self) -> RentCharge | OneOffCharge:
        """The charge the plan splits."""
        if self.rent_charge is not None:
            return self.rent_charge
        if self.one_off_charge is None:
            raise ValueError("An installment plan has a parent.")
        return self.one_off_charge


# --- Autopay plans (PLAN-012f-a D11 to D15) -----------------------------------------


class AutopayCadence(models.TextChoices):
    MONTHLY = "monthly", "Monthly"
    WEEKLY = "weekly", "Weekly"
    YEARLY = "yearly", "Yearly"


class AutopayEndRule(models.TextChoices):
    """When a plan ends: after ``end_count`` debits, after ``end_on``, or never."""

    COUNT = "count", "After a number of payments"
    DATE = "date", "On a date"
    NEVER = "never", "Never"


class AutopayPlanState(models.TextChoices):
    """``active`` makes occurrences; ``paused`` makes none until resumed; ``stopped`` is
    final (an admin's stop); ``finished`` once the end rule is met."""

    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    STOPPED = "stopped", "Stopped"
    FINISHED = "finished", "Finished"


#: The plan states that still make, or may again make, an occurrence.
LIVE_AUTOPAY_STATES: list[str] = [AutopayPlanState.ACTIVE.value, AutopayPlanState.PAUSED.value]
#: The fewest and most debits of a plan that ends after a number of payments.
AUTOPAY_COUNT_MIN = 2
AUTOPAY_COUNT_MAX = 999


class AutopayPlan(GuardedModel):
    """One item repeated as bank debits on a lease (PLAN-012f-a D11): monthly, weekly or
    yearly from ``first_on``, ending after ``end_count`` debits, after ``end_on`` or never.
    Each period is a :class:`OneOffCharge` of category ``autopay_plan`` (``autopay_plan``,
    ``period_no``) collected by its own single-occurrence schedule, made by
    :mod:`ledger.services.autopay_plans` ahead of its date. ``next_on`` is the date of the
    next occurrence to make; ``made_count`` the occurrences made and not cancelled by the
    plan. ``fee_type`` (the catalogue item the line named) is for reporting only. At most
    one live plan per lease and item."""

    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="autopay_plans")
    operator = models.ForeignKey(Operator, on_delete=models.PROTECT, related_name="autopay_plans")
    #: The lease's store, kept for the store page's Autopay tab.
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="autopay_plans")
    item_name = guarded_char(NAME_MAX_LENGTH)
    description = free_text()
    quantity = StrictDecimalField(max_digits=12, decimal_places=4)
    unit_amount = MoneyField()
    #: Each debit: the quantity times the unit amount, rounded half up to the cent.
    amount = MoneyField()
    fee_type = models.ForeignKey(
        FeeType, on_delete=models.PROTECT, null=True, blank=True, related_name="autopay_plans"
    )
    cadence = _choice(AutopayCadence)
    first_on = models.DateField()
    end_rule = _choice(AutopayEndRule)
    end_count = models.SmallIntegerField(null=True, blank=True)
    end_on = models.DateField(null=True, blank=True)
    state = _choice(AutopayPlanState, default=AutopayPlanState.ACTIVE)
    next_on = models.DateField()
    made_count = models.IntegerField(default=0)
    #: When staff confirmed the large debit at setup; every occurrence is audited
    #: against it (D13). Null when the amount is not a large debit.
    large_debit_confirmed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "autopay_plans"
        indexes = [
            # The daily run reads the active plans whose next occurrence is near.
            models.Index(fields=["state", "next_on"], name="autopay_plans_state_next_idx"),
        ]
        constraints = [
            _not_blank("autopay_plans", "item_name"),
            _positive("autopay_plans", "quantity"),
            _positive("autopay_plans", "unit_amount"),
            _positive("autopay_plans", "amount"),
            _not_negative("autopay_plans", "made_count"),
            models.CheckConstraint(
                condition=Q(amount__gte=F("quantity") * F("unit_amount") - HALF_CENT)
                & Q(amount__lte=F("quantity") * F("unit_amount") + HALF_CENT),
                name="autopay_plans_amount_is_quantity_times_unit",
            ),
            # Each rule reads only its own shape, so an unknown rule fails its own CHECK
            # (``autopay_plans_end_rule_valid``) alone; ``isnull=False`` because a NULL
            # comparison passes a CHECK.
            models.CheckConstraint(
                condition=(
                    ~Q(end_rule="count")
                    | Q(
                        end_count__isnull=False,
                        end_count__gte=AUTOPAY_COUNT_MIN,
                        end_count__lte=AUTOPAY_COUNT_MAX,
                        end_on__isnull=True,
                    )
                )
                & (
                    ~Q(end_rule="date")
                    | Q(end_on__isnull=False, end_on__gte=F("first_on"), end_count__isnull=True)
                )
                & (~Q(end_rule="never") | Q(end_count__isnull=True, end_on__isnull=True)),
                name="autopay_plans_end_rule_shape",
            ),
            models.CheckConstraint(
                condition=Q(next_on__gte=F("first_on")),
                name="autopay_plans_next_on_from_first",
            ),
            models.UniqueConstraint(
                fields=["lease", "item_name"],
                condition=Q(state__in=LIVE_AUTOPAY_STATES),
                name="autopay_plans_one_live_per_item",
            ),
            _valid_choice("autopay_plans", "cadence", AutopayCadence),
            _valid_choice("autopay_plans", "end_rule", AutopayEndRule),
            _valid_choice("autopay_plans", "state", AutopayPlanState),
        ]

    def __str__(self) -> str:
        return f"Autopay plan {self.pk}"


# --- Store costs (PLAN-010b D8) ------------------------------------------------------


class StoreCostSource(models.TextChoices):
    IMPORT = "import", "Import"
    STAFF = "staff", "Staff"


class StoreCost(GuardedModel):
    """What the business pays the landlord for a store, date-effective like
    ``lease_rent_history`` (PLAN-010b D8).

    ``effective_to`` is the last day a row is in effect (inclusive); the open row has
    none, and a store has at most one. Rows never overlap: a new row closes the open one
    the day before it starts (:mod:`ledger.services.store_costs`). No row on a day means
    the cost is unknown, never zero. ``other_monthly_cost`` null means no other cost.

    **Corrections** (PLAN-010c D10): a row is never edited to say something else; an
    admin's correction writes a new row and marks this one superseded
    (``superseded_at``, ``superseded_by``, and ``change_note`` saying why). A superseded
    row counts for nothing and stays for the history. *Live* rows are the ones with
    ``superseded_at`` null; the open-row and same-start rules hold among live rows only.
    ``change_note`` also records why an admin closed a row.
    """

    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="costs")
    landlord_rent = MoneyField()
    other_monthly_cost = MoneyField(null=True, blank=True)
    other_cost_description = guarded_char(NAME_MAX_LENGTH, blank=True)
    landlord_lease_end = models.DateField(null=True, blank=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    source = _choice(StoreCostSource)
    notes = free_text()
    #: When an admin's correction replaced this row (D10); null on a live row.
    superseded_at = models.DateTimeField(null=True, blank=True)
    #: The row that replaced this one.
    superseded_by = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    #: Why the row was corrected or closed, or why it is a correction (D10).
    change_note = free_text()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "store_costs"
        constraints = [
            _not_negative("store_costs", "landlord_rent"),
            models.CheckConstraint(
                condition=Q(other_monthly_cost__isnull=True) | Q(other_monthly_cost__gte=0),
                name="store_costs_other_monthly_cost_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True) | Q(effective_to__gte=F("effective_from")),
                name="store_costs_effective_order",
            ),
            # Among live rows: one row per start date, and one open row per store.
            models.UniqueConstraint(
                fields=["store", "effective_from"],
                condition=Q(superseded_at__isnull=True),
                name="store_costs_store_from_unique",
            ),
            models.UniqueConstraint(
                fields=["store"],
                condition=Q(effective_to__isnull=True, superseded_at__isnull=True),
                name="store_costs_one_open_row",
            ),
            # A superseded row names its replacement and says why; a live row does neither.
            models.CheckConstraint(
                condition=Q(superseded_at__isnull=True, superseded_by__isnull=True)
                | Q(
                    superseded_at__isnull=False,
                    superseded_by__isnull=False,
                    change_note__regex=_NOT_BLANK,
                ),
                name="store_costs_superseded_complete",
            ),
            models.CheckConstraint(
                condition=~Q(superseded_by=F("id")), name="store_costs_not_superseded_by_itself"
            ),
            _valid_choice("store_costs", "source", StoreCostSource),
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


#: An invoice's total is the sum of these (PLAN-012a D2).
_INVOICE_PARTS = F("subtotal") + F("tax") + F("fees") + F("surcharge")


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
    one_off_charge = models.ForeignKey(
        OneOffCharge, on_delete=models.PROTECT, null=True, blank=True, related_name="invoices"
    )
    issue_date = models.DateField()
    due_date = models.DateField()
    memo = free_text()
    subtotal = MoneyField()
    tax = MoneyField(default=ZERO)
    #: Fees and a surcharge, stored apart from ``subtotal`` (PLAN-012a D2). Always 0
    #: today, and never sent to the payment provider.
    fees = MoneyField(default=ZERO)
    surcharge = MoneyField(default=ZERO)
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
        indexes = [
            # PLAN-011 D6: the dashboard lists invoices whose provider status is ATTEMPTED.
            models.Index(fields=["provider_status"], name="invoices_provider_status_idx"),
        ]
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
            # One invoice per one-off charge (PLAN-010b D6, the rent rule).
            models.UniqueConstraint(
                fields=["one_off_charge"],
                condition=Q(one_off_charge__isnull=False),
                name="invoices_one_per_one_off_charge",
            ),
            models.CheckConstraint(
                condition=_at_most_one_set(
                    "rent_charge", "insurance_charge", "charge_adjustment", "one_off_charge"
                ),
                name="invoices_at_most_one_source",
            ),
            models.CheckConstraint(
                condition=Q(total__gt=_INVOICE_PARTS - HALF_CENT)
                & Q(total__lt=_INVOICE_PARTS + HALF_CENT),
                name="invoices_total_is_sum_of_parts",
            ),
            _not_negative("invoices", "subtotal"),
            _not_negative("invoices", "tax"),
            _not_negative("invoices", "fees"),
            _not_negative("invoices", "surcharge"),
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


class InvoiceLineKind(models.TextChoices):
    """What a line bills (PLAN-012a D1). Only ``charge`` is written today; the others
    are reserved for a later plan."""

    CHARGE = "charge", "Charge"
    FEE = "fee", "Fee"
    SURCHARGE = "surcharge", "Surcharge"
    TAX = "tax", "Tax"
    DISCOUNT = "discount", "Discount"


class InvoiceLineItem(GuardedModel):
    """One line of a local invoice. On an invoice made by the invoice builder
    (PLAN-012 D6) each line names the charge it bills: a one-off charge
    (``one_off_charge``), or for a Rent line the month's rent charge (``rent_charge``,
    PLAN-012b-b D5). A payment on the invoice pays its first charge, then the other lines
    in line order."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="line_items")
    position = models.IntegerField()
    description = free_text()
    #: The line's detail (PLAN-012b D3): on a builder line, the draft line's description;
    #: the provider line carries it as its ``details``.
    details = free_text()
    quantity = StrictDecimalField(max_digits=12, decimal_places=4)
    unit_price = MoneyField()
    line_total = MoneyField()
    one_off_charge = models.ForeignKey(
        OneOffCharge,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoice_lines",
    )
    rent_charge = models.ForeignKey(
        RentCharge,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoice_lines",
    )
    kind = _choice(InvoiceLineKind, default=InvoiceLineKind.CHARGE)

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
            # A charge is billed on one line of one invoice.
            models.UniqueConstraint(
                fields=["one_off_charge"],
                condition=Q(one_off_charge__isnull=False),
                name="invoice_line_items_one_per_one_off_charge",
            ),
            models.CheckConstraint(
                condition=Q(one_off_charge__isnull=True) | Q(rent_charge__isnull=True),
                name="invoice_line_items_one_charge",
            ),
            _valid_choice("invoice_line_items", "kind", InvoiceLineKind),
        ]


# --- The invoice builder (PLAN-012 D6) ------------------------------------------------


class InvoiceDraftStatus(models.TextChoices):
    """A draft is local only until it is sent (or charged now); a cancelled draft was
    never sent. A sent draft names its invoice (``sent_invoice``)."""

    DRAFT = "draft", "Draft"
    SENT = "sent", "Sent"
    CANCELLED = "cancelled", "Cancelled"


class InvoiceDraft(GuardedModel):
    """An invoice being prepared (PLAN-012 D6): an operator, optionally one of its leases
    (checked by :mod:`ledger.services.invoice_builder`), a memo, an optional due date and
    its lines. Nothing of it reaches the payment provider until it is sent, when it
    becomes one one-off charge per line and one local invoice (``sent_invoice``)."""

    operator = models.ForeignKey(Operator, on_delete=models.PROTECT, related_name="invoice_drafts")
    lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, related_name="invoice_drafts"
    )
    memo = free_text()
    due_date = models.DateField(null=True, blank=True)
    status = _choice(InvoiceDraftStatus, default=InvoiceDraftStatus.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    sent_invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, null=True, blank=True, related_name="drafts"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "invoice_drafts"
        constraints = [
            _valid_choice("invoice_drafts", "status", InvoiceDraftStatus),
            models.CheckConstraint(
                condition=Q(status="sent", sent_invoice__isnull=False)
                | (~Q(status="sent") & Q(sent_invoice__isnull=True)),
                name="invoice_drafts_sent_iff_invoice",
            ),
            models.UniqueConstraint(
                fields=["sent_invoice"],
                condition=Q(sent_invoice__isnull=False),
                name="invoice_drafts_one_per_invoice",
            ),
        ]

    def __str__(self) -> str:
        return self.number

    @property
    def number(self) -> str:
        """What staff call the draft before it is sent: ``DRAFT-`` and the first eight
        characters of its id, in capitals."""
        return f"DRAFT-{str(self.pk)[:8].upper()}"


def format_quantity(value: Decimal) -> str:
    """A quantity as staff write it: ``3`` rather than ``3.0000``, ``0.5`` rather than
    ``0.5000`` (never an exponent)."""
    quantity = Decimal(value)
    if quantity == quantity.to_integral_value():
        return str(quantity.quantize(Decimal(1)))
    return str(quantity.normalize())


class InvoiceDraftLine(GuardedModel):
    """One line of a draft: an item, an optional detail (``description``), a quantity and
    a unit amount; ``line_total`` is the quantity times the unit amount rounded to the
    cent (computed by the service, held to it by a CHECK within half a cent).

    ``item`` is the line's text (PLAN-012b D1): a catalogue item's name, then
    ``fee_type`` names it, or a name typed on the spot, then ``fee_type`` is null.

    A **Rent** line (PLAN-012b-b D4) names a lease of the draft's operator and a month
    (``rent_lease``, ``rent_month``, both or neither, the month's first day) and no fee
    type; on send it makes or takes that month's rent charge.

    An **existing-charge** line (PLAN-012d D10) names an open one-off charge of the
    draft's lease (``existing_charge``), with no fee type and no rent; on send it
    attaches that charge to the invoice instead of creating one."""

    draft = models.ForeignKey(InvoiceDraft, on_delete=models.CASCADE, related_name="lines")
    position = models.IntegerField()
    fee_type = models.ForeignKey(
        FeeType, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    item = guarded_char(NAME_MAX_LENGTH)
    description = guarded_char(DESCRIPTION_MAX_LENGTH, blank=True)
    quantity = StrictDecimalField(max_digits=12, decimal_places=4)
    unit_amount = MoneyField()
    line_total = MoneyField()
    rent_lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    rent_month = models.DateField(null=True, blank=True)
    existing_charge = models.ForeignKey(
        OneOffCharge, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "invoice_draft_lines"
        ordering = ("position",)
        constraints = [
            models.UniqueConstraint(
                fields=["draft", "position"], name="invoice_draft_lines_position_unique"
            ),
            models.CheckConstraint(
                condition=Q(position__gte=1), name="invoice_draft_lines_position_positive"
            ),
            _not_blank("invoice_draft_lines", "item"),
            _positive("invoice_draft_lines", "quantity"),
            _positive("invoice_draft_lines", "unit_amount"),
            _positive("invoice_draft_lines", "line_total"),
            models.CheckConstraint(
                condition=Q(line_total__gte=F("quantity") * F("unit_amount") - HALF_CENT)
                & Q(line_total__lte=F("quantity") * F("unit_amount") + HALF_CENT),
                name="invoice_draft_lines_total_is_quantity_times_unit",
            ),
            models.CheckConstraint(
                condition=Q(rent_lease__isnull=True, rent_month__isnull=True)
                | Q(
                    rent_lease__isnull=False,
                    rent_month__isnull=False,
                    rent_month__day=1,
                    fee_type__isnull=True,
                ),
                name="invoice_draft_lines_rent_line",
            ),
            models.CheckConstraint(
                condition=Q(existing_charge__isnull=True)
                | Q(rent_lease__isnull=True, rent_month__isnull=True, fee_type__isnull=True),
                name="invoice_draft_lines_existing_charge",
            ),
        ]

    @property
    def is_rent(self) -> bool:
        return self.rent_lease_id is not None

    @property
    def is_existing(self) -> bool:
        return self.existing_charge_id is not None

    @property
    def quantity_text(self) -> str:
        return format_quantity(self.quantity)


class PaymentKind(models.TextChoices):
    RENT = "rent", "Rent"
    INSURANCE = "insurance", "Insurance"
    ADJUSTMENT = "adjustment", "Adjustment"
    #: A payment on a one-off charge (PLAN-010b D4 as ``setup``; PLAN-010d D1).
    ONE_OFF = "one_off", "One-off charge"
    #: Money paid back to the operator from a deposit; never a provider refund (D4, D7).
    REFUND_DEPOSIT = "refund_deposit", "Deposit refund"
    #: Money paid back to the operator from a credit held on the lease, at the lease end
    #: (PLAN-012d D6b, ADR-0013); never a provider refund.
    CREDIT_REFUND = "credit_refund", "Credit refund"


#: The payment kinds that belong to a one-off charge.
ONE_OFF_PAYMENT_KINDS: list[str] = [PaymentKind.ONE_OFF.value, PaymentKind.REFUND_DEPOSIT.value]
#: Methods recorded by hand, not through the payment provider.
MANUAL_PAYMENT_METHODS: list[str] = ["zelle", "wire", "check", "cash", "other"]


class PaymentMethod(models.TextChoices):
    ACH = "ach", "Bank debit (ACH)"
    ZELLE = "zelle", "Zelle"
    WIRE = "wire", "Wire"
    #: A paper check received by hand (PLAN-012e-3 D1); its reference is the check number.
    CHECK = "check", "Check"
    #: Cash received by hand (PLAN-012f-b D14).
    CASH = "cash", "Cash"
    OTHER = "other", "Other"
    #: A credit held on the lease applied to a charge (PLAN-012d D6a, ADR-0013): no money
    #: moves and no provider is involved.
    CREDIT = "credit", "Credit"


class MoneyMethod(models.TextChoices):
    """The methods money moves by: :class:`PaymentMethod` without ``credit`` (a deposit
    resolution's method, PLAN-010b D7)."""

    ACH = "ach", "Bank debit (ACH)"
    ZELLE = "zelle", "Zelle"
    WIRE = "wire", "Wire"
    #: A paper check received by hand (PLAN-012e-3 D1); its reference is the check number.
    CHECK = "check", "Check"
    #: Cash received by hand (PLAN-012f-b D14).
    CASH = "cash", "Cash"
    OTHER = "other", "Other"


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SETTLED = "settled", "Settled"
    RETURNED = "returned", "Returned"
    VOIDED = "voided", "Voided"


class ResolutionKind(models.TextChoices):
    """How staff resolved a returned payment (PLAN-008 D6, PLAN-011b D5)."""

    NOTE = "note", "Resolved with a note"
    RESCHEDULED = "rescheduled", "Rescheduled"


class LeaseCredit(GuardedModel):
    """Money received for a lease and not applied to a charge (PLAN-012d D6a, D6b;
    ADR-0013): a liability to the operator, never revenue. ``remaining`` falls as the
    credit is applied to charges (payments of method ``credit``) or paid back at the
    lease end (a ``credit_refund`` payment). Written only by
    :mod:`ledger.services.credits` and :mod:`ledger.services.payments_entry`."""

    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="credits")
    amount = MoneyField()
    remaining = MoneyField()
    method = _choice(MoneyMethod)
    received_on = models.DateField()
    reference = free_text()
    note = free_text()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    #: The payment entry that kept it (:attr:`Payment.allocation_group`).
    allocation_group = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "lease_credits"
        constraints = [
            _positive("lease_credits", "amount"),
            _not_negative("lease_credits", "remaining"),
            models.CheckConstraint(
                condition=Q(remaining__lte=F("amount")),
                name="lease_credits_remaining_at_most_amount",
            ),
            models.CheckConstraint(
                # With ``lease_credits_method_valid``: Zelle, wire, check or other, never ACH.
                condition=~Q(method=MoneyMethod.ACH.value),
                name="lease_credits_method_manual",
            ),
            _not_blank("lease_credits", "note"),
            _valid_choice("lease_credits", "method", MoneyMethod),
        ]

    def __str__(self) -> str:
        return f"Lease credit {self.pk}"


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
    one_off_charge = models.ForeignKey(
        OneOffCharge, on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
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
    #: PLAN-011b D5: ``note`` (resolved with a note) or ``rescheduled`` (a re-debit on
    #: ``redebit_date``, a New York date); empty while unresolved.
    resolution_kind = _choice(ResolutionKind, blank=True, default="")
    redebit_date = models.DateField(null=True, blank=True)
    #: PLAN-012d D6a: the payments of one entry of the store page's payment form, one per
    #: charge it paid, share this value (the kept credit too); null otherwise.
    allocation_group = models.UUIDField(null=True, blank=True)
    #: The credit a ``credit`` payment applied, or a ``credit_refund`` paid back.
    credit = models.ForeignKey(
        LeaseCredit, on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payments"
        indexes = [
            # PLAN-011 D6: the dashboard's "paid this month" reads settled payments by date.
            models.Index(fields=["settled_on", "status"], name="payments_settled_status_idx"),
        ]
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
            # Zelle, wire, check and other are recorded by hand; the note says what was
            # received.
            models.CheckConstraint(
                condition=~Q(method__in=MANUAL_PAYMENT_METHODS) | Q(note__regex=_NOT_BLANK),
                name="payments_manual_method_has_note",
            ),
            # PLAN-010b D4: a one-off charge is paid only by kind one_off (or paid back
            # by refund_deposit), and those kinds always name it and nothing else.
            models.CheckConstraint(
                condition=Q(kind__in=ONE_OFF_PAYMENT_KINDS, one_off_charge__isnull=False)
                | (~Q(kind__in=ONE_OFF_PAYMENT_KINDS) & Q(one_off_charge__isnull=True)),
                name="payments_one_off_kind_iff_one_off_charge",
            ),
            models.CheckConstraint(
                condition=Q(one_off_charge__isnull=True)
                | Q(rent_charge__isnull=True, insurance_charge__isnull=True)
                & Q(charge_adjustment__isnull=True),
                name="payments_one_off_charge_only_target",
            ),
            # Money out by hand: never a refund row, never of a provider transaction.
            models.CheckConstraint(
                condition=~Q(kind="refund_deposit")
                | Q(
                    is_refund=False,
                    parent_payment__isnull=True,
                    method__in=MANUAL_PAYMENT_METHODS,
                ),
                name="payments_refund_deposit_manual",
            ),
            models.CheckConstraint(
                condition=Q(resolved_at__isnull=True)
                | (Q(status="returned") & Q(resolution_note__regex=_NOT_BLANK)),
                name="payments_resolved_return_has_note",
            ),
            models.CheckConstraint(
                condition=Q(resolved_at__isnull=True, resolution_kind="")
                | (Q(resolved_at__isnull=False) & ~Q(resolution_kind="")),
                name="payments_resolution_kind_iff_resolved",
            ),
            models.CheckConstraint(
                condition=Q(resolution_kind="rescheduled", redebit_date__isnull=False)
                | (~Q(resolution_kind="rescheduled") & Q(redebit_date__isnull=True)),
                name="payments_redebit_date_iff_rescheduled",
            ),
            # PLAN-012d D6a, D6b: a credit payment names its credit; only a credit
            # payment or a credit refund names one.
            models.CheckConstraint(
                condition=~Q(method="credit") | Q(credit__isnull=False, is_refund=False),
                name="payments_credit_method_has_credit",
            ),
            models.CheckConstraint(
                condition=Q(credit__isnull=True) | Q(method="credit") | Q(kind="credit_refund"),
                name="payments_credit_only_credit_rows",
            ),
            # A credit refund is money paid back by hand, of a credit and nothing else.
            models.CheckConstraint(
                condition=~Q(kind="credit_refund")
                | Q(
                    credit__isnull=False,
                    is_refund=False,
                    parent_payment__isnull=True,
                    method__in=MANUAL_PAYMENT_METHODS,
                    rent_charge__isnull=True,
                    insurance_charge__isnull=True,
                    charge_adjustment__isnull=True,
                    one_off_charge__isnull=True,
                ),
                name="payments_credit_refund_shape",
            ),
            _valid_choice("payments", "resolution_kind", ResolutionKind, blank=True),
            _valid_choice("payments", "kind", PaymentKind),
            _valid_choice("payments", "method", PaymentMethod),
            _valid_choice("payments", "status", PaymentStatus),
        ]


# --- Deposit resolution (PLAN-010b D7) ---------------------------------------------


class DepositResolutionKind(models.TextChoices):
    """What happened to a deposit at the end of its lease (PLAN-010b D7)."""

    REFUNDED = "refunded", "Refunded"
    WITHHELD = "withheld", "Withheld"
    TRANSFERRED = "transferred", "Transferred"


class DepositResolution(GuardedModel):
    """How a deposit (a setup charge of a refundable fee type) was resolved once its
    lease ended (PLAN-010b D7). One per deposit. ``setup_charge`` names a one-off charge
    of category ``setup`` (PLAN-010d D1 kept this column's name).

    ``amount`` is what was refunded, transferred or withheld. ``withheld_amount`` is the
    part of the paid deposit the business keeps: the paid amount less ``amount`` for a
    refund or a transfer, ``amount`` itself when withheld. ``method`` is set for a refund
    only, ``to_lease`` for a transfer only.
    """

    setup_charge = models.OneToOneField(
        OneOffCharge, on_delete=models.PROTECT, related_name="deposit_resolution"
    )
    kind = _choice(DepositResolutionKind)
    amount = MoneyField()
    withheld_amount = MoneyField(default=ZERO)
    #: PLAN-012e D5(d): the part of ``withheld_amount`` applied to the lease's open
    #: charges as "deposit offset" payments. Those payments are the revenue; this part
    #: is not counted again as a withheld deposit.
    offset_amount = MoneyField(default=ZERO)
    resolved_on = models.DateField()
    method = _choice(MoneyMethod, blank=True, default="")
    note = free_text(blank=False)
    to_lease = models.ForeignKey(
        Lease,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="deposits_received",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "deposit_resolutions"
        constraints = [
            _positive("deposit_resolutions", "amount"),
            _not_negative("deposit_resolutions", "withheld_amount"),
            models.CheckConstraint(
                condition=Q(offset_amount=0)
                | (Q(offset_amount__gt=0) & Q(offset_amount__lte=F("withheld_amount"))),
                name="deposit_resolutions_offset_within_withheld",
            ),
            _not_blank("deposit_resolutions", "note"),
            # A refund names how it was paid back, never through the provider; the other
            # kinds name no method. (Each rule reads only valid kinds, so an unknown kind
            # or method fails its own CHECK.)
            models.CheckConstraint(
                condition=(~Q(kind="refunded") | ~Q(method__in=["", PaymentMethod.ACH.value]))
                & (~Q(kind__in=["withheld", "transferred"]) | Q(method="")),
                name="deposit_resolutions_method_iff_refunded",
            ),
            models.CheckConstraint(
                condition=(~Q(kind="transferred") | Q(to_lease__isnull=False))
                & (~Q(kind__in=["refunded", "withheld"]) | Q(to_lease__isnull=True)),
                name="deposit_resolutions_to_lease_iff_transferred",
            ),
            _valid_choice("deposit_resolutions", "kind", DepositResolutionKind),
            _valid_choice("deposit_resolutions", "method", MoneyMethod, blank=True),
        ]


# --- Documents (PLAN-010c D4) --------------------------------------------------------


class DocumentSource(models.TextChoices):
    UPLOAD = "upload", "Upload"
    #: Reserved; unused (PLAN-010c §2).
    DOCUSIGN = "docusign", "DocuSign"


class DocumentKind(models.TextChoices):
    LEASE_AGREEMENT = "lease_agreement", "Lease agreement"
    AMENDMENT = "amendment", "Amendment"
    FLOOR_PLAN = "floor_plan", "Floor plan"
    PHOTO = "photo", "Photo"
    NOTICE = "notice", "Notice"
    OTHER = "other", "Other"
    #: PLAN-012 D8: a file sent to the payment provider for a dispute; kept on the
    #: dispute's operator, and the only kind an operator keeps.
    DISPUTE_EVIDENCE = "dispute_evidence", "Dispute evidence"
    #: PLAN-012e D5, D7: the PDF statement of a lease end, made by the app and kept on
    #: the store only.
    FINAL_STATEMENT = "final_statement", "Final statement"


class DocumentContentType(models.TextChoices):
    """The stored type, decided from the file's content, never from the browser
    (PLAN-010c D5)."""

    PDF = "application/pdf", "PDF"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word"
    JPEG = "image/jpeg", "JPEG image"
    PNG = "image/png", "PNG image"
    HEIC = "image/heic", "HEIC image"
    #: PLAN-012 D8: accepted for dispute evidence only.
    TIFF = "image/tiff", "TIFF image"


#: Longest stored filename (PLAN-010c D5).
DOCUMENT_FILENAME_MAX_LENGTH = 150


#: Lower-case hexadecimal SHA-256: exactly 64 characters, nothing around them.
SHA256_HEX = r"\A[0-9a-f]{64}\Z"


class Document(GuardedModel):
    """A file kept with a store or a lease (PLAN-010c D4), or dispute evidence kept with
    an operator (PLAN-012 D8).

    The bytes live in Blob Storage at ``blob_path`` (``documents/<store|lease|operator>/
    <owner uuid>/<document uuid>``, never the filename), never in the database or on the
    container's disk. Rows are never deleted: an admin's delete sets ``deleted_at``,
    ``deleted_by`` and ``delete_note``, and the blob stays. Written only by
    :mod:`ledger.services.documents`.
    """

    store = models.ForeignKey(
        Store, on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )
    lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )
    operator = models.ForeignKey(
        Operator, on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )
    kind = _choice(DocumentKind)
    source = _choice(DocumentSource, default=DocumentSource.UPLOAD)
    #: Sanitised (base name, no control or path characters), and guarded.
    original_filename = guarded_char(DOCUMENT_FILENAME_MAX_LENGTH)
    content_type = models.CharField(max_length=100, choices=DocumentContentType.choices)
    size_bytes = models.BigIntegerField()
    #: Built from UUIDs only, never the filename. Guarded all the same: the guard reads
    #: a UUID as one letter, so a real path always passes.
    blob_path = guarded_char(200, unique=True)
    #: Lower-case hexadecimal SHA-256 of the bytes. An identifier, not free text, and
    #: so outside the guard (PLAN-004 §4.6 point 1, like ``webhook_events.dedupe_hash``):
    #: random hexadecimal holds a Luhn-valid run of 13 digits often enough to matter.
    sha256 = models.CharField(max_length=64)
    note = free_text()
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    delete_note = free_text()

    class Meta:
        db_table = "documents"
        constraints = [
            models.CheckConstraint(
                condition=Q(store__isnull=False, lease__isnull=True, operator__isnull=True)
                | Q(store__isnull=True, lease__isnull=False, operator__isnull=True)
                | Q(store__isnull=True, lease__isnull=True, operator__isnull=False),
                name="documents_exactly_one_owner",
            ),
            # An operator keeps dispute evidence only, and dispute evidence is kept only
            # by an operator (PLAN-012 D8).
            models.CheckConstraint(
                condition=Q(operator__isnull=True) ^ Q(kind=DocumentKind.DISPUTE_EVIDENCE),
                name="documents_operator_keeps_dispute_evidence",
            ),
            # A final statement is kept by a store only (PLAN-012e D5).
            models.CheckConstraint(
                condition=~Q(kind=DocumentKind.FINAL_STATEMENT) | Q(store__isnull=False),
                name="documents_final_statement_on_store",
            ),
            _valid_choice("documents", "kind", DocumentKind),
            _valid_choice("documents", "source", DocumentSource),
            _valid_choice("documents", "content_type", DocumentContentType),
            _positive("documents", "size_bytes"),
            _not_blank("documents", "original_filename"),
            # A SHA-256 digest's shape. ``\A`` and ``\Z`` (not ``^`` and ``$``): on sqlite
            # the check runs Python's ``re.search``, where ``$`` also matches before a
            # final line break; both engines read ``\A`` and ``\Z`` as the string's ends.
            models.CheckConstraint(
                condition=Q(sha256__regex=SHA256_HEX), name="documents_sha256_hex"
            ),
            # A deleted row says why.
            models.CheckConstraint(
                condition=Q(deleted_at__isnull=True) | Q(delete_note__regex=_NOT_BLANK),
                name="documents_deleted_has_note",
            ),
            # The same file once per record among live rows (D4).
            models.UniqueConstraint(
                fields=["store", "sha256"],
                condition=Q(deleted_at__isnull=True, store__isnull=False),
                name="documents_store_sha256_live_unique",
            ),
            models.UniqueConstraint(
                fields=["lease", "sha256"],
                condition=Q(deleted_at__isnull=True, lease__isnull=False),
                name="documents_lease_sha256_live_unique",
            ),
            models.UniqueConstraint(
                fields=["operator", "sha256"],
                condition=Q(deleted_at__isnull=True, operator__isnull=False),
                name="documents_operator_sha256_live_unique",
            ),
        ]

    def __str__(self) -> str:
        return self.original_filename


# --- Disputes (PLAN-012 D8) ----------------------------------------------------------

#: Longest dispute status or reason kept (the provider's short enums, Rule 2).
DISPUTE_TEXT_MAX_LENGTH = 64
#: Longest evidence name (the provider's ``name`` field).
EVIDENCE_NAME_MAX_LENGTH = 150


class Dispute(GuardedModel):
    """A dispute the payment provider reports (PLAN-012 D8), read only.

    Written only by :mod:`ledger.services.disputes` from the provider's list. The
    provider's identifier lives in ``external_refs`` (``external_kind`` ``dispute``);
    ``status`` and ``reason`` are the provider's strings, verbatim and without a CHECK
    (Rule 2). ``payment`` is ours when we hold the disputed transaction; ``operator`` and
    ``store`` are derived from it at each sync. ``raw_summary`` keeps the documented
    scalar fields only, the transaction's identifier masked (Rule 1).
    """

    payment = models.ForeignKey(
        "Payment", on_delete=models.PROTECT, null=True, blank=True, related_name="disputes"
    )
    operator = models.ForeignKey(
        Operator, on_delete=models.PROTECT, null=True, blank=True, related_name="disputes"
    )
    store = models.ForeignKey(
        Store, on_delete=models.PROTECT, null=True, blank=True, related_name="disputes"
    )
    reason = guarded_char(DISPUTE_TEXT_MAX_LENGTH, blank=True)
    amount = MoneyField(null=True, blank=True)
    status = guarded_char(DISPUTE_TEXT_MAX_LENGTH, blank=True)
    respond_by = models.DateTimeField(null=True, blank=True)
    provider_created_at = models.DateTimeField(null=True, blank=True)
    #: When the provider last changed the dispute (its ``updated_at``).
    changed_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField()
    raw_summary = guarded_json(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "disputes"
        indexes = [models.Index(fields=["respond_by"], name="disputes_respond_by_idx")]

    def __str__(self) -> str:
        return f"Dispute {self.status}"


class DisputeEvidenceStatus(models.TextChoices):
    QUEUED = "queued", "Waiting"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class DisputeEvidence(GuardedModel):
    """One evidence file for a dispute (PLAN-012 D8): the document it is stored as (on
    the dispute's operator, kind ``dispute_evidence``) and its upload to the provider by
    the ``dispute_evidence_upload`` job. The provider's file identifier lives in
    ``external_refs`` (``dispute_file``); ``error`` holds a failure's class name only."""

    dispute = models.ForeignKey(Dispute, on_delete=models.PROTECT, related_name="evidence")
    document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name="dispute_evidence"
    )
    name = guarded_char(EVIDENCE_NAME_MAX_LENGTH)
    status = _choice(DisputeEvidenceStatus, default=DisputeEvidenceStatus.QUEUED)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = guarded_char(100, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dispute_evidence"
        constraints = [
            _valid_choice("dispute_evidence", "status", DisputeEvidenceStatus),
            _not_blank("dispute_evidence", "name"),
            models.CheckConstraint(
                condition=Q(status=DisputeEvidenceStatus.SENT, sent_at__isnull=False)
                | ~Q(status=DisputeEvidenceStatus.SENT),
                name="dispute_evidence_sent_has_time",
            ),
        ]

    def __str__(self) -> str:
        return self.name


# --- Collections ladder and outbound e-mail (PLAN-011b D4) ---------------------------


#: Longest template subject (PLAN-011b D4).
TEMPLATE_SUBJECT_MAX_LENGTH = 200
#: The latest day a ladder step may fall on.
STEP_MAX_DAY_OFFSET = 365


class MessageTemplate(GuardedModel):
    """An e-mail's subject and text (PLAN-011b D4, PLAN-012e D8), edited in the Django admin.

    The subject and the body may use the merge fields of :mod:`ledger.merge` for the
    template's code only (:func:`ledger.merge.fields_for`); any
    other field, attribute or index access, conversion or format specification is
    refused on save (:meth:`clean` and :meth:`save`) and never rendered.
    """

    code = models.SlugField(max_length=64, unique=True, validators=[validate_no_payment_data])
    subject = guarded_char(TEMPLATE_SUBJECT_MAX_LENGTH)
    body = free_text(blank=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "message_templates"
        ordering = ("code",)
        constraints = [
            _not_blank("message_templates", "subject"),
            _not_blank("message_templates", "body"),
            models.CheckConstraint(
                condition=Q(code__regex=r"^[a-z0-9_]+$"), name="message_templates_code_shape"
            ),
        ]

    def __str__(self) -> str:
        return self.code

    def merge_field_errors(self) -> dict[str, list[str]]:
        fields = merge.fields_for(self.code or "")
        found = {
            name: merge.problems(getattr(self, name) or "", fields) for name in ("subject", "body")
        }
        return {name: texts for name, texts in found.items() if texts}

    def clean(self) -> None:
        super().clean()
        errors = self.merge_field_errors()
        if errors:
            raise ValidationError(errors)

    def save(self, *args: Any, **kwargs: Any) -> None:
        errors = self.merge_field_errors()
        if errors:
            raise ValidationError(errors)
        super().save(*args, **kwargs)


class CollectionStep(GuardedModel):
    """One rung of the collections ladder (PLAN-011b D4): ``day_offset`` days after a
    charge's due date (New York days), its template is sent, once per charge. Edited in
    the Django admin; a step no longer used is made inactive, never deleted."""

    day_offset = models.PositiveSmallIntegerField()
    template = models.ForeignKey(MessageTemplate, on_delete=models.PROTECT, related_name="steps")
    active = models.BooleanField(default=True)
    position = models.SmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "collection_steps"
        ordering = ("day_offset", "position")
        constraints = [
            models.CheckConstraint(
                condition=Q(day_offset__gte=1, day_offset__lte=STEP_MAX_DAY_OFFSET),
                name="collection_steps_day_offset_range",
            ),
            models.UniqueConstraint(fields=["day_offset"], name="collection_steps_day_unique"),
        ]

    def __str__(self) -> str:
        return f"Day {self.day_offset}"


class MessageStatus(models.TextChoices):
    """What became of an outbound message (PLAN-011b D4); a closed list."""

    QUEUED = "queued", "Waiting"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SUPPRESSED = "suppressed", "Suppressed"
    CATCH_ALL = "catch_all", "Sent to the test inbox"


class ChargeEntityType(models.TextChoices):
    """What an outbound message is about: a charge of the collections ladder, or a lease
    end whose final statement it carries (PLAN-012e D8)."""

    RENT_CHARGE = "rent_charge", "Rent charge"
    ONE_OFF_CHARGE = "one_off_charge", "One-off charge"
    LEASE_END = "lease_end", "Lease end"


class MessageKind(models.TextChoices):
    """Why an outbound message is sent (PLAN-012e D8); a closed list."""

    LADDER = "ladder", "Collections ladder"
    FINAL_STATEMENT = "final_statement", "Final statement"


class OutboundMessage(GuardedModel):
    """One e-mail to an operator (PLAN-011b D4, PLAN-012e D8): a collection e-mail about
    one charge (``kind`` ``ladder``, with its ``step``), or the final statement of a lease
    end (``kind`` ``final_statement``, no step, the PDF in ``document``).

    The subject is named generically (``charge_entity_type``, ``charge_entity_id``), as
    audit rows name their entity. One ladder row per (charge, step): a hand send of a
    step already logged is a second row marked ``manual``, made only on an explicit
    confirmation; one final statement row per lease end. ``subject`` and ``body_sha256``
    are written when the message is rendered for sending; the body itself is never
    stored. ``error`` is a class name.
    """

    operator = models.ForeignKey(
        Operator, on_delete=models.PROTECT, related_name="outbound_messages"
    )
    template = models.ForeignKey(MessageTemplate, on_delete=models.PROTECT, related_name="+")
    kind = _choice(MessageKind, default=MessageKind.LADDER)
    step = models.ForeignKey(
        CollectionStep, on_delete=models.PROTECT, null=True, blank=True, related_name="messages"
    )
    charge_entity_type = _choice(ChargeEntityType)
    charge_entity_id = models.UUIDField()
    #: The attachment of a final statement (PLAN-012e D8); null otherwise.
    document = models.ForeignKey(
        Document, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    to_address = models.EmailField(validators=[validate_no_payment_data])
    subject = free_text()
    #: The SHA-256 of the text body sent, in lower-case hexadecimal; an identifier.
    body_sha256 = models.CharField(max_length=64, blank=True, default="")
    status = _choice(MessageStatus, default=MessageStatus.QUEUED)
    #: The mail service's message ID (Gmail's); an identifier.
    mail_message_id = models.CharField(max_length=128, blank=True, default="")
    #: The class name of the failure, never a message (CHECK); guarded like free text.
    error = guarded_char(100, blank=True, default="")
    manual = models.BooleanField(default=False)
    queued_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    #: Who sent it by hand, or who confirmed the lease end of a final statement; null
    #: for the ladder.
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "outbound_messages"
        indexes = [
            models.Index(fields=["operator", "queued_at"], name="outbound_messages_op_idx"),
            models.Index(
                fields=["charge_entity_type", "charge_entity_id"],
                name="outbound_messages_charge_idx",
            ),
        ]
        constraints = [
            _valid_choice("outbound_messages", "status", MessageStatus),
            _valid_choice("outbound_messages", "charge_entity_type", ChargeEntityType),
            _valid_choice("outbound_messages", "kind", MessageKind),
            # A ladder message names its step and a charge; a final statement names a
            # lease end and no step. (Each rule reads only valid kinds.)
            models.CheckConstraint(
                condition=(
                    ~Q(kind=MessageKind.LADDER)
                    | Q(
                        step__isnull=False,
                        charge_entity_type__in=[
                            ChargeEntityType.RENT_CHARGE,
                            ChargeEntityType.ONE_OFF_CHARGE,
                        ],
                    )
                )
                & (
                    ~Q(kind=MessageKind.FINAL_STATEMENT)
                    | Q(step__isnull=True, charge_entity_type=ChargeEntityType.LEASE_END)
                ),
                name="outbound_messages_step_iff_ladder",
            ),
            models.CheckConstraint(
                condition=Q(kind=MessageKind.FINAL_STATEMENT) | Q(document__isnull=True),
                name="outbound_messages_document_only_final_statement",
            ),
            models.CheckConstraint(
                condition=Q(body_sha256__regex=r"^([0-9a-f]{64})?$"),
                name="outbound_messages_body_sha256_hex",
            ),
            models.CheckConstraint(
                condition=Q(error__regex=r"^([A-Za-z_][A-Za-z0-9_]*)?$"),
                name="outbound_messages_error_class_name",
            ),
            models.CheckConstraint(
                condition=Q(mail_message_id__regex=r"^[A-Za-z0-9_-]*$"),
                name="outbound_messages_mail_message_id_shape",
            ),
            models.CheckConstraint(
                condition=Q(manual=False) | Q(sent_by__isnull=False),
                name="outbound_messages_manual_has_sender",
            ),
            models.UniqueConstraint(
                fields=["charge_entity_type", "charge_entity_id", "step"],
                condition=Q(manual=False),
                name="outbound_messages_one_per_charge_step",
            ),
            models.UniqueConstraint(
                fields=["charge_entity_type", "charge_entity_id"],
                condition=Q(kind=MessageKind.FINAL_STATEMENT, manual=False),
                name="outbound_messages_one_final_statement",
            ),
        ]


# --- Contact log and calendar (PLAN-011b D7, D8) ------------------------------------


class ContactKind(models.TextChoices):
    CALL = "call", "Call"
    EMAIL = "email", "E-mail"
    VISIT = "visit", "Visit"
    PROMISE = "promise", "Promise to pay"


#: The kinds other than ``promise``: each CHECK reads only valid kinds (see one-off charges).
NON_PROMISE_CONTACT_KINDS: list[str] = [
    ContactKind.CALL.value,
    ContactKind.EMAIL.value,
    ContactKind.VISIT.value,
]


class ContactStatus(models.TextChoices):
    """Where a promise to pay stands (PLAN-012 D4, D5). Every other kind stays ``open``."""

    OPEN = "open", "Open"
    KEPT = "kept", "Kept"
    RESCHEDULED = "rescheduled", "Rescheduled"
    BROKEN = "broken", "Broken"


#: The statuses other than ``open``: the CHECK reads only valid statuses, so that a bogus one
#: fails ``contact_entries_status_valid`` alone.
CLOSED_CONTACT_STATUSES: list[str] = [
    ContactStatus.KEPT.value,
    ContactStatus.RESCHEDULED.value,
    ContactStatus.BROKEN.value,
]


class ContactEntry(GuardedModel):
    """One dated contact with an operator: a call, an e-mail, a visit or a promise to pay
    (PLAN-011b D7, PLAN-012 D2 to D5). Billing adds them; the author may edit one for a
    short while, then only an appended :class:`ContactCorrection` changes what it says;
    an admin may soft delete one with a reason (``deleted_at``, ``deleted_by`` and
    ``delete_reason``: all three or none, CHECK). Nothing removes a row.

    ``lease`` (nullable) names the store the entry is about; the service holds it to a
    lease of the same operator. A promise carries ``promised_date`` and
    ``promised_amount``; no other kind does (CHECK). A promise's ``status`` is ``open``
    until it is kept (by a later entry's outcome, ``closed_by``, or by a settled payment,
    ``kept_by_payment``), rescheduled or broken; every other kind stays ``open`` (CHECK).
    When a promise is broken or kept is decided by :mod:`ledger.services.contacts`.
    """

    operator = models.ForeignKey(Operator, on_delete=models.PROTECT, related_name="contact_entries")
    lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, related_name="contact_entries"
    )
    kind = _choice(ContactKind)
    happened_on = models.DateField()
    promised_date = models.DateField(null=True, blank=True)
    promised_amount = MoneyField(null=True, blank=True)
    body = free_text(blank=False)
    status = _choice(ContactStatus, default=ContactStatus.OPEN)
    #: On a promise: the later entry that recorded its outcome.
    closed_by = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="closes"
    )
    #: On a kept promise: the settled payment that kept it.
    kept_by_payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="kept_promises"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    delete_reason = free_text()

    class Meta:
        db_table = "contact_entries"
        indexes = [
            models.Index(fields=["operator", "happened_on"], name="contact_entries_op_day_idx"),
        ]
        constraints = [
            _not_blank("contact_entries", "body"),
            _valid_choice("contact_entries", "kind", ContactKind),
            _valid_choice("contact_entries", "status", ContactStatus),
            models.CheckConstraint(
                condition=(
                    ~Q(kind="promise")
                    | Q(promised_date__isnull=False, promised_amount__isnull=False)
                )
                & (
                    ~Q(kind__in=NON_PROMISE_CONTACT_KINDS)
                    | Q(promised_date__isnull=True, promised_amount__isnull=True)
                ),
                name="contact_entries_promise_fields",
            ),
            models.CheckConstraint(
                condition=Q(promised_amount__isnull=True) | Q(promised_amount__gt=0),
                name="contact_entries_promised_amount_positive",
            ),
            models.CheckConstraint(
                condition=Q(closed_by__isnull=True) | (Q(kind="promise") & ~Q(closed_by=F("id"))),
                name="contact_entries_closed_by_promise_only",
            ),
            models.CheckConstraint(
                condition=~Q(kind__in=NON_PROMISE_CONTACT_KINDS)
                | ~Q(status__in=CLOSED_CONTACT_STATUSES),
                name="contact_entries_status_promise_only",
            ),
            models.CheckConstraint(
                condition=Q(kept_by_payment__isnull=True) | Q(status="kept"),
                name="contact_entries_kept_by_payment_kept",
            ),
            models.CheckConstraint(
                condition=Q(deleted_at__isnull=True, deleted_by__isnull=True, delete_reason="")
                | (
                    Q(deleted_at__isnull=False, deleted_by__isnull=False)
                    & Q(delete_reason__regex=_NOT_BLANK)
                ),
                name="contact_entries_deleted_all_or_none",
            ),
        ]


class ContactCorrection(GuardedModel):
    """A correction appended to a contact entry once its edit window has closed
    (PLAN-012 D2). Any billing user adds one; nothing edits or deletes one."""

    entry = models.ForeignKey(ContactEntry, on_delete=models.PROTECT, related_name="corrections")
    body = free_text(blank=False)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contact_corrections"
        constraints = [_not_blank("contact_corrections", "body")]


class CalendarEntryKind(models.TextChoices):
    NOTE = "note", "Note"
    REMINDER = "reminder", "Reminder"


#: Longest calendar title: it is an event's summary line (PLAN-011b D8).
CALENDAR_TITLE_MAX_LENGTH = 120


class CalendarEntry(GuardedModel):
    """A dated note or reminder on the staff calendar (PLAN-011b D8).

    Entries are shared staff notes: every staff user sees every entry, on the page and
    in their own calendar feed. There is no private entry. ``email_morning`` asks for an
    e-mail to the author on the morning of ``on`` (the daily tick, PLAN-011b D8).
    """

    kind = _choice(CalendarEntryKind)
    store = models.ForeignKey(
        Store, on_delete=models.PROTECT, null=True, blank=True, related_name="calendar_entries"
    )
    operator = models.ForeignKey(
        Operator,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="calendar_entries",
    )
    lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, related_name="calendar_entries"
    )
    on = models.DateField()
    title = guarded_char(CALENDAR_TITLE_MAX_LENGTH)
    body = free_text()
    email_morning = models.BooleanField(default=False)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "calendar_entries"
        indexes = [models.Index(fields=["on"], name="calendar_entries_on_idx")]
        constraints = [
            _not_blank("calendar_entries", "title"),
            _valid_choice("calendar_entries", "kind", CalendarEntryKind),
        ]


class CalendarFeed(models.Model):
    """A user's calendar feed token (PLAN-011b D8): only its SHA-256 is stored.

    The token is shown once, when it is made. A user has at most one live feed; making a
    new one revokes the old. A revoked feed answers 404 like an unknown one. The feed is
    per user only so that it can be revoked: it carries the same shared entries for
    everyone.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="calendar_feeds"
    )
    #: The token's SHA-256 digest, 32 bytes: binary, so no text guard can misread it.
    token_hash = models.BinaryField(max_length=32, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "calendar_feeds"
        constraints = [
            models.CheckConstraint(
                condition=Exact(Length("token_hash"), 32), name="calendar_feeds_token_hash_size"
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(revoked_at__isnull=True),
                name="calendar_feeds_one_live_per_user",
            ),
        ]


# --- Lease end and transfer (PLAN-012e D4, D5, D6) -----------------------------------


class LeaseEndReason(models.TextChoices):
    MOVED_OUT = "moved_out", "Moved out"
    TERMINATED = "terminated", "Terminated"
    TRANSFER = "transfer", "Transfer to another operator"
    OTHER = "other", "Other"


class LeaseEndStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    CONFIRMED = "confirmed", "Confirmed"


class RefundMethod(models.TextChoices):
    """How a lease-end refund is paid back (PLAN-012e D5(d)): never through the payment
    provider. A check is recorded as a payment of method ``other``."""

    ZELLE = "zelle", "Zelle"
    WIRE = "wire", "Wire"
    CHECK = "check", "Check"


#: Longest refund reference (a Zelle confirmation, a wire reference, a check number).
REFUND_REFERENCE_MAX_LENGTH = 100
#: Longest deduction description.
DEDUCTION_DESCRIPTION_MAX_LENGTH = 200


class LeaseEnd(GuardedModel):
    """The end-of-lease wizard's state and, once confirmed, its figures (PLAN-012e D4).

    One per lease: a draft can be left and resumed and is deleted on Cancel; a confirmed
    row keeps ``statement_json`` as the figures were at confirm, the PDF (``document``,
    set by the ``lease_end_documents`` job), and the rows the settlement wrote.
    Written only by :mod:`ledger.services.lease_end`.
    """

    lease = models.OneToOneField(Lease, on_delete=models.PROTECT, related_name="lease_end")
    ended_on = models.DateField(null=True, blank=True)
    reason = _choice(LeaseEndReason, blank=True, default="")
    #: Free words for the reason (required for ``other``).
    reason_note = free_text()
    prorate_last_month = models.BooleanField(default=True)
    status = _choice(LeaseEndStatus, default=LeaseEndStatus.DRAFT)
    refund_method = _choice(RefundMethod, blank=True, default="")
    refund_on = models.DateField(null=True, blank=True)
    refund_reference = guarded_char(REFUND_REFERENCE_MAX_LENGTH, blank=True, default="")
    settlement_note = free_text()
    statement_json = guarded_json(default=dict, blank=True)
    document = models.ForeignKey(
        Document, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    refund_payment = models.ForeignKey(
        "Payment", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    debt_charge = models.ForeignKey(
        OneOffCharge, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    #: PLAN-012b-b D9, PLAN-012f-a D: set when the statement step is first saved.
    #: A draft without it is not in progress: the store page shows no banner and a
    #: transfer adopts it.
    statement_saved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "lease_ends"
        constraints = [
            _valid_choice("lease_ends", "reason", LeaseEndReason, blank=True),
            _valid_choice("lease_ends", "status", LeaseEndStatus),
            _valid_choice("lease_ends", "refund_method", RefundMethod, blank=True),
            # A confirmed end has its date, its reason and its moment.
            models.CheckConstraint(
                condition=~Q(status=LeaseEndStatus.CONFIRMED)
                | (Q(ended_on__isnull=False, confirmed_at__isnull=False) & ~Q(reason="")),
                name="lease_ends_confirmed_complete",
            ),
            models.CheckConstraint(
                condition=Q(status=LeaseEndStatus.CONFIRMED) | Q(confirmed_at__isnull=True),
                name="lease_ends_confirmed_at_iff_confirmed",
            ),
        ]

    def __str__(self) -> str:
        return f"Lease end {self.pk}"


class DeductionKind(models.TextChoices):
    DAMAGE = "damage", "Damage"
    CLEANING = "cleaning", "Cleaning"
    UNPAID_BALANCE = "unpaid_balance", "Unpaid balance offset"
    OTHER = "other", "Other"


class LeaseEndDeduction(GuardedModel):
    """One typed deduction from the deposit on a lease end's statement (PLAN-012e D5(c))."""

    lease_end = models.ForeignKey(LeaseEnd, on_delete=models.CASCADE, related_name="deductions")
    kind = _choice(DeductionKind)
    description = guarded_char(DEDUCTION_DESCRIPTION_MAX_LENGTH, blank=True, default="")
    amount = MoneyField()
    position = models.SmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "lease_end_deductions"
        ordering = ("position", "created_at")
        constraints = [
            _valid_choice("lease_end_deductions", "kind", DeductionKind),
            _positive("lease_end_deductions", "amount"),
        ]


class LeaseTransferStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    CONFIRMED = "confirmed", "Confirmed"


class LeaseTransfer(GuardedModel):
    """A hand-over of a store from one operator to another (PLAN-012e D6): the outgoing
    lease, its end, the incoming lease and the builder draft of the move-in charges.

    The transfer wizard's state while a draft (one per store): the incoming operator and
    the incoming lease's terms (``incoming_*``), the deposit amount and the ticked setup
    items (``setup_lines``: ``[{"fee_type": <id>, "price": "<amount>"}]``, catalogue
    order). Confirmed, it names the incoming lease and the draft it made. A lease-end
    balance left on the outgoing operator needs an admin's written reason
    (``debt_override_*``, D14). Written only by :mod:`ledger.services.transfer`.
    """

    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="transfers")
    outgoing_lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, related_name="transfers_out"
    )
    incoming_lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, related_name="transfers_in"
    )
    lease_end = models.OneToOneField(
        LeaseEnd, on_delete=models.PROTECT, null=True, blank=True, related_name="transfer"
    )
    draft = models.ForeignKey(
        InvoiceDraft, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    status = _choice(LeaseTransferStatus, default=LeaseTransferStatus.DRAFT)
    incoming_operator = models.ForeignKey(
        Operator, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    incoming_started_on = models.DateField(null=True, blank=True)
    incoming_rent = MoneyField(null=True, blank=True)
    incoming_debit_day = models.SmallIntegerField(null=True, blank=True)
    incoming_second_debit_day = models.SmallIntegerField(null=True, blank=True)
    incoming_channel = _choice(PaymentChannel, blank=True, default="")
    incoming_prorate_first_month = models.BooleanField(default=True)
    deposit_amount = MoneyField(null=True, blank=True)
    setup_lines = guarded_json(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    #: D14: a lease-end balance an admin allowed to remain on the outgoing operator, with
    #: the written reason, who and when; all four set or none.
    debt_override_amount = MoneyField(null=True, blank=True)
    debt_override_reason = free_text()
    debt_override_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    debt_override_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "lease_transfers"
        constraints = [
            models.CheckConstraint(
                condition=Q(incoming_lease__isnull=True) | ~Q(incoming_lease=F("outgoing_lease")),
                name="lease_transfers_two_leases",
            ),
            _valid_choice("lease_transfers", "status", LeaseTransferStatus),
            _valid_choice("lease_transfers", "incoming_channel", PaymentChannel, blank=True),
            _day_of_month("lease_transfers", "incoming_debit_day"),
            _day_of_month("lease_transfers", "incoming_second_debit_day"),
            models.CheckConstraint(
                condition=Q(incoming_rent__isnull=True) | Q(incoming_rent__gt=0),
                name="lease_transfers_incoming_rent_positive",
            ),
            models.CheckConstraint(
                condition=Q(deposit_amount__isnull=True) | Q(deposit_amount__gte=0),
                name="lease_transfers_deposit_amount_not_negative",
            ),
            # A confirmed transfer names its end, its incoming lease and its moment.
            models.CheckConstraint(
                condition=~Q(status=LeaseTransferStatus.CONFIRMED)
                | Q(
                    confirmed_at__isnull=False,
                    lease_end__isnull=False,
                    incoming_lease__isnull=False,
                ),
                name="lease_transfers_confirmed_complete",
            ),
            models.CheckConstraint(
                condition=Q(status=LeaseTransferStatus.CONFIRMED) | Q(confirmed_at__isnull=True),
                name="lease_transfers_confirmed_at_iff_confirmed",
            ),
            # One transfer in progress per store.
            models.UniqueConstraint(
                fields=["store"],
                condition=Q(status=LeaseTransferStatus.DRAFT),
                name="lease_transfers_one_draft_per_store",
            ),
            # D14: an allowed balance has its amount, its reason and its moment; else none.
            models.CheckConstraint(
                condition=Q(
                    debt_override_at__isnull=True,
                    debt_override_amount__isnull=True,
                    debt_override_by__isnull=True,
                    debt_override_reason="",
                )
                | Q(
                    debt_override_at__isnull=False,
                    debt_override_amount__isnull=False,
                    debt_override_reason__regex=_NOT_BLANK,
                ),
                name="lease_transfers_debt_override_complete",
            ),
        ]

    def __str__(self) -> str:
        return f"Lease transfer {self.pk}"


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
        # The audit log page filters on a time range and on the entity (PLAN-010d D8).
        indexes = [
            models.Index(fields=["at"], name="audit_log_at_idx"),
            models.Index(fields=["entity_type", "entity_id", "at"], name="audit_log_entity_idx"),
        ]


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


# --- Company settings (PLAN-012c D2) ----------------------------------------------------


class CompanyTimeZone(models.TextChoices):
    """The US time zones the company may work in."""

    NEW_YORK = "America/New_York", "Eastern (New York)"
    CHICAGO = "America/Chicago", "Central (Chicago)"
    DENVER = "America/Denver", "Mountain (Denver)"
    PHOENIX = "America/Phoenix", "Mountain, no daylight saving (Phoenix)"
    LOS_ANGELES = "America/Los_Angeles", "Pacific (Los Angeles)"
    ANCHORAGE = "America/Anchorage", "Alaska (Anchorage)"
    HONOLULU = "Pacific/Honolulu", "Hawaii (Honolulu)"


COMPANY_SETTINGS_ID = 1
COMPANY_NAME_MAX_LENGTH = 100
#: A logo override is the blob path of a stored document, as
#: :mod:`ledger.services.documents` builds it: ``documents/<owner type>/<uuid>/<uuid>``.
#: Nothing else (no URL, no scheme, no ``..``) can be stored.
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
LOGO_PATH_PATTERN = rf"\Adocuments/(store|lease|operator)/{_UUID}/{_UUID}\Z"
LOGO_PATH_MESSAGE = "The logo must be the path of a stored document, or blank."
#: The stored types a logo override may have: images the preview route serves inline.
LOGO_CONTENT_TYPES = (DocumentContentType.PNG, DocumentContentType.JPEG)

validate_logo_path = RegexValidator(LOGO_PATH_PATTERN, LOGO_PATH_MESSAGE, code="logo_path")


class CompanySettings(PaymentDataGuardedModel):
    """The one row of company-wide display settings (PLAN-012c D2).

    The company's name and short name (the bar and every page title), the support
    e-mail, the time zone, a logo override (a stored document; the brand logo when blank)
    and the payment processor's display name (``processor_name``; no business column is
    named after the provider, PLAN-004), so no template names the processor. A
    CHECK holds the table to the row ``id = 1``; :meth:`current` reads it and creates it
    when it is missing (the admin and the management paths); :meth:`read` only reads it
    (the page path). Edited in the Django admin by an admin only, never added or
    deleted there, and every edit is audited (``company_settings.updated``).
    """

    id = models.PositiveSmallIntegerField(
        primary_key=True, default=COMPANY_SETTINGS_ID, editable=False
    )
    name = guarded_char(COMPANY_NAME_MAX_LENGTH)
    short_name = guarded_char(COMPANY_NAME_MAX_LENGTH)
    support_email = models.EmailField(blank=True, validators=[validate_no_payment_data])
    time_zone = _choice(CompanyTimeZone, default=CompanyTimeZone.NEW_YORK)
    logo = models.CharField(
        max_length=200, blank=True, validators=[validate_no_payment_data, validate_logo_path]
    )
    processor_name = guarded_char(COMPANY_NAME_MAX_LENGTH)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "company_settings"
        verbose_name = "company settings"
        verbose_name_plural = "company settings"
        constraints = [
            models.CheckConstraint(
                condition=Q(id=COMPANY_SETTINGS_ID), name="company_settings_singleton"
            ),
            _valid_choice("company_settings", "time_zone", CompanyTimeZone),
            _not_blank("company_settings", "name"),
            _not_blank("company_settings", "short_name"),
            _not_blank("company_settings", "processor_name"),
            models.CheckConstraint(
                condition=Q(logo="") | Q(logo__regex=LOGO_PATH_PATTERN),
                name="company_settings_logo_path",
            ),
        ]

    def __str__(self) -> str:
        return str(self.name)

    @property
    def provider_name(self) -> str:
        """The processor's display name, as templates read it (``company.provider_name``)."""
        return str(self.processor_name)

    @provider_name.setter
    def provider_name(self, value: str) -> None:
        self.processor_name = value

    @classmethod
    def current(cls) -> CompanySettings:
        """The row, created with the seed values when it is missing."""
        from ledger.company_defaults import SEED

        try:
            with transaction.atomic():
                row, _ = cls.objects.get_or_create(pk=COMPANY_SETTINGS_ID, defaults=SEED)
        except DatabaseError:
            # The table is not there yet (a database before migration 0045): the seed
            # values, unsaved, so a page such as the 401 page still renders.
            return cls(pk=COMPANY_SETTINGS_ID, **SEED)
        return row

    @classmethod
    def read(cls) -> CompanySettings:
        """The row, read with one plain query and never created (the page path; the
        migration seeds it): the seed values, unsaved, when it is missing or the table is
        not there yet."""
        from ledger.company_defaults import SEED

        try:
            row = cls.objects.filter(pk=COMPANY_SETTINGS_ID).first()
        except DatabaseError:
            row = None
        return row if row is not None else cls(pk=COMPANY_SETTINGS_ID, **SEED)
