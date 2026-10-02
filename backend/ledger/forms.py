"""Operator, lease and store forms (docs/qa/PLAN-006-operators.md,
PLAN-007-leases-and-rent-run.md, PLAN-007b-stores.md).

Server-side validation is what counts; the browser checks (``type=email``,
``required``) are a convenience. Every text field keeps the model's card-number guard,
which runs when the form validates the model instance.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django import forms
from django.core.validators import RegexValidator
from django.db.models import Q

from ledger.models import (
    CollectionStatus,
    Lease,
    Operator,
    PaymentChannel,
    PaymentMethod,
    Store,
)
from ledger.validators import validate_no_payment_data, visible_text
from payments.operator_sync import normalise_phone

if TYPE_CHECKING:
    _OperatorFormBase = forms.ModelForm[Operator]
    _LeaseFormBase = forms.ModelForm[Lease]
    _StoreFormBase = forms.ModelForm[Store]
else:  # ModelForm is only generic in the type stubs.
    _OperatorFormBase = forms.ModelForm
    _LeaseFormBase = forms.ModelForm
    _StoreFormBase = forms.ModelForm

#: Fixed and value-free (PLAN-006b §1).
PHONE_MESSAGE = "Enter 10 to 15 digits, for example 4155550132."
IDENTIFIER_MAX_LENGTH = 255
#: Letters, digits, hyphen and underscore only, 1 to 255 characters.
IDENTIFIER_VALIDATOR = RegexValidator(
    r"\A[A-Za-z0-9_-]{1,255}\Z",
    message="Use letters, digits, hyphens and underscores only.",
    code="identifier",
)


class OperatorForm(_OperatorFormBase):
    """Create and edit. The e-mail is lower-cased and stripped; empty means none.

    The phone must pass the payment provider's rule, on create and on every edit, so an
    operator with a bad phone cannot be saved (or re-synced) until it is fixed.
    """

    email = forms.EmailField(required=False, max_length=254)

    class Meta:
        model = Operator
        fields = (
            "display_name",
            "legal_name",
            "company",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "notes",
        )

    def clean_email(self) -> str | None:
        value = self.cleaned_data.get("email") or ""
        return value.strip().lower() or None

    def clean_phone(self) -> str:
        """The provider's phone rule; the normalised digits are stored, empty stays empty."""
        digits = normalise_phone(str(self.cleaned_data.get("phone") or ""))
        if digits is None:
            raise forms.ValidationError(PHONE_MESSAGE, code="phone")
        return digits

    def clean_display_name(self) -> str:
        value = str(self.cleaned_data.get("display_name") or "").strip()
        if not value:
            raise forms.ValidationError("Enter a display name.", code="required")
        return value


class OperatorCreateForm(OperatorForm):
    """Adds the one-time key rendered with the form (D6)."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)

    class Meta(OperatorForm.Meta):
        pass

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.order_fields(["creation_key", *OperatorForm.Meta.fields])


class LinkForm(forms.Form):
    """Exactly two identifiers copied from the payment provider's dashboard (D4)."""

    customer_id = forms.CharField(
        label="Customer ID",
        max_length=IDENTIFIER_MAX_LENGTH,
        validators=[IDENTIFIER_VALIDATOR],
    )
    payment_method_id = forms.CharField(
        label="Payment method ID",
        max_length=IDENTIFIER_MAX_LENGTH,
        validators=[IDENTIFIER_VALIDATOR],
    )


# --- Leases (PLAN-007 §4.1) -------------------------------------------------------------

#: Largest rent the money column holds (numeric(12,2)).
RENT_MAX = Decimal("9999999999.99")


class LeaseForm(_LeaseFormBase):
    """Create and edit a lease. Rent is a separate field: a change writes history (D5)."""

    debit_day = forms.IntegerField(min_value=1, max_value=31, required=False)
    second_debit_day = forms.IntegerField(
        min_value=1,
        max_value=31,
        required=False,
        help_text="Only for a lease debited twice a month.",
    )
    rent = forms.DecimalField(
        label="Monthly rent",
        min_value=Decimal("0.01"),
        max_value=RENT_MAX,
        max_digits=12,
        decimal_places=2,
    )
    rent_effective_from = forms.DateField(
        label="New rent takes effect on",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Only used when the rent changes. Empty means today.",
    )

    class Meta:
        model = Lease
        fields = (
            "store",
            "operator",
            "debit_day",
            "second_debit_day",
            "payment_channel",
            "started_on",
            "collection_status",
        )
        widgets = {"started_on": forms.DateInput(attrs={"type": "date"})}
        labels = {"collection_status": "Status", "started_on": "Start date"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        store = self.fields["store"]
        operator = self.fields["operator"]
        if isinstance(store, forms.ModelChoiceField):
            store.queryset = Store.objects.order_by("external_label", "id")
        if isinstance(operator, forms.ModelChoiceField):
            current = self.instance.operator_id if self.instance.pk else None
            operator.queryset = Operator.objects.filter(
                Q(archived_at__isnull=True) | Q(pk=current)
            ).order_by("display_name", "id")
        if not self.instance.pk:
            del self.fields["rent_effective_from"]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        day = data.get("debit_day")
        second = data.get("second_debit_day")
        if second is not None and day is None:
            self.add_error("second_debit_day", "Set the first debit day first.")
        elif second is not None and second == day:
            self.add_error("second_debit_day", "The second debit day must differ from the first.")
        if data.get("collection_status") == CollectionStatus.ACTIVE:
            if data.get("operator") is None:
                self.add_error("operator", "An active lease needs an operator.")
            if day is None:
                self.add_error("debit_day", "An active lease needs a debit day.")
            if data.get("payment_channel") == PaymentChannel.UNKNOWN:
                self.add_error("payment_channel", "An active lease needs a known channel.")
        return data


class LeaseCreateForm(LeaseForm):
    """Adds the one-time key rendered with the form, so a resubmission saves nothing."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)


class _ExactCheckboxInput(forms.CheckboxInput):
    """A checkbox that is ticked only by the exact value a rendered checkbox posts ("on")
    or the literal "true". Django's CheckboxInput treats any other non-empty string,
    such as "0" or "off", as ticked."""

    def value_from_datadict(self, data: Any, files: Any, name: str) -> bool:
        return data.get(name) in ("on", "true")


class EnrolForm(forms.Form):
    """The chosen payment method. Only its syntax is checked here; the service checks
    that it is a bank method of the operator's customer at the provider (D2)."""

    payment_method_id = forms.CharField(
        label="Payment method",
        max_length=IDENTIFIER_MAX_LENGTH,
        validators=[IDENTIFIER_VALIDATOR],
        error_messages={"required": "Choose a bank payment method."},
    )
    #: PLAN-007c: the admin's confirmation of a debit above the warning threshold. The
    #: service decides whether it is needed and refuses without it.
    confirm_large_debit = forms.BooleanField(required=False, widget=_ExactCheckboxInput)


# --- Stores (PLAN-007b §1) ----------------------------------------------------------------

OPEN_DATE_REQUIRED = "Enter the open date."
CLOSED_BEFORE_OPEN = "The closed date cannot be before the open date."


class StoreForm(_StoreFormBase):
    """Create and edit a store. The open date is required here (ADR-0005) although the
    model keeps it nullable for the import; ``needs_open_date`` follows the model."""

    store_number = forms.IntegerField(min_value=1, max_value=2147483647, required=False)
    open_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={"required": OPEN_DATE_REQUIRED},
    )

    class Meta:
        model = Store
        fields = (
            "store_number",
            "external_label",
            "city",
            "state",
            "open_date",
            "closed_at",
            "status",
        )
        widgets = {"closed_at": forms.DateInput(attrs={"type": "date"})}
        labels = {"external_label": "Name", "closed_at": "Closed date"}

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        opened = data.get("open_date")
        closed = data.get("closed_at")
        if opened is not None and closed is not None and closed < opened:
            self.add_error("closed_at", CLOSED_BEFORE_OPEN)
        return data


class StoreCreateForm(StoreForm):
    """Adds the one-time key rendered with the form, so a resubmission saves nothing."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)


# --- Manual payments (PLAN-007 D8) -------------------------------------------------------

#: Longest note accepted on a manual payment.
PAYMENT_NOTE_MAX_LENGTH = 2000
PAYMENT_REFERENCE_MAX_LENGTH = 100
MANUAL_METHOD_CHOICES = (
    (PaymentMethod.ZELLE.value, PaymentMethod.ZELLE.label),
    (PaymentMethod.WIRE.value, PaymentMethod.WIRE.label),
    (PaymentMethod.OTHER.value, PaymentMethod.OTHER.label),
)


class ManualPaymentForm(forms.Form):
    """A payment received outside the payment provider. The service re-checks every rule
    (balance, date, note) under a row lock; the form checks shape only."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)
    method = forms.ChoiceField(choices=MANUAL_METHOD_CHOICES)
    amount = forms.DecimalField(
        min_value=Decimal("0.01"), max_value=RENT_MAX, max_digits=12, decimal_places=2
    )
    paid_on = forms.DateField(label="Received on", widget=forms.DateInput(attrs={"type": "date"}))
    reference = forms.CharField(
        required=False,
        max_length=PAYMENT_REFERENCE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text=(
            "Optional: the bank or Zelle reference. Letters, digits and hyphens of it (up to "
            "25) are sent with the invoice when it is marked paid."
        ),
    )
    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: what was received, and any reference staff need.",
        error_messages={"required": "A note is required: say what was received."},
    )


class LinkInvoiceForm(forms.Form):
    """Link one of the operator's unlinked invoices to a rent charge (PLAN-008a §2 rule
    4). The invoice is named by its local key; the service checks under row locks that it
    belongs to the charge's operator and is unlinked, and requires the confirmation and
    the note when its total differs from the balance."""

    invoice = forms.UUIDField(error_messages={"required": "Choose an invoice."})
    #: The exact-value confirmation checkbox of PLAN-007c.
    confirm = forms.BooleanField(required=False, widget=_ExactCheckboxInput)
    note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required when the total differs from the balance: say why.",
    )


class ResolveReturnForm(forms.Form):
    """Resolve a returned payment (PLAN-008 D6): a note, nothing else."""

    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: how the return was resolved.",
        error_messages={"required": "A note is required: say how the return was resolved."},
    )


class ResolveMismatchForm(forms.Form):
    """Resolve a reconciliation mismatch (PLAN-010 D10): a note, nothing else."""

    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: how the mismatch was resolved.",
        error_messages={"required": "A note is required: say how the mismatch was resolved."},
    )

    def clean_note(self) -> str:
        # Invisible format characters and Unicode whitespace alone are not a note.
        note = visible_text(self.cleaned_data["note"])
        if not note:
            raise forms.ValidationError(self.fields["note"].error_messages["required"])
        return note


# --- Refunds and voids (PLAN-009 D6, D7) ---------------------------------------------------


class RefundForm(forms.Form):
    """An admin's refund of an ACH payment. The service re-checks every rule (status,
    refundable amount, confirmation) under the payment's row lock; the form checks
    shape only."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)
    amount = forms.DecimalField(
        label="Refund amount",
        min_value=Decimal("0.01"),
        max_value=RENT_MAX,
        max_digits=12,
        decimal_places=2,
    )
    #: The exact-value confirmation checkbox of PLAN-007c; the service refuses without it.
    confirm = forms.BooleanField(required=False, widget=_ExactCheckboxInput)


class VoidForm(forms.Form):
    """An admin's void of a whole ACH payment; the service re-checks every rule."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(required=False, widget=_ExactCheckboxInput)
