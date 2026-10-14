"""Operator, lease and store forms (docs/qa/PLAN-006-operators.md,
PLAN-007-leases-and-rent-run.md, PLAN-007b-stores.md).

Server-side validation is what counts; the browser checks (``type=email``,
``required``) are a convenience. Every text field keeps the model's card-number guard,
which runs when the form validates the model instance.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django import forms
from django.core.validators import RegexValidator
from django.db.models import Q

from ledger.models import (
    CALENDAR_TITLE_MAX_LENGTH,
    CONSENT_FIELDS,
    DESCRIPTION_MAX_LENGTH,
    EVIDENCE_NAME_MAX_LENGTH,
    INSTALLMENTS_MAX,
    INSTALLMENTS_MIN,
    NAME_MAX_LENGTH,
    CalendarEntryKind,
    CollectionStatus,
    ContactKind,
    ContactStatus,
    DeductionKind,
    DepositResolutionKind,
    DocumentKind,
    FeeType,
    InstallmentCadence,
    Lease,
    LeaseEndReason,
    Operator,
    PaymentChannel,
    PaymentMethod,
    RefundMethod,
    Store,
)
from ledger.services import invoice_builder
from ledger.services.deposits import NOTE_REQUIRED as DEPOSIT_NOTE_REQUIRED
from ledger.services.leases import END_DATE_REQUIRED
from ledger.services.one_off_charges import ChargeInput
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

#: What a ``datetime-local`` input sends and shows, to the second (PLAN-012a D3).
CONSENT_INPUT_FORMAT = "%Y-%m-%dT%H:%M:%S"
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
            "sms_phone",
            "sms_consent_at",
            "email_consent_at",
        )
        labels = {
            "sms_phone": "SMS phone (international, +1…)",
            "sms_consent_at": "SMS consent given on",
            "email_consent_at": "E-mail consent given on",
        }
        widgets = {
            name: forms.DateTimeInput(
                attrs={"type": "datetime-local", "step": "1"}, format=CONSENT_INPUT_FORMAT
            )
            for name in CONSENT_FIELDS
        }

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
            "prorate_first_month",
        )
        widgets = {"started_on": forms.DateInput(attrs={"type": "date"})}
        labels = {
            "collection_status": "Status",
            "started_on": "Start date",
            "prorate_first_month": "Prorate first month",
        }
        help_texts = {
            "prorate_first_month": (
                "When the lease starts after the 1st, the first month's rent is charged "
                "for the days used only."
            )
        }

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

    def lease_fields(self) -> list[forms.BoundField]:
        """The visible fields of the lease itself (the create form adds a checklist)."""
        return [field for field in self.visible_fields() if not field.name.startswith(SETUP)]

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
    """Adds the one-time key rendered with the form, so a resubmission saves nothing,
    and the setup checklist (PLAN-010b D5): per active fee type a tick, cost, price,
    vendor and due date, the amounts defaulting from the catalogue. A ticked row needs a
    price; an empty due date is the lease's start date."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fee_types = list(
            FeeType.objects.filter(active=True).order_by("sort_order", "name", "id")
        )
        for fee_type in self.fee_types:
            name = _setup_name(fee_type)
            self.fields[f"{name}_tick"] = forms.BooleanField(
                required=False, widget=_ExactCheckboxInput, label=fee_type.name
            )
            self.fields[f"{name}_cost"] = _money_field(
                "Cost", minimum=Decimal("0.00"), initial=fee_type.default_cost
            )
            self.fields[f"{name}_price"] = _money_field(
                "Price", minimum=Decimal("0.01"), initial=fee_type.default_price
            )
            self.fields[f"{name}_vendor"] = _vendor_field()
            self.fields[f"{name}_due_date"] = forms.DateField(
                label="Due date", required=False, widget=forms.DateInput(attrs={"type": "date"})
            )

    def setup_rows(self) -> list[dict[str, Any]]:
        """The checklist, one row of bound fields per active fee type."""
        return [
            {
                "fee_type": fee_type,
                **{
                    part: self[f"{_setup_name(fee_type)}_{part}"]
                    for part in ("tick", "cost", "price", "vendor", "due_date")
                },
            }
            for fee_type in self.fee_types
        ]

    def clean(self) -> dict[str, Any]:
        data = super().clean()
        started = data.get("started_on")
        for fee_type in self.fee_types:
            name = _setup_name(fee_type)
            if data.get(f"{name}_tick") is not True:
                continue
            if data.get(f"{name}_price") is None and f"{name}_price" not in self.errors:
                self.add_error(f"{name}_price", SETUP_PRICE_REQUIRED)
            if data.get(f"{name}_due_date") is None and started is None:
                self.add_error(f"{name}_due_date", SETUP_DUE_REQUIRED)
        return data

    def setup_items(self) -> list[ChargeInput]:
        """The ticked rows of a valid form, in catalogue order."""
        data = self.cleaned_data
        started: date | None = data.get("started_on")
        items = []
        for fee_type in self.fee_types:
            name = _setup_name(fee_type)
            if data.get(f"{name}_tick") is not True:
                continue
            due = data.get(f"{name}_due_date") or started
            if due is None:  # refused by clean()
                continue
            items.append(
                ChargeInput(
                    fee_type=fee_type,
                    price=data[f"{name}_price"],
                    cost=data.get(f"{name}_cost"),
                    vendor=data.get(f"{name}_vendor") or "",
                    due_date=due,
                )
            )
        return items


# --- Setup charges (PLAN-010b D5, D6) ------------------------------------------------------

#: The name prefix of every checklist field of the lease create form.
SETUP = "setup_"
SETUP_PRICE_REQUIRED = "Enter the price of a ticked item."
SETUP_DUE_REQUIRED = "Enter a due date, or a start date for the lease."
COST_HELP = "Empty means the cost is unknown."


def _setup_name(fee_type: FeeType) -> str:
    return f"{SETUP}{fee_type.code}"


def _money_field(
    label: str, *, minimum: Decimal, initial: Decimal | None = None, required: bool = False
) -> forms.DecimalField:
    return forms.DecimalField(
        label=label,
        required=required,
        min_value=minimum,
        max_value=RENT_MAX,
        max_digits=12,
        decimal_places=2,
        initial=initial,
    )


def _vendor_field() -> forms.CharField:
    return forms.CharField(
        label="Vendor",
        required=False,
        max_length=NAME_MAX_LENGTH,
        validators=[validate_no_payment_data],
    )


class SetupChargeEditForm(forms.Form):
    """Cost, price, vendor and due date of a setup charge (D6). The service locks the price
    once a payment exists and re-checks every rule under the row lock."""

    cost = _money_field("Cost", minimum=Decimal("0.00"))
    price = _money_field("Price", minimum=Decimal("0.01"), required=True)
    vendor = _vendor_field()
    due_date = forms.DateField(label="Due date", widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["cost"].help_text = COST_HELP


class SetupChargeAddForm(SetupChargeEditForm):
    """One setup charge added from the lease page (D5): an active fee type, then the edit
    fields."""

    fee_type = forms.ModelChoiceField(
        queryset=FeeType.objects.none(), error_messages={"required": "Choose a fee type."}
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field = self.fields["fee_type"]
        if isinstance(field, forms.ModelChoiceField):
            field.queryset = FeeType.objects.filter(active=True).order_by("sort_order", "name")
        self.order_fields(["fee_type", "cost", "price", "vendor", "due_date"])

    def item(self) -> ChargeInput:
        data = self.cleaned_data
        return ChargeInput(
            fee_type=data["fee_type"],
            price=data["price"],
            cost=data.get("cost"),
            vendor=data.get("vendor") or "",
            due_date=data["due_date"],
        )


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

    def clean_note(self) -> str:
        # Invisible format characters and Unicode whitespace alone are not a note
        # (PLAN-010c D12).
        note = visible_text(self.cleaned_data["note"])
        if not note:
            raise forms.ValidationError(self.fields["note"].error_messages["required"])
        return note


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

    def clean_note(self) -> str:
        # Stored without invisible characters; the service decides whether a note is
        # required and judges it by the same visible text (PLAN-010c D12).
        return visible_text(self.cleaned_data["note"])


class ResolveReturnForm(forms.Form):
    """Resolve a returned payment (PLAN-008 D6): a note, nothing else."""

    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: how the return was resolved.",
        error_messages={"required": "A note is required: say how the return was resolved."},
    )

    def clean_note(self) -> str:
        # Invisible format characters and Unicode whitespace alone are not a note
        # (PLAN-010c D12).
        note = visible_text(self.cleaned_data["note"])
        if not note:
            raise forms.ValidationError(self.fields["note"].error_messages["required"])
        return note


class RescheduleReturnForm(forms.Form):
    """Reschedule a returned payment (PLAN-011b D5): the agreed re-debit date, the fee
    waiver (admin; the note is then required) and a note. The service judges the rules."""

    redebit_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={"required": "Enter the re-debit date.", "invalid": "Enter a valid date."},
    )
    waive_fee = forms.BooleanField(required=False)
    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        required=False,
        validators=[validate_no_payment_data],
    )
    confirm = forms.BooleanField(required=False)

    def clean_note(self) -> str:
        return visible_text(self.cleaned_data["note"])


# --- One-off charges (PLAN-010d D2, D6) ----------------------------------------------------

#: The longest description the form accepts; the service refuses above 120 characters with
#: its own sentence, so a long paste is answered, not truncated.
DESCRIPTION_INPUT_MAX_LENGTH = 1000
#: The longest item the form accepts; the service refuses above 200 characters with its
#: own sentence.
ITEM_INPUT_MAX_LENGTH = 1000
#: The ``id`` of the draft form's one list of catalogue item names (PLAN-012b D2).
ITEMS_LIST_ID = "draft-items"
#: Blank line rows a draft form shows besides its lines, so that it works without script.
BLANK_LINE_ROWS = 5


def _lease_label(lease: Lease) -> str:
    start = lease.started_on.isoformat() if lease.started_on else "no start date"
    operator = lease.operator.display_name if lease.operator is not None else "no operator"
    return f"{lease.store.external_label}: {operator}, from {start}"


class InvoiceDraftForm(forms.Form):
    """The header of an invoice draft (PLAN-012 D6): the lease (one of ``leases``; or
    none, the operator alone, when ``allow_none``), the memo and the optional due date.
    The form checks shape; :mod:`ledger.services.invoice_builder` checks every rule."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput, required=False)
    lease = forms.ChoiceField(required=False, label="Lease")
    memo = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=invoice_builder.MEMO_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Shown on the invoice.",
    )
    due_date = forms.DateField(
        required=False,
        label="Due date",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=(
            f"Empty: {invoice_builder.DUE_DAYS} days after the invoice is sent (New York time)."
        ),
    )

    def __init__(
        self, *args: Any, leases: Iterable[Lease], allow_none: bool, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.leases = {str(row.pk): row for row in leases}
        lease_field = self.fields["lease"]
        lease_field.required = not allow_none
        lease_field.error_messages["required"] = "Choose the lease."
        if isinstance(lease_field, forms.ChoiceField):
            lease_field.choices = ([("", "No lease (the operator)")] if allow_none else []) + [
                (key, _lease_label(row)) for key, row in self.leases.items()
            ]

    def chosen_lease(self) -> Lease | None:
        return self.leases.get(self.cleaned_data.get("lease") or "")


class InvoiceLineForm(forms.Form):
    """One line of a draft (PLAN-012b D1, D2): an item (one box: a catalogue item's name
    from the list of :data:`ITEMS_LIST_ID`, or a new name), an optional description (the
    detail), a quantity (empty: 1), a unit amount and Save as catalogue item. The form
    never sends a fee type: the service resolves the item by its name. A row with neither
    an item, a description nor a unit amount is blank and ignored; a row whose Remove box
    is ticked is dropped."""

    item = forms.CharField(
        required=False,
        label="Item",
        max_length=ITEM_INPUT_MAX_LENGTH,
        validators=[validate_no_payment_data],
        widget=forms.TextInput(
            {"list": ITEMS_LIST_ID, "placeholder": "Catalogue item or a new name"}
        ),
    )
    description = forms.CharField(
        required=False,
        max_length=DESCRIPTION_INPUT_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text=f"Optional detail under the item; at most {DESCRIPTION_MAX_LENGTH} characters.",
    )
    quantity = forms.DecimalField(
        required=False, max_digits=12, decimal_places=4, widget=forms.NumberInput({"step": "any"})
    )
    unit_amount = forms.DecimalField(
        required=False,
        label="Unit amount",
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput({"step": "0.01"}),
    )
    save_item = forms.BooleanField(required=False, label="Save as catalogue item")

    def is_blank(self) -> bool:
        data = self.cleaned_data
        return not (data.get("item") or data.get("description") or data.get("unit_amount"))

    def line(self) -> invoice_builder.LineInput:
        data = self.cleaned_data
        return invoice_builder.LineInput(
            item=data.get("item") or "",
            description=data.get("description") or "",
            quantity=data.get("quantity"),
            unit_amount=data.get("unit_amount"),
            save_item=data.get("save_item") is True,
        )


InvoiceLineFormSet = forms.formset_factory(
    InvoiceLineForm,
    extra=BLANK_LINE_ROWS,
    can_delete=True,
    max_num=invoice_builder.MAX_LINES,
    validate_max=True,
    absolute_max=invoice_builder.MAX_LINES + BLANK_LINE_ROWS,
)


def draft_lines(formset: forms.BaseFormSet[InvoiceLineForm]) -> list[invoice_builder.LineInput]:
    """The lines of a valid formset, in order: blank and removed rows left out."""
    return [
        form.line()
        for form in formset.forms
        if form.has_changed()
        and not form.cleaned_data.get("DELETE")
        and form.cleaned_data
        and not form.is_blank()
    ]


class CancelChargeForm(forms.Form):
    """Cancel an unpaid one-off charge (D6, billing): a note, nothing else."""

    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: why the charge is cancelled.",
        error_messages={"required": "A note is required: say why the charge is cancelled."},
    )

    def clean_note(self) -> str:
        note = visible_text(self.cleaned_data["note"])
        if not note:
            raise forms.ValidationError(self.fields["note"].error_messages["required"])
        return note


class WaiveForm(forms.Form):
    """Waive a one-off charge (PLAN-010b D6, admin): a note, nothing else."""

    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: why the charge is waived.",
        error_messages={"required": "A note is required: say why the charge is waived."},
    )

    def clean_note(self) -> str:
        # Invisible format characters and Unicode whitespace alone are not a note.
        note = visible_text(self.cleaned_data["note"])
        if not note:
            raise forms.ValidationError(self.fields["note"].error_messages["required"])
        return note


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


class ChargeNowForm(forms.Form):
    """An admin's confirmation of "Charge now" (PLAN-010d D4). ``confirm`` is the
    large-debit box; the service re-checks every rule under the charge's row lock."""

    confirm = forms.BooleanField(required=False, widget=_ExactCheckboxInput)


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


# --- Store costs (PLAN-010b D8) ------------------------------------------------------------


class StoreCostForm(forms.Form):
    """The store page's "Set landlord rent" (billing). The service decides, under the
    store's row lock, whether this updates the open row, starts a new one or is refused."""

    landlord_rent = _money_field("Landlord rent (monthly)", minimum=Decimal("0.00"), required=True)
    effective_from = forms.DateField(
        label="Effective from", widget=forms.DateInput(attrs={"type": "date"})
    )
    other_monthly_cost = _money_field("Other monthly cost", minimum=Decimal("0.00"))
    other_cost_description = forms.CharField(
        label="Other cost description",
        required=False,
        max_length=NAME_MAX_LENGTH,
        validators=[validate_no_payment_data],
    )
    landlord_lease_end = forms.DateField(
        label="Landlord lease end",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["other_monthly_cost"].help_text = "Empty means no other monthly cost."
        self.fields[
            "effective_from"
        ].help_text = "The first day this amount applies. The current row ends the day before."


CHANGE_NOTE_MAX_LENGTH = 2000


def _change_note_field(required_message: str, help_text: str) -> forms.CharField:
    return forms.CharField(
        label="Why",
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=CHANGE_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text=help_text,
        error_messages={"required": required_message},
    )


class StoreCostCorrectForm(StoreCostForm):
    """Correct one cost row (admin, PLAN-010c D10): the corrected values and dates, and
    why. The service writes a new row and supersedes the old one under the store's row
    lock, and refuses an overlap with another live row."""

    effective_to = forms.DateField(
        label="Effective to",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The last day this amount applies. Empty means it still applies.",
    )
    change_note = _change_note_field(
        "A note is required: say why the row is changed.",
        "Required: why the row is corrected. Kept with the history.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["effective_from"].help_text = "The first day this amount applies."

    def clean_change_note(self) -> str:
        # Invisible format characters and Unicode whitespace alone are not a note.
        note = visible_text(self.cleaned_data["change_note"])
        if not note:
            raise forms.ValidationError(self.fields["change_note"].error_messages["required"])
        return note

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        start = data.get("effective_from")
        end = data.get("effective_to")
        if start is not None and end is not None and end < start:
            self.add_error("effective_to", "The end date cannot be before the start date.")
        return data


class StoreCostCloseForm(forms.Form):
    """Close the open cost row on a date (admin, PLAN-010c D10)."""

    effective_to = forms.DateField(
        label="Last day",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The last day the current landlord rent applies.",
    )
    change_note = _change_note_field(
        "A note is required: say why the row is closed.", "Required: why the row is closed."
    )

    def clean_change_note(self) -> str:
        note = visible_text(self.cleaned_data["change_note"])
        if not note:
            raise forms.ValidationError(self.fields["change_note"].error_messages["required"])
        return note


# --- Deposit resolution (PLAN-010b D7) -------------------------------------------------------

RESOLUTION_METHOD_CHOICES = (("", "—"), *MANUAL_METHOD_CHOICES)


class DepositResolutionForm(forms.Form):
    """Resolve a deposit at the end of its lease (admin). The service re-checks every
    rule under the charge's row lock; the form checks shape only."""

    kind = forms.ChoiceField(choices=DepositResolutionKind.choices, label="Resolution")
    amount = forms.DecimalField(
        min_value=Decimal("0.01"), max_value=RENT_MAX, max_digits=12, decimal_places=2
    )
    resolved_on = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    method = forms.ChoiceField(
        choices=RESOLUTION_METHOD_CHOICES,
        required=False,
        help_text="Refunded only: how the money was paid back.",
    )
    to_lease = forms.UUIDField(
        required=False,
        label="Transfer to lease",
        widget=forms.Select(),
        help_text="Transferred only: another lease of the same operator that has not ended.",
    )
    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: what happened to the deposit, and why any part was kept.",
        error_messages={"required": DEPOSIT_NOTE_REQUIRED},
    )

    def __init__(
        self, *args: Any, targets: list[tuple[str, str]] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        widget = self.fields["to_lease"].widget
        if isinstance(widget, forms.Select):
            widget.choices = [("", "—"), *(targets or [])]

    def clean_note(self) -> str:
        # Invisible format characters and Unicode whitespace alone are not a note.
        note = visible_text(self.cleaned_data["note"])
        if not note:
            raise forms.ValidationError(DEPOSIT_NOTE_REQUIRED)
        return note


# --- End of lease (PLAN-012e D4, D5) -------------------------------------------------------

#: Deduction rows the statement step shows.
DEDUCTION_ROWS = 8


class LeaseEndTermsForm(forms.Form):
    """Step 1 of the end-of-lease wizard; the service checks every rule again."""

    ended_on = forms.DateField(
        label="End date",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The last day the operator had the store: not before the lease started, "
        "not in the future.",
        error_messages={"required": END_DATE_REQUIRED},
    )
    reason = forms.ChoiceField(
        choices=[("", "—"), *LeaseEndReason.choices],
        help_text="Why the lease ends; Other needs a few words below.",
    )
    reason_note = forms.CharField(
        label="Reason in words",
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 2, "maxlength": PAYMENT_NOTE_MAX_LENGTH}),
        help_text="Optional, except for Other; never a card or account number.",
    )
    prorate_last_month = forms.BooleanField(
        label="Prorate last month",
        required=False,
        initial=True,
        help_text="Charge the last month for the days up to the end date only.",
    )

    def __init__(self, *args: Any, fixed_reason: str = "", **kwargs: Any) -> None:
        """``fixed_reason``: the reason cannot be changed (a transfer's lease end)."""
        super().__init__(*args, **kwargs)
        if fixed_reason:
            reason = self.fields["reason"]
            reason.initial = fixed_reason
            reason.disabled = True
            reason.help_text = "A transfer ends the lease; the reason is fixed."


class LeaseEndDeductionsForm(forms.Form):
    """Step 3: the typed deductions from the deposit, one row each; an empty row is
    skipped. The service checks every rule again."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for index in range(1, DEDUCTION_ROWS + 1):
            self.fields[f"kind_{index}"] = forms.ChoiceField(
                label="Kind", required=False, choices=[("", "—"), *DeductionKind.choices]
            )
            self.fields[f"description_{index}"] = forms.CharField(
                label="Description", required=False, strip=False, max_length=200
            )
            self.fields[f"amount_{index}"] = forms.DecimalField(
                label="Amount",
                required=False,
                min_value=Decimal("0.01"),
                max_value=RENT_MAX,
                max_digits=12,
                decimal_places=2,
            )

    def rows(self) -> list[dict[str, forms.BoundField]]:
        return [
            {part: self[f"{part}_{index}"] for part in ("kind", "description", "amount")}
            for index in range(1, DEDUCTION_ROWS + 1)
        ]

    def lines(self) -> list[tuple[str, str, Decimal]]:
        """The filled rows as (kind, description, amount); call after validation."""
        found = []
        for index in range(1, DEDUCTION_ROWS + 1):
            kind = self.cleaned_data.get(f"kind_{index}") or ""
            amount = self.cleaned_data.get(f"amount_{index}")
            description = self.cleaned_data.get(f"description_{index}") or ""
            if not kind and amount is None and not description.strip():
                continue
            found.append((kind, description, amount if amount is not None else Decimal("0")))
        return found


class LeaseEndSettlementForm(forms.Form):
    """Step 4: how a refund is paid back. The service checks every rule again."""

    method = forms.ChoiceField(
        label="Refund method",
        required=False,
        choices=[("", "—"), *RefundMethod.choices],
        help_text="How the refund is paid: Zelle, wire or check, never the payment provider.",
    )
    refund_on = forms.DateField(
        label="Refund date",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The day the refund is sent; not in the future.",
    )
    reference = forms.CharField(
        label="Reference",
        required=False,
        strip=False,
        max_length=100,
        help_text="A Zelle confirmation, a wire reference or a check number; no account number.",
    )
    note = forms.CharField(
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 2, "maxlength": PAYMENT_NOTE_MAX_LENGTH}),
        help_text="Anything staff should know about the settlement.",
    )


class LeaseEndConfirmForm(forms.Form):
    confirmed = forms.BooleanField(
        label="I have read the effects above and want to end the lease",
        required=True,
        widget=_ExactCheckboxInput,
        error_messages={"required": "Tick the box to confirm."},
    )


# --- Transfer (PLAN-012e D6) ---------------------------------------------------------------


class TransferOperatorForm(forms.Form):
    """Step 1 of the transfer: the operator picked from the search. The service checks
    that it is not archived and not the outgoing one."""

    operator = forms.UUIDField(
        widget=forms.HiddenInput, error_messages={"required": "Choose an operator."}
    )


class TransferIncomingForm(forms.Form):
    """The incoming lease's terms, its deposit and its setup checklist (one tick and price
    per offered catalogue item that is not a deposit, the price defaulting from the
    catalogue). The service checks every rule again."""

    started_on = forms.DateField(
        label="Start date",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The first day of the incoming lease: the day after the outgoing lease "
        "ends, or later.",
    )
    rent = forms.DecimalField(
        label="Monthly rent",
        min_value=Decimal("0.01"),
        max_value=RENT_MAX,
        max_digits=12,
        decimal_places=2,
    )
    debit_day = forms.IntegerField(label="Debit day", min_value=1, max_value=31)
    second_debit_day = forms.IntegerField(
        label="Second debit day",
        min_value=1,
        max_value=31,
        required=False,
        help_text="Only for a lease debited twice a month.",
    )
    payment_channel = forms.ChoiceField(
        label="Payment channel",
        choices=[("", "—")]
        + [
            (value, label)
            for value, label in PaymentChannel.choices
            if value != PaymentChannel.UNKNOWN
        ],
        help_text="How the incoming operator pays the rent.",
    )
    prorate_first_month = forms.BooleanField(
        label="Prorate first month",
        required=False,
        initial=True,
        help_text="When the lease starts after the 1st, the first month's rent is charged "
        "for the days used only.",
    )
    deposit_amount = forms.DecimalField(
        label="Security deposit",
        required=False,
        min_value=Decimal("0.00"),
        max_value=RENT_MAX,
        max_digits=12,
        decimal_places=2,
        help_text="Paid by the incoming operator; zero or empty for none. It goes on the "
        "move-in invoice draft.",
    )

    def __init__(self, *args: Any, fee_types: Iterable[FeeType] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fee_types = list(fee_types)
        for fee_type in self.fee_types:
            name = _setup_name(fee_type)
            self.fields[f"{name}_tick"] = forms.BooleanField(
                required=False, widget=_ExactCheckboxInput, label=fee_type.name
            )
            self.fields[f"{name}_price"] = _money_field(
                "Price", minimum=Decimal("0.01"), initial=fee_type.default_price
            )

    def lease_fields(self) -> list[forms.BoundField]:
        return [field for field in self.visible_fields() if not field.name.startswith(SETUP)]

    def setup_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "fee_type": fee_type,
                "tick": self[f"{_setup_name(fee_type)}_tick"],
                "price": self[f"{_setup_name(fee_type)}_price"],
            }
            for fee_type in self.fee_types
        ]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        for fee_type in self.fee_types:
            name = _setup_name(fee_type)
            if data.get(f"{name}_tick") is True and data.get(f"{name}_price") is None:
                if f"{name}_price" not in self.errors:
                    self.add_error(f"{name}_price", SETUP_PRICE_REQUIRED)
        return data

    def setup_items(self) -> list[tuple[FeeType, Decimal]]:
        """The ticked rows of a valid form as (fee type, price), in catalogue order."""
        items = []
        for fee_type in self.fee_types:
            name = _setup_name(fee_type)
            price = self.cleaned_data.get(f"{name}_price")
            if self.cleaned_data.get(f"{name}_tick") is True and price is not None:
                items.append((fee_type, price))
        return items


class TransferConfirmForm(forms.Form):
    confirmed = forms.BooleanField(
        label="I have read the effects above and want to transfer the store",
        required=True,
        widget=_ExactCheckboxInput,
        error_messages={"required": "Tick the box to confirm."},
    )


class TransferDebtOverrideForm(forms.Form):
    """Allow a lease-end balance to remain after a transfer (PLAN-012e D14, admin): a
    written reason, nothing else."""

    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: why the balance may remain on the outgoing operator.",
        error_messages={"required": "A reason is required: say why the balance may remain."},
    )

    def clean_reason(self) -> str:
        reason = visible_text(self.cleaned_data["reason"])
        if not reason:
            raise forms.ValidationError(self.fields["reason"].error_messages["required"])
        return reason


# --- Documents (PLAN-010c D7) --------------------------------------------------------------

DOCUMENT_NOTE_MAX_LENGTH = 2000
DOCUMENT_FILE_REQUIRED = "Choose a file to upload."
DOCUMENT_FILE_ACCEPT = "image/*,.pdf,.docx,.heic"
DOCUMENT_PHOTO_ACCEPT = "image/*"


#: The types dispute evidence may be (PLAN-012 D8); narrows the file picker only.
EVIDENCE_FILE_ACCEPT = ".pdf,.jpg,.jpeg,.png,.tif,.tiff"


class DisputeEvidenceForm(forms.Form):
    """One evidence file for a dispute (PLAN-012 D8). Shape only: the service checks the
    name, the size, the type, the notice for an image and the respond-by date, and the
    documents upload runs the 10c checks, so every refusal is audited."""

    name = forms.CharField(label="Name", strip=False, max_length=4 * EVIDENCE_NAME_MAX_LENGTH)
    file = forms.FileField(
        label="File",
        allow_empty_file=True,
        widget=forms.FileInput(attrs={"accept": EVIDENCE_FILE_ACCEPT}),
    )
    acknowledged = forms.BooleanField(
        label="This is a receipt, lease page or correspondence, not a bank form",
        required=False,
    )


class DocumentUploadForm(forms.Form):
    """One file, its kind and an optional note. Shape only: the service decides the
    type from the content, checks the size, the kind and the note, and runs the
    payment-data guard (D5, D6), so that every refusal is audited in one place.

    Rendered with the prefix ``document`` on the store and lease create forms, where the
    file is optional and uploaded after the record is saved.
    """

    file = forms.FileField(
        label="File",
        # Required with the photo below: one of the two (clean).
        required=False,
        # An empty file reaches the service, which refuses it and audits the refusal.
        allow_empty_file=True,
        # The type is decided from the content; this only narrows the file picker
        # (images include a phone's photo library; PLAN-011 D9).
        widget=forms.FileInput(attrs={"accept": DOCUMENT_FILE_ACCEPT, "class": "js-document-file"}),
    )
    #: "Take photo" (PLAN-011 D9): the phone opens its rear camera. Images only; one file
    #: per submit still (DATA_UPLOAD_MAX_NUMBER_FILES), so the page clears the other input.
    photo = forms.FileField(
        label="Take photo",
        required=False,
        allow_empty_file=True,
        widget=forms.FileInput(
            attrs={
                "accept": DOCUMENT_PHOTO_ACCEPT,
                "capture": "environment",
                "class": "js-document-photo",
            }
        ),
    )
    #: A select of the kinds; any other value reaches the service, which refuses it.
    kind = forms.CharField(
        label="Kind",
        widget=forms.Select(
            # Dispute evidence is kept on an operator, from the disputes page (PLAN-012 D8);
            # a final statement is made by the end-of-lease wizard (PLAN-012e D7).
            choices=[
                choice
                for choice in DocumentKind.choices
                if choice[0] not in (DocumentKind.DISPUTE_EVIDENCE, DocumentKind.FINAL_STATEMENT)
            ]
        ),
    )
    note = forms.CharField(
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 2, "maxlength": DOCUMENT_NOTE_MAX_LENGTH}),
    )

    def clean(self) -> dict[str, Any]:
        """The file comes from either input; the photo becomes ``file``."""
        cleaned = super().clean() or {}
        photo = cleaned.get("photo")
        if cleaned.get("file") is None and photo is not None:
            cleaned["file"] = photo
        if cleaned.get("file") is None and "file" not in self.errors:
            self.add_error("file", DOCUMENT_FILE_REQUIRED)
        return cleaned


class DocumentDeleteForm(forms.Form):
    """Delete a document (admin): a note, nothing else."""

    note = forms.CharField(
        max_length=DOCUMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        error_messages={"required": "A note is required: say why the document is deleted."},
    )

    def clean_note(self) -> str:
        # Invisible format characters and Unicode whitespace alone are not a note.
        note = visible_text(self.cleaned_data["note"])
        if not note:
            raise forms.ValidationError(self.fields["note"].error_messages["required"])
        return note


# --- Contact log and calendar (PLAN-011b D7, D8) -------------------------------------------

#: The longest contact note accepted.
CONTACT_BODY_MAX_LENGTH = 2000


class ContactEditForm(forms.Form):
    """The fields of a contact entry its author may change (PLAN-012 D2). The form checks
    shape; the service (:mod:`ledger.services.contacts`) decides every rule."""

    kind = forms.ChoiceField(
        choices=ContactKind.choices, error_messages={"required": "Choose the kind of contact."}
    )
    happened_on = forms.DateField(
        label="Date", widget=forms.DateInput(attrs={"type": "date"}), help_text="New York date."
    )
    promised_date = forms.DateField(
        required=False,
        label="Promised date",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Promise to pay only.",
    )
    promised_amount = forms.DecimalField(
        required=False,
        label="Promised amount (USD)",
        min_value=Decimal("0.01"),
        max_value=Decimal("9999999999.99"),
        max_digits=12,
        decimal_places=2,
        help_text="Promise to pay only.",
    )
    body = forms.CharField(
        label="Note",
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=CONTACT_BODY_MAX_LENGTH,
        validators=[validate_no_payment_data],
        error_messages={"required": "A note is required: say what was said or agreed."},
    )

    def clean_body(self) -> str:
        body = visible_text(self.cleaned_data["body"])
        if not body:
            raise forms.ValidationError(self.fields["body"].error_messages["required"])
        return body


#: The outcomes of a previous promise, as the entry form offers them (PLAN-012 D5); "Still
#: open" first, the default (PLAN-012c-b D1).
OUTCOME_CHOICES = [
    ("open", "Still open (no change)"),
    ("kept", "Kept"),
    ("rescheduled", "Rescheduled"),
    ("broken", "Broken"),
]


class ContactEntryForm(ContactEditForm):
    """Add one contact entry to an operator's log (PLAN-011b D7, PLAN-012 D3, D5).

    ``lease``: required when the operator has more than one lease in effect
    (``lease_required``). ``previous`` and ``outcome``: offered when the operator has an
    open promise (``promises``); ``previous`` is required, ``outcome`` defaults to "Still
    open" (PLAN-012c-b D1). Each choice of ``promises`` may carry the promise's date and
    amount for the page's "Rescheduled" preselection."""

    lease = forms.ChoiceField(required=False, label="Store")
    previous = forms.ChoiceField(required=False, label="Previous promise")
    outcome = forms.ChoiceField(
        required=False,
        label="What happened to it",
        choices=OUTCOME_CHOICES,
        widget=forms.RadioSelect,
        initial="open",
    )
    field_order = [
        "lease",
        "previous",
        "outcome",
        "kind",
        "happened_on",
        "promised_date",
        "promised_amount",
        "body",
    ]

    def __init__(
        self,
        *args: Any,
        leases: list[tuple[str, str]],
        lease_required: bool,
        promises: list[tuple[str, str]],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        lease = self.fields["lease"]
        if isinstance(lease, forms.ChoiceField):
            lease.choices = ([] if lease_required else [("", "No particular store")]) + leases
            lease.required = lease_required
            lease.error_messages["required"] = "Choose the store this is about."
        if not leases:
            del self.fields["lease"]
        previous = self.fields["previous"]
        if promises and isinstance(previous, forms.ChoiceField):
            previous.choices = promises
            previous.required = True
        else:
            del self.fields["previous"]
            del self.fields["outcome"]


#: How the outcome of a promise was learned (PLAN-012c-b D2): the kinds of a Kept or Broken
#: entry; a Reschedule records a promise.
OUTCOME_KINDS = [
    (ContactKind.CALL.value, ContactKind.CALL.label),
    (ContactKind.EMAIL.value, ContactKind.EMAIL.label),
    (ContactKind.VISIT.value, ContactKind.VISIT.label),
]


class ContactOutcomeForm(ContactEditForm):
    """Kept, Reschedule or Broken on one open promise (PLAN-012c-b D2): the date, a note,
    and for ``rescheduled`` the new promised date and amount (required); for ``kept`` and
    ``broken``, how it was learned (call, e-mail or visit). ``leases``: the store choice,
    offered only when the promise names no store and the operator has more than one lease
    in effect (an entry must then name one)."""

    lease = forms.ChoiceField(
        label="Store", error_messages={"required": "Choose the store this is about."}
    )
    field_order = ["lease", "kind", "happened_on", "promised_date", "promised_amount", "body"]

    def __init__(
        self, *args: Any, outcome: str, leases: list[tuple[str, str]], **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        lease = self.fields["lease"]
        if leases and isinstance(lease, forms.ChoiceField):
            lease.choices = leases
        else:
            del self.fields["lease"]
        kind = self.fields["kind"]
        if outcome == ContactStatus.RESCHEDULED:
            del self.fields["kind"]
            for name in ("promised_date", "promised_amount"):
                self.fields[name].required = True
                self.fields[name].help_text = "The new promise; it starts from the old one."
        else:
            if isinstance(kind, forms.ChoiceField):
                kind.choices = OUTCOME_KINDS
                kind.label = "How you learned it"
            del self.fields["promised_date"]
            del self.fields["promised_amount"]


class ContactCorrectionForm(forms.Form):
    """A correction appended to a contact entry (PLAN-012 D2)."""

    body = forms.CharField(
        label="Correction",
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=CONTACT_BODY_MAX_LENGTH,
        validators=[validate_no_payment_data],
        error_messages={"required": "Say what is corrected."},
    )


class ContactDeleteForm(forms.Form):
    """The reason an admin deletes a contact entry (PLAN-012 D2)."""

    reason = forms.CharField(
        label="Reason",
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=CONTACT_BODY_MAX_LENGTH,
        validators=[validate_no_payment_data],
        error_messages={"required": "A reason is required: say why this entry is deleted."},
    )


class CalendarEntryForm(forms.Form):
    """Add or change a calendar note or reminder (D8). Record choices are UUIDs; the
    view checks they exist."""

    kind = forms.ChoiceField(
        choices=CalendarEntryKind.choices, error_messages={"required": "Choose note or reminder."}
    )
    on = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    title = forms.CharField(
        max_length=CALENDAR_TITLE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        error_messages={"required": "A title is required."},
    )
    body = forms.CharField(
        required=False,
        label="Text",
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=2000,
        validators=[validate_no_payment_data],
    )
    email_morning = forms.BooleanField(
        required=False,
        label="E-mail me that morning",
        help_text="An e-mail to you on the morning of that day (09:00 New York).",
    )
    store = forms.ChoiceField(required=False, label="Store")
    operator = forms.ChoiceField(required=False, label="Operator")
    lease = forms.ChoiceField(required=False, label="Lease")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        choices: dict[str, list[tuple[str, str]]] = {
            "store": [
                (str(pk), label)
                for pk, label in Store.objects.order_by("external_label", "id").values_list(
                    "pk", "external_label"
                )
            ],
            "operator": [
                (str(pk), label)
                for pk, label in Operator.objects.filter(archived_at__isnull=True)
                .order_by("display_name", "id")
                .values_list("pk", "display_name")
            ],
            "lease": [
                (str(pk), f"{label} from {started or 'no start date'}")
                for pk, label, started in Lease.objects.order_by(
                    "store__external_label", "started_on", "id"
                ).values_list("pk", "store__external_label", "started_on")
            ],
        }
        for name, options in choices.items():
            field = self.fields[name]
            if isinstance(field, forms.ChoiceField):
                field.choices = [("", "None")] + options

    def clean_title(self) -> str:
        title = visible_text(self.cleaned_data["title"])
        if not title:
            raise forms.ValidationError(self.fields["title"].error_messages["required"])
        return title


# --- Installment plans (PLAN-012e D10, D11) ----------------------------------------------


def _cadence_field() -> forms.ChoiceField:
    return forms.ChoiceField(
        choices=InstallmentCadence.choices,
        initial=InstallmentCadence.MONTHLY,
        error_messages={
            "required": "Choose weekly, every two weeks or monthly.",
            "invalid_choice": "Choose weekly, every two weeks or monthly.",
        },
    )


def _first_due_field() -> forms.DateField:
    return forms.DateField(
        label="First installment's date",
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={
            "required": "Enter the first installment's date.",
            "invalid": "Enter the first installment's date.",
        },
    )


class SplitForm(forms.Form):
    """Split into installments (D10, billing), first step: how many, from when, how often.
    Read from the query string: the preview that follows writes nothing."""

    count = forms.IntegerField(
        label="Number of installments",
        min_value=INSTALLMENTS_MIN,
        max_value=INSTALLMENTS_MAX,
        initial=3,
        error_messages={
            "required": f"Choose between {INSTALLMENTS_MIN} and {INSTALLMENTS_MAX} installments.",
            "invalid": f"Choose between {INSTALLMENTS_MIN} and {INSTALLMENTS_MAX} installments.",
            "min_value": f"Choose between {INSTALLMENTS_MIN} and {INSTALLMENTS_MAX} installments.",
            "max_value": f"Choose between {INSTALLMENTS_MIN} and {INSTALLMENTS_MAX} installments.",
        },
    )
    first_due = _first_due_field()
    cadence = _cadence_field()


class PlanRescheduleForm(forms.Form):
    """Reschedule an installment plan (D11, billing): the unpaid installments' new start."""

    first_due = _first_due_field()
    cadence = _cadence_field()


class PlanCancelForm(CancelChargeForm):
    """Cancel an installment plan (D11, admin): a note, nothing else."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        note = self.fields["note"]
        note.help_text = "Required: why the plan is cancelled."
        note.error_messages["required"] = "A note is required: say why the plan is cancelled."
