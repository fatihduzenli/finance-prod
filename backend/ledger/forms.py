"""Operator, lease and store forms (docs/qa/PLAN-006-operators.md,
PLAN-007-leases-and-rent-run.md, PLAN-007b-stores.md).

Server-side validation is what counts; the browser checks (``type=email``,
``required``) are a convenience. Every text field keeps the model's card-number guard,
which runs when the form validates the model instance.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django import forms
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from finance.dates import day_text
from finance.templatetags.phones import phone_text
from finance.us_states import US_STATES
from ledger.models import (
    AUTOPAY_COUNT_MAX,
    AUTOPAY_COUNT_MIN,
    CALENDAR_TITLE_MAX_LENGTH,
    CONSENT_FIELDS,
    DESCRIPTION_MAX_LENGTH,
    EVIDENCE_NAME_MAX_LENGTH,
    FREE_RENT_DAYS_MAX,
    INSTALLMENTS_MAX,
    INSTALLMENTS_MIN,
    NAME_MAX_LENGTH,
    NOTE_BODY_MAX_LENGTH,
    NOTE_DELETE_REASON_MAX_LENGTH,
    ZERO,
    AutopayCadence,
    AutopayEndRule,
    Brand,
    CalendarEntryKind,
    CollectionStatus,
    ContactKind,
    ContactStatus,
    DeductionKind,
    DepositResolutionKind,
    DocumentKind,
    FeeType,
    FreeRentFrom,
    InstallmentCadence,
    Lease,
    LeaseBilling,
    LeaseEndReason,
    OneOffCharge,
    Operator,
    PaymentChannel,
    PaymentMethod,
    RefundMethod,
    Store,
)
from ledger.services import (
    autopay_plans,
    deposits,
    free_rent,
    installments,
    invoice_builder,
    one_off_charges,
    proration,
    rent_run,
)
from ledger.services.contacts import PROMISE_FIELDS_REQUIRED
from ledger.services.deposits import NOTE_REQUIRED as DEPOSIT_NOTE_REQUIRED
from ledger.services.leases import (
    ACCOUNT_ENTER,
    ACCOUNT_LATER,
    ACCOUNT_NOT_SHAREABLE,
    ACCOUNT_SAME,
    END_DATE_REQUIRED,
    SharedAccount,
)
from ledger.services.one_off_charges import ChargeInput
from ledger.services.stores import crumb as store_crumb
from ledger.services.workspace_cards import initials
from ledger.validators import validate_no_payment_data, visible_text
from ledger.wording import format_amount, provider_sentence
from payments.lease_sync import normalise_phone

if TYPE_CHECKING:
    _OperatorFormBase = forms.ModelForm[Operator]
    _LeaseFormBase = forms.ModelForm[Lease]
    _LeaseBillingFormBase = forms.ModelForm[LeaseBilling]
    _StoreFormBase = forms.ModelForm[Store]
    _BillingStepBase = forms.BaseForm
else:  # ModelForm is only generic in the type stubs.
    _OperatorFormBase = forms.ModelForm
    _LeaseFormBase = forms.ModelForm
    _LeaseBillingFormBase = forms.ModelForm
    _StoreFormBase = forms.ModelForm
    #: A mixin, not a form: the form classes of this module are each tested as a form.
    _BillingStepBase = object

#: The attribute of a dependent field (PLAN-012c D8): the field is shown while one of the
#: ``|``-separated conditions holds, each ``<control id>`` (a ticked box or a filled input)
#: or ``<control id>=<value>``; ``ledger/shown_when.js`` hides it otherwise and does not
#: send it. Without the script the field stays visible; the server's rules never change.
SHOWN_WHEN = "data-shown-when"
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


#: Every select's refusal (PLAN-012f-b finding B3-1): Django's own message repeats the
#: posted value, which may be anything, a card number too.
CHOICE_REFUSED = "Choose one of the options."


class _QuietChoices(forms.Field):
    """The refusal of every select in this module: its own ``invalid_choice`` message,
    which never names the posted value (``CHOICE_REFUSED`` unless the field words it).
    A model select's value that is not a UUID is refused with the same message; Django
    reports it with the value in the text."""

    default_error_messages = {"invalid_choice": CHOICE_REFUSED}

    def to_python(self, value: Any) -> Any:
        try:
            return super().to_python(value)
        except forms.ValidationError:
            raise forms.ValidationError(
                self.error_messages["invalid_choice"], code="invalid_choice"
            ) from None


class _QuietChoiceField(_QuietChoices, forms.ChoiceField):
    pass


class _QuietTypedChoiceField(_QuietChoices, forms.TypedChoiceField):
    pass


class _QuietModelChoiceField(_QuietChoices, forms.ModelChoiceField):  # type: ignore[type-arg]
    pass


def _quiet_formfield(db_field: models.Field[Any, Any], **kwargs: Any) -> forms.Field | None:
    """A model form's field for ``db_field``, its select quiet (``_QuietChoices``): a
    foreign key's and a choice column's."""
    if isinstance(db_field, models.ForeignKey):
        kwargs.setdefault("form_class", _QuietModelChoiceField)
    elif db_field.choices:
        kwargs.setdefault("choices_form_class", _QuietTypedChoiceField)
    return db_field.formfield(**kwargs)


#: The State select of the store and operator forms (PLAN-012f-b D11, PLAN-014 I-b): "—" for
#: none, then the full names; the values are the two-letter codes stored.
STATE_CHOICES: tuple[tuple[str, str], ...] = (("", "\u2014"), *US_STATES)
STATE_REFUSED = "Choose a state from the list."


#: The contact detail each consent needs on the operator form (PLAN-012c D8).
CONSENT_SHOWN_WHEN = {"sms_consent_at": "id_sms_phone|id_phone", "email_consent_at": "id_email"}


class OperatorForm(_OperatorFormBase):
    """Create and edit (PLAN-014 C1: an operator is a person). The fields are the Full name
    (required; stored as the display name), the e-mail, the phone, the address, the notes
    and the SMS phone and consents. The e-mail is lower-cased and stripped; empty means
    none. The phone must pass the payment provider's rule, as before. The State is the store
    form's select of the 50 states and DC, the code stored (PLAN-014 I-b).

    The type, company name, contact person, first and last name and legal name left the
    form (the columns stay, unwritten); a post that still sends them is not refused: the
    model form ignores fields it does not have.
    """

    email = forms.EmailField(required=False, max_length=254)
    # PLAN-014 I-b: the store form's State select; the refusal never repeats the posted value.
    state = _QuietChoiceField(
        choices=STATE_CHOICES,
        required=False,
        label="State",
        error_messages={"invalid_choice": STATE_REFUSED},
    )

    class Meta:
        model = Operator
        fields = (
            "display_name",
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
            "display_name": "Full name",
            "sms_phone": "Text-message phone (international, +1…)",
            "sms_consent_at": "Text-message consent given on",
            "email_consent_at": "E-mail consent given on",
        }
        error_messages = {"display_name": {"required": "Enter the full name."}}
        # PLAN-012c D8: a consent is shown only with a phone (SMS) or an e-mail on the form.
        widgets = {
            name: forms.DateTimeInput(
                attrs={
                    "type": "datetime-local",
                    "step": "1",
                    SHOWN_WHEN: CONSENT_SHOWN_WHEN[name],
                },
                format=CONSENT_INPUT_FORMAT,
            )
            for name in CONSENT_FIELDS
        }

    def clean_display_name(self) -> str:
        """The full name with its spaces collapsed; empty is refused."""
        value = " ".join(str(self.cleaned_data.get("display_name") or "").split())
        if not value:
            raise forms.ValidationError("Enter the full name.", code="required")
        return value

    def clean_email(self) -> str | None:
        value = self.cleaned_data.get("email") or ""
        return value.strip().lower() or None

    def clean_phone(self) -> str:
        """The provider's phone rule; the normalised digits are stored, empty stays empty."""
        digits = normalise_phone(str(self.cleaned_data.get("phone") or ""))
        if digits is None:
            raise forms.ValidationError(PHONE_MESSAGE, code="phone")
        return digits


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
        label=provider_sentence("{provider} customer ID"),
        max_length=IDENTIFIER_MAX_LENGTH,
        validators=[IDENTIFIER_VALIDATOR],
    )
    payment_method_id = forms.CharField(
        label=provider_sentence("{provider} bank method ID"),
        max_length=IDENTIFIER_MAX_LENGTH,
        validators=[IDENTIFIER_VALIDATOR],
    )


class LeaseBillingForm(_LeaseBillingFormBase):
    """The lease's billing details (PLAN-014 C3, the LEASE card's "Edit billing details"):
    the company billed, the e-mail receipts and messages go to, the phone and the address.
    The e-mail is lower-cased and stripped; the phone must pass the payment provider's rule,
    as on the operator form. Nothing else of the lease is posted here."""

    email = forms.EmailField(required=False, max_length=254, label="E-mail")

    class Meta:
        model = LeaseBilling
        fields = (
            "company_name",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
        )
        labels = {
            "company_name": "Company name",
            "address_line1": "Address line 1",
            "address_line2": "Address line 2",
            "postal_code": "ZIP code",
        }

    def clean_email(self) -> str:
        return str(self.cleaned_data.get("email") or "").strip().lower()

    def clean_phone(self) -> str:
        """The provider's phone rule; the normalised digits are stored, empty stays empty."""
        digits = normalise_phone(str(self.cleaned_data.get("phone") or ""))
        if digits is None:
            raise forms.ValidationError(PHONE_MESSAGE, code="phone")
        return digits


# --- The billing details of a new lease (PLAN-014 C4) -----------------------------------

#: The billing details fields of the New lease page and the transfer's incoming lease, as
#: on Edit billing details (``LeaseBillingForm``), and the bank-account choice.
BILLING_STEP_FIELDS: tuple[str, ...] = (*LeaseBillingForm.Meta.fields, "account")
#: The operator's contact details the "Copy from operator" button fills in the browser:
#: the ``data-*`` attribute of each billing field (``data-email`` fills ``email``).
COPIED_FIELDS: tuple[str, ...] = (
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
)
ACCOUNT_LATER_LABEL = "Later"
ACCOUNT_ENTER_LABEL = "Enter a bank account"
ACCOUNT_SAME_LABEL = "Use the same account as #{store}{last4}"
#: The New lease page's bank-account segments (PLAN-014b item 1, the owner's reference): Later |
#: Use #1991's account | Enter a new account.
NEW_LEASE_ENTER_LABEL = "Enter a new account"
NEW_LEASE_SAME_LABEL = "Use {store}'s account"


def copied_attrs(operator: Operator | None) -> dict[str, str]:
    """The ``data-*`` attributes "Copy from operator" reads: the operator's e-mail, phone
    and address (nothing is copied by the server)."""
    if operator is None:
        return {}
    return {
        f"data-{name.replace('_', '-')}": str(getattr(operator, name) or "")
        for name in COPIED_FIELDS
    }


class OperatorContactSelect(forms.Select):
    """The New lease page's operator select: each option carries the operator's contact
    details as ``data-*`` attributes for "Copy from operator" (PLAN-014 C4), and for the
    operator card (PLAN-014b item 1, decision 3) its initials (``data-initials``), its phone
    as shown (``data-phone-text``) and its other current stores (``data-stores``, "#no Name"
    comma-separated, and ``data-store-count``) from ``stores`` (read once for every operator
    by the view)."""

    def __init__(
        self, attrs: dict[str, Any] | None = None, stores: dict[uuid.UUID, list[str]] | None = None
    ) -> None:
        super().__init__(attrs)
        self.stores = stores or {}

    def create_option(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        option = super().create_option(*args, **kwargs)
        instance = getattr(option["value"], "instance", None)
        if isinstance(instance, Operator):
            option["attrs"].update(copied_attrs(instance))
            found = self.stores.get(instance.pk, [])
            option["attrs"].update(
                {
                    "data-initials": initials(instance.display_name),
                    "data-phone-text": phone_text(instance.phone),
                    "data-stores": ", ".join(found),
                    "data-store-count": str(len(found)),
                }
            )
        return option


class BillingStepMixin(_BillingStepBase):
    """The Billing details of a new lease (PLAN-014 C4): the ``LeaseBillingForm`` fields,
    starting empty, and **Bank account**: ``later`` (the default), ``enter`` (the hosted
    fields after the save; refused while they are not set up) or ``same:<lease id>`` (the
    account of a lease of :func:`~ledger.services.leases.shareable_accounts`, which the
    service checks again under the row lock)."""

    shared_accounts: list[SharedAccount]
    enter_refusal: str
    same_shown_when: str
    #: The labels of "Enter a bank account" and of a ``same:`` option (the New lease page
    #: words them as the owner's reference, PLAN-014b item 1).
    enter_label: str = ACCOUNT_ENTER_LABEL
    short_same_label: bool = False

    def add_billing_step(
        self,
        accounts: Sequence[SharedAccount],
        *,
        enter_refusal: str,
        shown_when: str = "",
    ) -> None:
        """Add the fields; ``shown_when`` (the operator select's id) hides the billing
        fields until an operator is chosen and each ``same:`` option until its lease's
        operator is the one chosen."""
        for name in LeaseBillingForm.Meta.fields:
            field = copy.deepcopy(LeaseBillingForm.base_fields[name])
            if shown_when:
                field.widget.attrs[SHOWN_WHEN] = shown_when
            self.fields[name] = field
        self.shared_accounts = list(accounts)
        self.enter_refusal = enter_refusal
        self.same_shown_when = shown_when
        self.fields["account"] = _QuietChoiceField(
            label="Bank account",
            required=False,
            initial=ACCOUNT_LATER,
            choices=[
                (ACCOUNT_LATER, ACCOUNT_LATER_LABEL),
                (ACCOUNT_ENTER, self.enter_label),
                *((found.value, found.value) for found in self.shared_accounts),
            ],
            widget=forms.RadioSelect,
        )

    def clean_email(self) -> str:
        return str(self.cleaned_data.get("email") or "").strip().lower()

    def clean_phone(self) -> str:
        """The provider's phone rule, as on Edit billing details."""
        digits = normalise_phone(str(self.cleaned_data.get("phone") or ""))
        if digits is None:
            raise forms.ValidationError(PHONE_MESSAGE, code="phone")
        return digits

    def clean_account(self) -> str:
        value = str(self.cleaned_data.get("account") or "") or ACCOUNT_LATER
        if value == ACCOUNT_ENTER and self.enter_refusal:
            raise forms.ValidationError(self.enter_refusal, code="enter")
        if value.startswith(ACCOUNT_SAME) and "operator" in self.fields:
            # PLAN-014b item 1 (decision 4): the account of another operator's lease is refused
            # here already; the service checks it again under the row lock.
            chosen = self.cleaned_data.get("operator")
            shared = next((found for found in self.shared_accounts if found.value == value), None)
            if shared is None or chosen is None or shared.lease.operator_id != chosen.pk:
                raise forms.ValidationError(ACCOUNT_NOT_SHAREABLE, code="not_shareable")
        return value

    def billing_values(self) -> dict[str, str]:
        """The billing details as typed (a valid form)."""
        return {
            name: str(self.cleaned_data.get(name) or "") for name in LeaseBillingForm.Meta.fields
        }

    def billing_fields(self) -> list[forms.BoundField]:
        return [self[name] for name in LeaseBillingForm.Meta.fields]

    def account_options(self) -> list[dict[str, Any]]:
        """The Bank account radios, rendered one by one (a ``same:`` radio carries its
        own ``data-shown-when``)."""
        chosen = str(self["account"].value() or ACCOUNT_LATER)
        options: list[dict[str, Any]] = [
            {"value": ACCOUNT_LATER, "label": ACCOUNT_LATER_LABEL},
            {"value": ACCOUNT_ENTER, "label": self.enter_label, "reason": self.enter_refusal},
        ]
        for found in self.shared_accounts:
            options.append(
                {
                    "value": found.value,
                    "label": (
                        NEW_LEASE_SAME_LABEL.format(store=store_crumb(found.lease.store))
                        if self.short_same_label
                        else ACCOUNT_SAME_LABEL.format(
                            store=found.lease.store.external_label,
                            last4=f" (····{found.last4})" if found.last4 else "",
                        )
                    ),
                    "shown_when": (
                        f"{self.same_shown_when}={found.lease.operator_id}"
                        if self.same_shown_when
                        else ""
                    ),
                    # PLAN-014b item 1 (decision 4): the lease's operator, on every same: radio.
                    "operator": str(found.lease.operator_id),
                    # The New lease page's line under the short segment: which account it is.
                    "detail": (
                        ACCOUNT_SAME_LABEL.format(
                            store=found.lease.store.external_label,
                            last4=f" (····{found.last4})" if found.last4 else "",
                        )
                        if self.short_same_label
                        else ""
                    ),
                }
            )
        for index, option in enumerate(options):
            option["id"] = f"id_account_{index}"
            option["checked"] = option["value"] == chosen and not option.get("reason")
            option.setdefault("reason", "")
            option.setdefault("shown_when", "")
            option.setdefault("operator", "")
            option.setdefault("detail", "")
        return options


# --- Leases (PLAN-007 §4.1) -------------------------------------------------------------

#: Largest rent the money column holds (numeric(12,2)).
RENT_MAX = Decimal("9999999999.99")
#: The lease form's "Debited twice a month" tick (PLAN-012c D8): a control of the page,
#: without a name, so it is never posted.
TWICE_MONTHLY_ID = "twice-monthly"


def _twice_monthly(second_debit_day: forms.BoundField) -> bool:
    """The "Debited twice a month" tick starts ticked when a second debit day is set,
    typed or refused (PLAN-012c D8)."""
    return bool(str(second_debit_day.value() or "").strip()) or bool(second_debit_day.errors)


#: PLAN-015d §3 (2): the "Free rent" fields of New lease and Edit lease.
FREE_RENT_DAYS_LABEL = "Free rent (days)"
FREE_RENT_FROM_LABEL = "Free rent starts"
FREE_RENT_CHANGE_LABEL = "Free rent"
#: The Starts select's short words (the field fits a third of the Terms row).
FREE_RENT_FROM_CHOICES = [
    (FreeRentFrom.MOVE_IN.value, "Move-in date"),
    (FreeRentFrom.NEXT_MONTH.value, "1st of next month"),
]


def _free_rent_fields(form: forms.BaseForm) -> None:
    """The free-rent days (0 to 366; empty is none) and where they start."""
    form.fields["free_rent_days"] = forms.IntegerField(
        label=FREE_RENT_DAYS_LABEL,
        min_value=0,
        max_value=FREE_RENT_DAYS_MAX,
        required=False,
        initial=0,
        widget=forms.NumberInput(attrs={"min": 0, "max": FREE_RENT_DAYS_MAX, "step": 1}),
    )
    form.fields["free_rent_from"] = _QuietChoiceField(
        label=FREE_RENT_FROM_LABEL,
        choices=FREE_RENT_FROM_CHOICES,
        initial=FreeRentFrom.MOVE_IN,
        # Not posted (an older form, a script-less post): from the move-in date.
        required=False,
    )


def _clean_free_rent(data: dict[str, Any]) -> None:
    if data.get("free_rent_days") is None:
        data["free_rent_days"] = 0
    if not data.get("free_rent_from"):
        data["free_rent_from"] = FreeRentFrom.MOVE_IN


class LeaseForm(_LeaseFormBase):
    """A new lease's own fields (the New lease page builds on it, :class:`LeaseCreateForm`).
    Rent is a separate field: the service writes its first history row (D5). The Edit lease
    page has its own form since PLAN-015a (:class:`LeaseEditForm`)."""

    #: The status whose rules a form without the Status field checks (the New lease page:
    #: ``active``; it saves a lease starting after today ``prospective``, PLAN-015b §9.1).
    fixed_status: str | None = None

    debit_day = forms.IntegerField(min_value=1, max_value=31, required=False)
    second_debit_day = forms.IntegerField(
        min_value=1,
        max_value=31,
        required=False,
        help_text="Only for a lease debited twice a month.",
        # PLAN-012c D8: shown while "Debited twice a month" is ticked; the tick is the
        # page's only (never posted), and the form's rules for this field are unchanged.
        widget=forms.NumberInput(attrs={SHOWN_WHEN: TWICE_MONTHLY_ID}),
    )
    rent = forms.DecimalField(
        label="Monthly rent",
        min_value=Decimal("0.01"),
        max_value=RENT_MAX,
        max_digits=12,
        decimal_places=2,
    )

    class Meta:
        model = Lease
        # The store and operator selects never repeat a refused value (B3-1).
        formfield_callback = _quiet_formfield
        fields = (
            "store",
            "operator",
            "debit_day",
            "second_debit_day",
            "payment_channel",
            "started_on",
            "prorate_first_month",
            "free_rent_days",
            "free_rent_from",
        )
        widgets = {"started_on": forms.DateInput(attrs={"type": "date"})}
        labels = {
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
        _free_rent_fields(self)
        store = self.fields["store"]
        operator = self.fields["operator"]
        if isinstance(store, forms.ModelChoiceField):
            store.queryset = Store.objects.order_by("external_label", "id")
        if isinstance(operator, forms.ModelChoiceField):
            current = self.instance.operator_id if self.instance.pk else None
            operator.queryset = Operator.objects.filter(
                Q(archived_at__isnull=True) | Q(pk=current)
            ).order_by("display_name", "id")

    def twice_monthly(self) -> bool:
        """Whether the page's "Debited twice a month" tick starts ticked (PLAN-012c D8)."""
        return _twice_monthly(self["second_debit_day"])

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        _clean_free_rent(data)
        active = data.get("collection_status", self.fixed_status) == CollectionStatus.ACTIVE
        if active and data.get("operator") is None:
            self.add_error("operator", "An active lease needs an operator.")
        _check_debits(self, data, active=active)
        return data


def _check_debits(form: forms.BaseForm, data: dict[str, Any], *, active: bool) -> None:
    """The debit rules of both lease forms: a second debit day needs a first one and differs
    from it; an active lease needs a debit day and a known channel."""
    day = data.get("debit_day")
    second = data.get("second_debit_day")
    if second is not None and day is None:
        form.add_error("second_debit_day", "Set the first debit day first.")
    elif second is not None and second == day:
        form.add_error("second_debit_day", "The second debit day must differ from the first.")
    if active:
        if day is None:
            form.add_error("debit_day", "An active lease needs a debit day.")
        if data.get("payment_channel") == PaymentChannel.UNKNOWN:
            form.add_error("payment_channel", "An active lease needs a known channel.")


def ordinal(day: int) -> str:
    """``1st``, ``2nd``, ``3rd``, ``4th`` ... ``11th`` ... ``21st`` (the Debit day select)."""
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


#: PLAN-014b item 1 (decision 2): the New lease page's Debit day, the 1st to the 28th.
DEBIT_DAY_CHOICES: tuple[tuple[int, str], ...] = tuple((day, ordinal(day)) for day in range(1, 29))
#: Decision 1: the New lease page's three payment-channel cards, each its stored value, its
#: title and its muted line (the provider's name from the settings, Rule 8).
CHANNEL_CARDS: tuple[tuple[str, Any, Any, Any], ...] = (
    (
        PaymentChannel.PROVIDER_ACH,
        "Autopay (bank debit)",
        "Debited from the store's bank account",
        "Autopay",
    ),
    (
        PaymentChannel.INVOICE,
        "E-mail invoice",
        "We e-mail a reminder with a pay link before each due date",
        "E-mail invoice",
    ),
    (
        PaymentChannel.OTHER,
        provider_sentence("Paid outside {provider}"),
        "Zelle, wire or check, recorded by staff",
        provider_sentence("Paid outside {provider}"),
    ),
)
#: The Summary card's empty value.
NO_VALUE = "\u2014"
#: The Summary card's last line (PLAN-015b §9.1: the second for a start after today).
SUMMARY_AFTER = "After saving you can send the move-in invoice by e-mail."
ACTIVATED_ON_START = "Starts {day}, activated on that day."
CHANNEL_REFUSED = provider_sentence(
    "Choose Autopay (bank debit), E-mail invoice or Paid outside {provider}."
)
#: The New lease page's operator select before a choice (Fable's 14b-1 visual review).
OPERATOR_EMPTY_LABEL = "Choose an operator\u2026"
#: The Terms card's label of the refundable fee type's price (decision 2).
DEPOSIT_LABEL = "Deposit"


def _channel_cards(
    bound: forms.BoundField, cards: Sequence[tuple[str, Any, Any, Any]]
) -> list[dict[str, Any]]:
    """The channel cards of a lease form (the New and Edit lease pages): per card its value,
    title, muted line, short name (the summaries' words), radio id and whether it is chosen."""
    chosen = str(bound.value() or "")
    return [
        {
            "value": value,
            "title": title,
            "note": note,
            "short": short,
            "id": f"id_payment_channel_{index}",
            "checked": value == chosen,
        }
        for index, (value, title, note, short) in enumerate(cards)
    ]


class LeaseCreateForm(BillingStepMixin, LeaseForm):
    """Adds the one-time key rendered with the form, so a resubmission saves nothing,
    and the setup checklist (PLAN-010b D5; PLAN-012d D9): per fee type of
    :func:`~ledger.services.one_off_charges.checklist_types` a cost, a price, a vendor
    and a due date. A row is charged when its price has a value: the price input starts
    empty with the catalogue's default price as its placeholder, never as a value (no
    tick box; a posted tick is ignored). An empty due date is the lease's start date.

    PLAN-014 C4: the Billing details (:class:`BillingStepMixin`), shown once an operator
    is chosen; the operator select's options carry the contact details "Copy from
    operator" fills in the browser.

    PLAN-014b item 1 (Fable's decisions 1 to 4): no Status (the active lease's rules apply;
    the lease is saved ``active``, or ``prospective`` when it starts after ``today``,
    PLAN-015b §9.1, and the daily job activates it on its start date); the Debit day a
    select of the 1st to the 28th (the 1st by default); the channel one of the three cards
    (:data:`CHANNEL_CARDS`; Zelle, Wire and Unknown are refused here, the Edit lease form
    keeps them); the deposit is the price field of the first refundable fee type
    (:attr:`deposit_type`); "+ Add item" offers one row per active fee type not in the
    checklist (:attr:`extra_types`), charged as a checklist row is, only with a price.
    ``stores``: the operator's other current stores per operator id, for the options'
    ``data-stores``."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)
    fixed_status = CollectionStatus.ACTIVE
    enter_label = NEW_LEASE_ENTER_LABEL
    short_same_label = True

    def __init__(
        self,
        *args: Any,
        accounts: Sequence[SharedAccount] = (),
        enter_refusal: str = "",
        stores: dict[uuid.UUID, list[str]] | None = None,
        today: date | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.today = today or timezone.localdate()
        self.instance.collection_status = CollectionStatus.ACTIVE
        self.fields["debit_day"] = _QuietTypedChoiceField(
            label="Debit day", choices=DEBIT_DAY_CHOICES, coerce=int, initial=1
        )
        self.fields["payment_channel"] = _QuietChoiceField(
            label="Payment channel",
            choices=[(value, title) for value, title, _, _ in CHANNEL_CARDS],
            initial=PaymentChannel.PROVIDER_ACH,
            widget=forms.RadioSelect,
            error_messages={"invalid_choice": CHANNEL_REFUSED},
        )
        prorate = self.fields["prorate_first_month"]
        prorate.label = "Prorate the first month"
        prorate.widget.attrs.update({"class": "switch", "role": "switch"})
        operator = self.fields["operator"]
        operator.widget = OperatorContactSelect(attrs=operator.widget.attrs, stores=stores)
        if isinstance(operator, forms.ModelChoiceField):
            operator.empty_label = OPERATOR_EMPTY_LABEL
        if isinstance(operator, forms.ChoiceField):
            operator.widget.choices = operator.choices
        self.add_billing_step(accounts, enter_refusal=enter_refusal, shown_when="id_operator")
        checklist = one_off_charges.checklist_types()
        listed = {fee_type.pk for fee_type in checklist}
        self.extra_types = [
            fee_type for fee_type in one_off_charges.offered_types() if fee_type.pk not in listed
        ]
        self.fee_types = [*checklist, *self.extra_types]
        self.deposit_type = next(
            (fee_type for fee_type in self.fee_types if fee_type.refundable), None
        )
        for fee_type in self.fee_types:
            name = _setup_name(fee_type)
            self.fields[f"{name}_cost"] = _money_field(
                "Cost", minimum=Decimal("0.00"), initial=fee_type.default_cost
            )
            self.fields[f"{name}_price"] = _setup_price_field(fee_type)
            self.fields[f"{name}_vendor"] = _vendor_field()
            self.fields[f"{name}_due_date"] = forms.DateField(
                label="Due date", required=False, widget=forms.DateInput(attrs={"type": "date"})
            )
        if self.deposit_type is not None:
            self.fields[f"{_setup_name(self.deposit_type)}_price"].label = DEPOSIT_LABEL
        # PLAN-015d §3 (3): when the deposit is collected.
        self.fields["deposit_when"] = _QuietChoiceField(
            label="When",
            choices=deposits.DEPOSIT_WHEN_CHOICES,
            initial=deposits.DepositWhen.WITH_INVOICE,
            required=False,
        )
        on_date = {SHOWN_WHEN: "id_deposit_when=on_date"}
        plan = {SHOWN_WHEN: "id_deposit_when=installments"}
        self.fields["deposit_on"] = forms.DateField(
            label="Due on", required=False, widget=forms.DateInput({"type": "date", **on_date})
        )
        self.fields["deposit_count"] = forms.IntegerField(
            label="Installments",
            min_value=INSTALLMENTS_MIN,
            max_value=INSTALLMENTS_MAX,
            required=False,
            widget=forms.NumberInput(
                {"min": INSTALLMENTS_MIN, "max": INSTALLMENTS_MAX, "step": 1, **plan}
            ),
        )
        self.fields["deposit_first_due"] = forms.DateField(
            label="First due", required=False, widget=forms.DateInput({"type": "date", **plan})
        )
        self.fields["deposit_cadence"] = _QuietChoiceField(
            label="Every",
            choices=deposits.DEPOSIT_CADENCES,
            initial=InstallmentCadence.MONTHLY,
            required=False,
            widget=forms.Select(plan),
        )

    def channel_cards(self) -> list[dict[str, Any]]:
        """The three channel cards, each a radio with its title and muted line."""
        return _channel_cards(self["payment_channel"], CHANNEL_CARDS)

    def _value(self, name: str) -> Any:
        """The field's value as the form holds it, cleaned; ``None`` when it does not clean."""
        bound = self[name]
        try:
            return bound.field.clean(bound.value())
        except forms.ValidationError:
            return None

    def summary(self) -> dict[str, str]:
        """The Summary card (decision 5) from what the form holds: the operator, the start
        and the last line (``after``: "Starts <date>, activated on that day." for a start
        after ``today``, PLAN-015b §9.1, else ``default_after``), the rent, who collects, the
        first month (prorated when the toggle is on and the lease starts after the 1st, as
        :func:`ledger.services.rent_run.first_month`), the move-in charges (every priced row,
        the deposit included) and the first invoice's total. The page's script keeps it
        live; this is the page without it."""
        operator = self._value("operator")
        start: date | None = self._value("started_on")
        rent: Decimal | None = self._value("rent")
        day = self._value("debit_day")
        second = self._value("second_debit_day")
        chosen = str(self["payment_channel"].value() or "")
        short = next((card[3] for card in CHANNEL_CARDS if card[0] == chosen), "")
        days = " and ".join(ordinal(int(value)) for value in (day, second) if value)
        first: Decimal | None = rent
        prorated = False
        if (
            rent is not None
            and start is not None
            and start.day > 1
            and self._value("prorate_first_month")
        ):
            month = start.replace(day=1)
            first = proration.prorated(rent, month, start.day, proration.days_in(month)).amount
            prorated = True
        moving_in = self.setup_total()
        if self._deposit_off_invoice() and self.deposit_type is not None:
            # PLAN-015d §3 (3): a deposit due on a date or in installments is its own charge.
            deposit = self._value(f"{_setup_name(self.deposit_type)}_price")
            moving_in -= deposit if isinstance(deposit, Decimal) else ZERO
        free = ZERO
        if first is not None:
            free = free_rent.first_month_concession(
                start,
                int(self._value("free_rent_days") or 0),
                str(self["free_rent_from"].value() or FreeRentFrom.MOVE_IN),
                first,
                prorate=bool(self._value("prorate_first_month")),
            )
        return {
            "operator": operator.display_name if isinstance(operator, Operator) else NO_VALUE,
            "starts": day_text(start) if start is not None else NO_VALUE,
            "after": (
                ACTIVATED_ON_START.format(day=day_text(start))
                if start is not None and start > self.today
                else SUMMARY_AFTER
            ),
            "default_after": SUMMARY_AFTER,
            "rent": format_amount(rent) if rent is not None else NO_VALUE,
            "collected_by": " \u00b7 ".join(part for part in (str(short), days) if part)
            or NO_VALUE,
            "first_month_label": "First month (prorated)" if prorated else "First month",
            "first_month": format_amount(first) if first is not None else NO_VALUE,
            "move_in": format_amount(moving_in),
            # PLAN-015d: the first month's free rent, "" without any.
            "free_rent": format_amount(-free) if free > 0 else "",
            "total": format_amount(first - free + moving_in) if first is not None else NO_VALUE,
        }

    def deposit_field(self) -> forms.BoundField | None:
        """The deposit's price field (the Terms card's "Deposit"), or ``None``."""
        if self.deposit_type is None:
            return None
        return self[f"{_setup_name(self.deposit_type)}_price"]

    def setup_rows(self) -> list[dict[str, Any]]:
        """The checklist, one row of bound fields per checklist fee type, then the rows "+ Add
        item" reveals (``extra``; shown when one holds a value or an error)."""
        extras = {fee_type.pk for fee_type in self.extra_types}
        rows = []
        for fee_type in self.fee_types:
            parts = {
                part: self[f"{_setup_name(fee_type)}_{part}"]
                for part in ("cost", "price", "vendor", "due_date")
            }
            filled = any(
                str(bound.value() or "").strip() or bound.errors
                for bound in (parts["price"], parts["vendor"], parts["due_date"])
            )
            rows.append(
                {
                    "fee_type": fee_type,
                    "extra": fee_type.pk in extras and not filled,
                    "deposit": fee_type == self.deposit_type,
                    "usual": (
                        format_amount(Decimal(fee_type.default_price)).removeprefix("$")
                        if fee_type.default_price is not None
                        else ""
                    ),
                    **parts,
                }
            )
        return rows

    def setup_total(self) -> Decimal:
        """The sum of the prices typed so far (the move-in charges, the deposit included)."""
        return _setup_total(self, self.fee_types)

    def deposit_timing(self) -> deposits.DepositTiming:
        """The deposit's timing of a valid form (PLAN-015d §3 (3))."""
        data = self.cleaned_data
        return deposits.DepositTiming(
            when=data.get("deposit_when") or deposits.DepositWhen.WITH_INVOICE,
            due_on=data.get("deposit_on"),
            count=int(data.get("deposit_count") or 0),
            first_due=data.get("deposit_first_due"),
            cadence=data.get("deposit_cadence") or InstallmentCadence.MONTHLY,
        )

    def _deposit_off_invoice(self) -> bool:
        return str(self["deposit_when"].value() or "") in (
            deposits.DepositWhen.ON_DATE,
            deposits.DepositWhen.INSTALLMENTS,
        )

    def _clean_deposit(self, data: dict[str, Any], started: date | None) -> None:
        """The deposit's timing (PLAN-015d §3 (3)): a date not in the past for "Due on a
        date"; 2 to 12 installments from a first date, not before the start date nor today,
        for "In installments"; either needs the deposit's amount."""
        when = data.get("deposit_when") or deposits.DepositWhen.WITH_INVOICE
        data["deposit_when"] = when
        if when == deposits.DepositWhen.WITH_INVOICE:
            return
        price = (
            data.get(f"{_setup_name(self.deposit_type)}_price")
            if self.deposit_type is not None
            else None
        )
        if price is None:
            self.add_error("deposit_when", deposits.DEPOSIT_REQUIRED)
            return
        if when == deposits.DepositWhen.ON_DATE:
            due = data.get("deposit_on")
            if due is None:
                self.add_error("deposit_on", "Enter the date the deposit is due.")
            elif due < self.today:
                self.add_error("deposit_on", deposits.DUE_BEFORE_TODAY)
            return
        if data.get("deposit_count") is None:
            self.add_error("deposit_count", installments.COUNT_RANGE)
        first = data.get("deposit_first_due")
        if first is None:
            self.add_error("deposit_first_due", installments.FIRST_REQUIRED)
        elif started is not None and first < started:
            self.add_error("deposit_first_due", deposits.FIRST_BEFORE_START)
        elif first < self.today:
            self.add_error("deposit_first_due", installments.FIRST_IN_PAST)
        if not data.get("deposit_cadence"):
            data["deposit_cadence"] = InstallmentCadence.MONTHLY

    def clean(self) -> dict[str, Any]:
        self.cleaned_data["collection_status"] = CollectionStatus.ACTIVE
        data = super().clean()
        started = data.get("started_on")
        self._clean_deposit(data, started)
        if started is not None and started > self.today:
            # PLAN-015b §9.1: checked by the active lease's rules, saved prospective.
            data["collection_status"] = CollectionStatus.PROSPECTIVE
        for fee_type in self.fee_types:
            name = _setup_name(fee_type)
            if data.get(f"{name}_price") is None:
                continue
            if data.get(f"{name}_due_date") is None and started is None:
                self.add_error(f"{name}_due_date", SETUP_DUE_REQUIRED)
        return data

    def setup_items(self) -> list[ChargeInput]:
        """The rows of a valid form with a price, in catalogue order."""
        data = self.cleaned_data
        started: date | None = data.get("started_on")
        items = []
        for fee_type in self.fee_types:
            name = _setup_name(fee_type)
            price = data.get(f"{name}_price")
            if price is None:
                continue
            due = data.get(f"{name}_due_date") or started
            if fee_type == self.deposit_type:
                # PLAN-015d §3 (3): a deposit off the move-in invoice is due on its own date.
                due = self.deposit_timing().due_date or due
            if due is None:  # refused by clean()
                continue
            items.append(
                ChargeInput(
                    fee_type=fee_type,
                    price=price,
                    cost=data.get(f"{name}_cost"),
                    vendor=data.get(f"{name}_vendor") or "",
                    due_date=due,
                )
            )
        return items


# --- Edit lease (PLAN-015a) --------------------------------------------------------------

#: A current value the Edit lease page no longer offers is one more choice, so an unchanged
#: lease stays valid: "30th (current)", "Wire (current)".
CURRENT_SUFFIX = " (current)"
#: The muted line of the card of a channel outside the three cards.
CURRENT_CHANNEL_NOTE = "This lease's channel today. Not offered for a new lease."
#: The Changes card's words where they differ from the field's label.
CHANGE_LABELS = {
    "rent": "Monthly rent",
    "rent_effective_from": "From",
    "free_rent_days": FREE_RENT_CHANGE_LABEL,
    "free_rent_from": FREE_RENT_FROM_LABEL,
}
#: A lease with no rent row and no rent typed (a lease the service always gives one).
RENT_REQUIRED = "Enter the monthly rent: this lease has none yet."


def _debit_day_choices(current: int | None, *, blank: bool) -> list[tuple[Any, str]]:
    """The 1st to the 28th, after "—" when ``blank``; a current day above the 28th is one
    more choice, labelled as the current one."""
    choices: list[tuple[Any, str]] = [("", NO_VALUE)] if blank else []
    choices += DEBIT_DAY_CHOICES
    if current is not None and current > DEBIT_DAY_CHOICES[-1][0]:
        choices.append((current, f"{ordinal(current)}{CURRENT_SUFFIX}"))
    return choices


class LeaseEditForm(_LeaseFormBase):
    """The Edit lease page (PLAN-015a §3): the Collection card (the debit day or days, the 1st
    to the 28th, and the channel, one of :data:`CHANNEL_CARDS`), "Prorate the first month"
    until the first month is billed (``first_month_billed``: the field is removed, so a posted
    value is ignored) and "Change rent" (``rent``: empty means unchanged;
    ``rent_effective_from``: empty means the next rent due date, ``rent_default``; a valid
    form's ``rent`` and ``rent_effective_from`` are what ``update_lease`` takes). A current
    day above the 28th or a current channel outside the cards is one more choice, so an
    unchanged lease stays valid; a new one is refused. The store, the operator, the start date
    and the status are not fields here (the transfer and lease-end wizards change them): a
    posted value is ignored, never saved. ``open_rent``: the latest rent row's amount, which a
    rent change replaces.

    Each editable control carries ``data-initial`` (the lease's value) and ``data-label`` (the
    Changes card's words, ``ledger/edit_lease.js``); the channel cards carry them in their
    markup (:meth:`channel_cards`)."""

    rent = forms.DecimalField(
        label="New monthly rent",
        required=False,
        min_value=Decimal("0.01"),
        max_value=RENT_MAX,
        max_digits=12,
        decimal_places=2,
    )
    rent_effective_from = forms.DateField(
        label="Takes effect on",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The current rent ends the day before.",
    )

    class Meta:
        model = Lease
        formfield_callback = _quiet_formfield
        fields = (
            "debit_day",
            "second_debit_day",
            "payment_channel",
            "prorate_first_month",
            "free_rent_days",
            "free_rent_from",
        )
        labels = {"prorate_first_month": "Prorate the first month"}

    def __init__(
        self,
        *args: Any,
        first_month_billed: bool = False,
        open_rent: Decimal | None = None,
        rent_default: date | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        lease = self.instance
        self.open_rent = open_rent
        self.rent_default = rent_default
        # An active lease needs a debit day: "—" only where it may have none.
        blank = lease.debit_day is None or lease.collection_status != CollectionStatus.ACTIVE
        self.fields["debit_day"] = _QuietTypedChoiceField(
            label="Debit day",
            choices=_debit_day_choices(lease.debit_day, blank=blank),
            coerce=int,
            empty_value=None,
            required=False,
        )
        self.fields["second_debit_day"] = _QuietTypedChoiceField(
            label="Second debit day",
            choices=_debit_day_choices(lease.second_debit_day, blank=True),
            coerce=int,
            empty_value=None,
            required=False,
            # PLAN-012c D8: shown while "Twice a month" is ticked; hidden, it is not posted.
            widget=forms.Select(attrs={SHOWN_WHEN: TWICE_MONTHLY_ID}),
        )
        self.cards = list(CHANNEL_CARDS)
        current = lease.payment_channel
        if current and current not in {card[0] for card in CHANNEL_CARDS}:
            name = PaymentChannel(current).label if current in PaymentChannel.values else current
            self.cards.append((current, f"{name}{CURRENT_SUFFIX}", CURRENT_CHANNEL_NOTE, name))
        self.fields["payment_channel"] = _QuietChoiceField(
            label="Payment channel",
            choices=[(value, title) for value, title, _, _ in self.cards],
            widget=forms.RadioSelect,
            error_messages={"invalid_choice": CHANNEL_REFUSED},
        )
        #: Whether the page shows "Prorate the first month" (not once the month is billed).
        self.offers_prorate = not first_month_billed
        if first_month_billed:
            del self.fields["prorate_first_month"]
        else:
            prorate = self.fields["prorate_first_month"]
            prorate.widget.attrs.update({"class": "switch", "role": "switch"})
        if rent_default is not None:
            self.fields["rent_effective_from"].initial = rent_default
        _free_rent_fields(self)
        self.fields["free_rent_days"].initial = lease.free_rent_days
        self.fields["free_rent_from"].initial = lease.free_rent_from
        self.fields["free_rent_days"].widget.attrs["data-suffix"] = " days"
        initial: dict[str, Any] = {
            "debit_day": lease.debit_day,
            "second_debit_day": lease.second_debit_day,
            "prorate_first_month": "true" if lease.prorate_first_month else "false",
            "rent": "",
            "rent_effective_from": rent_default.isoformat() if rent_default else "",
            "free_rent_days": lease.free_rent_days,
            "free_rent_from": lease.free_rent_from,
        }
        for name, value in initial.items():
            field = self.fields.get(name)
            if field is None:
                continue
            field.widget.attrs["data-initial"] = "" if value is None else str(value)
            field.widget.attrs["data-label"] = CHANGE_LABELS.get(name, str(field.label))
        if open_rent is not None:
            self.fields["rent"].widget.attrs["data-current"] = f"{open_rent:.2f}"
            self.fields["rent"].widget.attrs["data-current-text"] = format_amount(open_rent)

    def twice_monthly(self) -> bool:
        """Whether the page's "Twice a month" tick starts ticked (PLAN-012c D8)."""
        return _twice_monthly(self["second_debit_day"])

    def channel_cards(self) -> list[dict[str, Any]]:
        """The channel cards (the three, and the current one outside them), each a radio
        with its title, muted line and short name; ``initial`` the lease's channel."""
        initial = self.instance.payment_channel or ""
        return [
            {**card, "initial": initial}
            for card in _channel_cards(self["payment_channel"], self.cards)
        ]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        _clean_free_rent(data)
        active = self.instance.collection_status == CollectionStatus.ACTIVE
        _check_debits(self, data, active=active)
        if "rent" in self.errors:
            return data
        if data.get("rent") is None:
            if self.open_rent is None:
                self.add_error("rent", RENT_REQUIRED)
            # An empty rent is no change: the open row's amount, no date.
            data["rent"] = self.open_rent
            data["rent_effective_from"] = None
        elif data.get("rent_effective_from") is None:
            data["rent_effective_from"] = self.rent_default
        return data


# --- Setup charges (PLAN-010b D5, D6) ------------------------------------------------------

#: The name prefix of every checklist field of the lease create form.
SETUP = "setup_"
SETUP_DUE_REQUIRED = "Enter a due date, or a start date for the lease."
COST_HELP = "Empty means the cost is unknown."
#: The class of an amount input summed by ``ledger/table_total.js`` (the setup checklist's
#: prices, the lease end's deductions; PLAN-012d D9, D10).
TOTAL_INPUT_CLASS = "js-total"


def _setup_name(fee_type: FeeType) -> str:
    return f"{SETUP}{fee_type.code}"


def _setup_price_field(fee_type: FeeType) -> forms.DecimalField:
    """A checklist row's price (PLAN-012d D9): empty, the catalogue's default price shown
    as the placeholder only, so an untouched row charges nothing."""
    field = _money_field("Price", minimum=Decimal("0.01"))
    field.widget.attrs["class"] = TOTAL_INPUT_CLASS
    if fee_type.default_price is not None:
        field.widget.attrs["placeholder"] = f"{Decimal(fee_type.default_price):.2f}"
    return field


def _setup_total(form: forms.BaseForm, fee_types: Iterable[FeeType]) -> Decimal:
    """The sum of the checklist prices the form holds (posted, or initial on a GET); a
    price that is not a valid amount counts as nothing."""
    total = Decimal("0.00")
    for fee_type in fee_types:
        bound = form[f"{_setup_name(fee_type)}_price"]
        try:
            value = bound.field.clean(bound.value())
        except forms.ValidationError:
            continue
        if isinstance(value, Decimal):
            total += value
    return total


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
LANDLORD_RENT_HELP = (
    "Leave empty when the cost is not known yet; 0.00 means the store costs nothing."
)
CLOSED_BEFORE_OPEN = "The closed date cannot be before the open date."


BRAND_REFUSED = "Choose a brand from the list."


class StoreForm(_StoreFormBase):
    """Create and edit a store. The open date is required here (ADR-0005) although the
    model keeps it nullable for the import; ``needs_open_date`` follows the model.

    PLAN-012f-b D10, D11, PLAN-014 B1: Brand is optional, a select of the active brands (the
    store's own brand stays offered when it was made inactive since, so an edit keeps it);
    State is a select of the 50 states and DC by name, the code stored, empty allowed."""

    store_number = forms.IntegerField(min_value=1, max_value=2147483647, required=False)
    # The refusals never repeat the posted value (it may be anything, a card number too).
    brand = _QuietModelChoiceField(
        queryset=Brand.objects.none(),
        required=False,
        empty_label="\u2014",
        label="Brand",
        error_messages={"invalid_choice": BRAND_REFUSED},
    )
    state = _QuietChoiceField(
        choices=STATE_CHOICES,
        required=False,
        label="State",
        error_messages={"invalid_choice": STATE_REFUSED},
    )
    open_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={"required": OPEN_DATE_REQUIRED},
    )

    class Meta:
        model = Store
        # The status select never repeats a refused value (B3-1).
        formfield_callback = _quiet_formfield
        fields = (
            "store_number",
            "external_label",
            "brand",
            "address_line1",
            "city",
            "state",
            "postal_code",
            "open_date",
            "closed_at",
            "status",
        )
        widgets = {"closed_at": forms.DateInput(attrs={"type": "date"})}
        labels = {
            "external_label": "Name",
            "address_line1": "Street",
            "postal_code": "ZIP",
            "closed_at": "Closed date",
        }

    #: The fields of the store's first cost row, shown under "Store cost" (create only).
    COST_FIELDS: tuple[str, ...] = ()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        offered = Q(active=True)
        if self.instance.brand_id is not None:
            offered |= Q(pk=self.instance.brand_id)
        field = self.fields["brand"]
        if isinstance(field, forms.ModelChoiceField):
            field.queryset = Brand.objects.filter(offered)

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        opened = data.get("open_date")
        closed = data.get("closed_at")
        if opened is not None and closed is not None and closed < opened:
            self.add_error("closed_at", CLOSED_BEFORE_OPEN)
        return data

    def store_fields(self) -> list[forms.BoundField]:
        return [field for field in self.visible_fields() if field.name not in self.COST_FIELDS]

    def cost_fields(self) -> list[forms.BoundField]:
        return [field for field in self.visible_fields() if field.name in self.COST_FIELDS]


class StoreCreateForm(StoreForm):
    """Adds the one-time key rendered with the form, so a resubmission saves nothing, and
    the store's first cost row (PLAN-012e-2 D9): the landlord rent is optional (PLAN-012f-b
    D12): empty is "cost not entered", no cost row, and ``0.00`` is a known "no cost"; the
    Edit form has none of these, "Change" sets them."""

    COST_FIELDS = (
        "landlord_rent",
        "landlord_lease_end",
        "other_monthly_cost",
        "other_cost_description",
    )

    creation_key = forms.UUIDField(widget=forms.HiddenInput)
    landlord_rent = _money_field("Landlord rent (monthly)", minimum=Decimal("0.00"))
    landlord_lease_end = forms.DateField(
        label="Landlord lease end",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    other_monthly_cost = _money_field("Other monthly cost", minimum=Decimal("0.00"))
    other_cost_description = forms.CharField(
        label="Other cost description",
        required=False,
        max_length=NAME_MAX_LENGTH,
        validators=[validate_no_payment_data],
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["landlord_rent"].help_text = LANDLORD_RENT_HELP
        self.fields["other_monthly_cost"].help_text = "Empty means no other monthly cost."


# --- Manual payments (PLAN-007 D8) -------------------------------------------------------

#: Longest note accepted on a manual payment.
PAYMENT_NOTE_MAX_LENGTH = 2000
PAYMENT_REFERENCE_MAX_LENGTH = 100
MANUAL_METHOD_CHOICES = tuple(
    (PaymentMethod(method).value, PaymentMethod(method).label) for method in rent_run.MANUAL_METHODS
)
#: The reference's help on every manual payment form (PLAN-012e-3 D3).
REFERENCE_HELP = (
    "A Zelle confirmation, a wire reference or a check number; never an account number."
)
#: The reference's help on Add payment (PLAN-012f-c D10): what it is for is in the help
#: topic "Payments".
ADD_PAYMENT_REFERENCE_HELP = "Never an account number."
#: The reference's label while Check is the method chosen (PLAN-012e-3 D3).
REFERENCE_LABEL_CHECK = "Check number"


def _label_check_number(form: forms.Form) -> None:
    """While Check is chosen in ``method``, the ``reference`` label reads "Check number"
    (``data-label-when`` on the control, applied by ``ledger/shown_when.js``); without the
    script it stays "Reference" and the help names the check number (PLAN-012e-3 D3)."""
    attrs = form.fields["reference"].widget.attrs
    attrs["data-label-when"] = f"{form['method'].auto_id}={PaymentMethod.CHECK.value}"
    attrs["data-label"] = REFERENCE_LABEL_CHECK


class ManualPaymentForm(forms.Form):
    """A payment received outside the payment provider. The service re-checks every rule
    (balance, date, note) under a row lock; the form checks shape only."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)
    method = _QuietChoiceField(choices=MANUAL_METHOD_CHOICES)
    amount = forms.DecimalField(
        min_value=Decimal("0.01"), max_value=RENT_MAX, max_digits=12, decimal_places=2
    )
    paid_on = forms.DateField(label="Received on", widget=forms.DateInput(attrs={"type": "date"}))
    reference = forms.CharField(
        required=False,
        max_length=PAYMENT_REFERENCE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text=provider_sentence(
            f"{REFERENCE_HELP} Optional: letters, digits and hyphens of it (up to 25) are sent "
            "to {provider} when the invoice is marked paid."
        ),
    )
    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: what was received, and any reference staff need.",
        error_messages={"required": "A note is required: say what was received."},
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _label_check_number(self)

    def clean_note(self) -> str:
        # Invisible format characters and Unicode whitespace alone are not a note
        # (PLAN-010c D12).
        note = visible_text(self.cleaned_data["note"])
        if not note:
            raise forms.ValidationError(self.fields["note"].error_messages["required"])
        return note


#: Add payment's two cards (PLAN-012f-b D14): the radio ``how``.
HOW_DEBIT = "debit"
HOW_RECORD = "record"
HOW_CHOICES: tuple[tuple[str, str], ...] = (
    (HOW_DEBIT, provider_sentence("Charge the operator's bank account (ACH via {provider})")),
    (HOW_RECORD, provider_sentence("Record a payment received outside {provider}")),
)
#: Billing debits from tomorrow on: today goes through Charge now, an admin's (PLAN-012f-b
#: C2-1). The line under Debit on, and the refusal of today's date.
DEBIT_TODAY_NEEDS_ADMIN = "A debit today needs an admin."
NOTE_OPTIONAL_LABEL = "Note (optional)"
NOTE_REQUIRED_LABEL = "Note (required)"
#: The record card's fields, sent and checked only while that card is chosen.
RECORD_FIELDS = ("method", "amount", "paid_on", "reference")
#: The debit card's fields, sent and checked only while that card is chosen.
DEBIT_FIELDS = ("debit_amount", "debit_on", "confirm_large_debit")


class AddPaymentForm(ManualPaymentForm):
    """The Add payment page (PLAN-012f-b D14): ``how`` picks the bank-debit card
    (``debit_amount``, ``debit_on``, ``confirm_large_debit``) or the record card (today's
    :class:`ManualPaymentForm` fields). ``debit_reason`` is why the debit card is greyed
    (``""``: offered), ``debit_reason_more`` its second line (PLAN-012f-c D7) and
    ``debit_reason_url`` the operator page linked after the first line (§11 E2);
    ``balance`` what a debit takes; ``admin`` whether today may be the debit date (else
    Debit on starts tomorrow, C2-1). A post without ``how`` is the record card (the shape
    of the page before). Each card's fields carry ``data-shown-when``
    (``ledger/shown_when.js``) and count only for their card; the note is required only
    for ``record``. The services check every rule again."""

    how = _QuietChoiceField(required=False, choices=HOW_CHOICES, label="How is this being paid?")
    debit_amount = forms.DecimalField(
        required=False,
        min_value=Decimal("0.01"),
        max_value=RENT_MAX,
        max_digits=12,
        decimal_places=2,
        label="Amount",
        help_text="Up to the balance due.",
        widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
    )
    debit_on = forms.DateField(
        required=False, label="Debit on", widget=forms.DateInput(attrs={"type": "date"})
    )
    confirm_large_debit = forms.BooleanField(
        required=False, widget=_ExactCheckboxInput, label="I confirm this large debit"
    )

    def __init__(
        self,
        data: Any = None,
        *args: Any,
        debit_reason: str,
        balance: Decimal,
        today: date | None = None,
        admin: bool = True,
        debit_reason_more: str = "",
        debit_reason_url: str = "",
        **kwargs: Any,
    ) -> None:
        if data is not None and not data.get("how"):
            data = data.copy()
            data["how"] = HOW_RECORD
        super().__init__(data, *args, **kwargs)
        self.debit_reason = debit_reason
        self.debit_reason_more = debit_reason_more if debit_reason else ""
        self.debit_reason_url = debit_reason_url if debit_reason else ""
        self.balance = balance
        self.today = today or timezone.localdate()
        self.admin = admin
        earliest = self.today if admin else self.today + timedelta(days=1)
        for name in ("method", "amount", "paid_on", "note"):
            self.fields[name].required = False
        self.fields["reference"].help_text = ADD_PAYMENT_REFERENCE_HELP
        self.fields["how"].initial = HOW_RECORD if debit_reason else HOW_DEBIT
        self.fields["debit_amount"].initial = balance
        self.fields["debit_amount"].widget.attrs["max"] = str(balance)
        self.fields["debit_on"].initial = earliest
        self.fields["debit_on"].widget.attrs["min"] = earliest.isoformat()
        if not admin:
            self.fields["debit_on"].help_text = DEBIT_TODAY_NEEDS_ADMIN
        for name in RECORD_FIELDS:
            self.fields[name].widget.attrs["data-shown-when"] = f"how-{HOW_RECORD}"
        for name in DEBIT_FIELDS:
            self.fields[name].widget.attrs["data-shown-when"] = f"how-{HOW_DEBIT}"
        # The note's label follows the card: optional for a debit, required to record.
        chosen = self.chosen()
        note = self.fields["note"]
        note.help_text = ""
        other = HOW_DEBIT if chosen == HOW_RECORD else HOW_RECORD
        note.label = NOTE_REQUIRED_LABEL if chosen == HOW_RECORD else NOTE_OPTIONAL_LABEL
        note.widget.attrs["data-label-when"] = f"how-{other}"
        note.widget.attrs["data-label"] = (
            NOTE_OPTIONAL_LABEL if chosen == HOW_RECORD else NOTE_REQUIRED_LABEL
        )

    def chosen(self) -> str:
        """The card chosen: the record card while the debit card is greyed, else the bound
        or initial choice."""
        if self.debit_reason:
            return HOW_RECORD
        return HOW_RECORD if self["how"].value() == HOW_RECORD else HOW_DEBIT

    def options(self) -> list[dict[str, Any]]:
        """The two radio cards as the page renders them (ids ``how-<value>``)."""
        chosen = self.chosen()
        return [
            {
                "value": value,
                "label": label,
                "id": f"how-{value}",
                "checked": value == chosen,
                "reason": self.debit_reason if value == HOW_DEBIT else "",
                "reason_more": self.debit_reason_more if value == HOW_DEBIT else "",
                "reason_url": self.debit_reason_url if value == HOW_DEBIT else "",
            }
            for value, label in HOW_CHOICES
        ]

    def clean_note(self) -> str:
        return visible_text(self.cleaned_data.get("note") or "")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        how = data.get("how") or HOW_RECORD
        data["how"] = how
        ignored = DEBIT_FIELDS if how == HOW_RECORD else RECORD_FIELDS
        for name in ignored:
            # A page without the script sends both cards; the other card's values count
            # for nothing, but a card number is never let through (Rule 1).
            if name in self.errors and not (name == "reference" and self._holds_payment_data(name)):
                del self.errors[name]
                data[name] = None
        if how == HOW_RECORD:
            self._clean_record(data)
        else:
            self._clean_debit(data)
        return data

    def _holds_payment_data(self, name: str) -> bool:
        try:
            validate_no_payment_data(str(self.data.get(name) or ""))
        except forms.ValidationError:
            return True
        return False

    def _clean_record(self, data: dict[str, Any]) -> None:
        if not data.get("method") and "method" not in self.errors:
            self.add_error("method", rent_run.PAY_METHOD)
        if data.get("amount") is None and "amount" not in self.errors:
            self.add_error("amount", rent_run.PAY_NOT_POSITIVE)
        if data.get("paid_on") is None and "paid_on" not in self.errors:
            self.add_error("paid_on", "Enter the date the payment was received.")
        if not data.get("note") and "note" not in self.errors:
            self.add_error("note", self.fields["note"].error_messages["required"])

    def _clean_debit(self, data: dict[str, Any]) -> None:
        if self.debit_reason:
            self.add_error("how", self.debit_reason)
            return
        amount = data.get("debit_amount")
        if amount is None and "debit_amount" not in self.errors:
            self.add_error("debit_amount", one_off_charges.AMOUNT_NOT_POSITIVE)
        elif amount is not None and amount > self.balance:
            self.add_error("debit_amount", one_off_charges.DEBIT_OVER_BALANCE)
        elif amount is not None and amount != self.balance:
            self.add_error(
                "debit_amount",
                one_off_charges.DEBIT_WHOLE_BALANCE.format(amount=format_amount(self.balance)),
            )
        on = data.get("debit_on")
        if on is None and "debit_on" not in self.errors:
            self.add_error("debit_on", one_off_charges.DEBIT_DATE_REQUIRED)
        elif on is not None and on < self.today:
            self.add_error("debit_on", one_off_charges.DEBIT_DATE_PAST)
        elif on == self.today and not self.admin:
            self.add_error("debit_on", DEBIT_TODAY_NEEDS_ADMIN)


#: The prefixes of the store payment form's per-charge inputs (PLAN-012d D6).
ALLOCATION_PREFIX = "alloc_"
CREDIT_PREFIX = "credit_"


class AllocationPaymentForm(forms.Form):
    """The store page's payment form (PLAN-012d D6): one amount received, allocated by hand
    over the open charges ``charges`` (each with an ``alloc_<id>`` input and a
    ``credit_<id>`` input for the credit held), and ``keep_credit`` to keep the rest as a
    credit on the lease. The amount may be empty when only the credit is applied. The
    service (:func:`ledger.services.payments_entry.record`) checks every rule again under
    the lease's row lock; the form checks shape only."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)
    method = _QuietChoiceField(choices=MANUAL_METHOD_CHOICES)
    amount = forms.DecimalField(
        required=False,
        max_value=RENT_MAX,
        max_digits=12,
        decimal_places=2,
        label="Amount received",
        help_text="Leave empty to apply the credit held alone.",
    )
    paid_on = forms.DateField(label="Received on", widget=forms.DateInput(attrs={"type": "date"}))
    reference = ManualPaymentForm.base_fields["reference"]
    note = ManualPaymentForm.base_fields["note"]
    keep_credit = forms.BooleanField(
        required=False, widget=_ExactCheckboxInput, label="Keep the rest as a credit on the lease"
    )

    def __init__(self, *args: Any, charges: Iterable[Any] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _label_check_number(self)
        self.charges = list(charges)
        for charge in self.charges:
            for prefix in (ALLOCATION_PREFIX, CREDIT_PREFIX):
                self.fields[f"{prefix}{charge.charge_id}"] = forms.DecimalField(
                    required=False,
                    max_digits=12,
                    decimal_places=2,
                    label=charge.description,
                    widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
                )

    clean_note = ManualPaymentForm.clean_note

    def amounts(self, prefix: str) -> dict[uuid.UUID, Decimal]:
        """The per-charge amounts typed with ``prefix`` (an empty input is zero)."""
        return {
            charge.charge_id: self.cleaned_data.get(f"{prefix}{charge.charge_id}")
            or Decimal("0.00")
            for charge in self.charges
        }


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
#: Blank line rows a draft form shows besides its lines: a new draft opens with one line;
#: "Add line" adds more (PLAN-012d D10).
BLANK_LINE_ROWS = 1
#: The months a Rent line offers around the current one (PLAN-012d D10: ± 12).
RENT_MONTHS_AROUND = 12


def _lease_label(lease: Lease) -> str:
    start = day_text(lease.started_on) if lease.started_on else "no start date"
    operator = lease.operator.display_name if lease.operator is not None else "no operator"
    return f"{lease.store.external_label}: {operator}, from {start}"


class InvoiceDraftForm(forms.Form):
    """The header of an invoice draft (PLAN-012 D6): the lease (one of ``leases``; or
    none, the operator alone, when ``allow_none``), the memo and the optional due date.
    The form checks shape; :mod:`ledger.services.invoice_builder` checks every rule."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput, required=False)
    lease = _QuietChoiceField(required=False, label="Lease")
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


#: The Item box's choice of a Rent line (PLAN-012d D10; the kind select of PLAN-012b-b D4
#: is gone): the first entry of the list.
RENT_ITEM = "Rent"
RENT_LINE_INCOMPLETE = "Choose the lease and the month of the rent."
EXISTING_LINE_UNKNOWN = "Choose the existing open charge from the list."


def rent_months(today: date, around: int = RENT_MONTHS_AROUND) -> list[tuple[str, str]]:
    """The months a Rent line offers: ``around`` before and after the current one, as
    (``YYYY-MM``, ``October 2026``)."""
    first = today.year * 12 + today.month - 1
    months = []
    for number in range(first - around, first + around + 1):
        month = date(number // 12, number % 12 + 1, 1)
        months.append((f"{month:%Y-%m}", invoice_builder.month_label(month)))
    return months


def _month_value(value: date | str | None) -> str:
    if isinstance(value, date):
        return f"{value:%Y-%m}"
    return str(value or "")


class InvoiceLineForm(forms.Form):
    """One line of a draft (PLAN-012b D1, D2; PLAN-012d D10): one Item box on the list
    of :data:`ITEMS_LIST_ID`, which starts with **Rent**, then the draft's lease's
    existing open charges ("Existing open charge: <label> ($balance)"), then the
    catalogue items; any other name is a typed item. An optional description (the
    detail), a quantity (empty: 1), a unit amount and Save as catalogue item. The form
    never sends a fee type: the service resolves the item by its name. A row with neither
    an item, a description nor a unit amount is blank and ignored; a removed row (the
    trash button sets its ``DELETE``) is dropped unchecked.

    **Rent** bills a month of a lease: the lease (one of ``rent_leases``,
    ``default_lease`` preselected) and the month (a select of ``months``, values
    ``YYYY-MM``); an empty unit amount is the month's rent. An **existing open charge**
    names the charge (``existing_charge``, set by the list's script, else found by the
    box's text among ``existing``); an empty unit amount is its balance. The service
    checks every rule."""

    item = forms.CharField(
        required=False,
        label="Item",
        max_length=ITEM_INPUT_MAX_LENGTH,
        validators=[validate_no_payment_data],
        widget=forms.TextInput(
            {
                "list": ITEMS_LIST_ID,
                "placeholder": "Rent, an open charge, a catalogue item or a new name",
                "autocomplete": "off",
            }
        ),
    )
    existing_charge = forms.CharField(required=False, widget=forms.HiddenInput)
    description = forms.CharField(
        required=False,
        max_length=DESCRIPTION_INPUT_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text=f"Optional detail under the item; at most {DESCRIPTION_MAX_LENGTH} characters.",
    )
    quantity = forms.DecimalField(
        required=False,
        max_digits=12,
        decimal_places=4,
        widget=forms.NumberInput({"step": "any", "class": "js-quantity"}),
    )
    unit_amount = forms.DecimalField(
        required=False,
        label="Unit amount",
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput({"step": "0.01", "class": "js-unit"}),
    )
    save_item = forms.BooleanField(required=False, label="Save as catalogue item")
    rent_lease = _QuietChoiceField(required=False, label="Rent: lease")
    rent_month = _QuietChoiceField(required=False, label="Rent: month")

    def __init__(
        self,
        *args: Any,
        rent_leases: Iterable[Lease] = (),
        default_lease: Lease | None = None,
        months: Iterable[tuple[str, str]] = (),
        existing: Iterable[OneOffCharge] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        lease_field = self.fields["rent_lease"]
        if isinstance(lease_field, forms.ChoiceField):
            lease_field.choices = [("", "Choose the lease")] + [
                (str(row.pk), _lease_label(row)) for row in rent_leases
            ]
        if default_lease is not None:
            lease_field.initial = str(default_lease.pk)
        offered = list(months)
        stored = _month_value(self.initial.get("rent_month"))
        if stored and stored not in {value for value, _ in offered}:
            # A draft's month outside the window stays choosable.
            parsed = date.fromisoformat(f"{stored}-01")
            offered = sorted([*offered, (stored, invoice_builder.month_label(parsed))])
        month_field = self.fields["rent_month"]
        if isinstance(month_field, forms.ChoiceField):
            month_field.choices = [("", "Choose the month")] + offered
        self.existing = {
            invoice_builder.catalogue_key(invoice_builder.existing_label(row)): row
            for row in existing
        }

    @property
    def is_rent(self) -> bool:
        return invoice_builder.catalogue_key(self.cleaned_data.get("item") or "") == (
            invoice_builder.catalogue_key(RENT_ITEM)
        )

    @property
    def is_existing(self) -> bool:
        return invoice_builder.catalogue_key(self.cleaned_data.get("item") or "").startswith(
            invoice_builder.catalogue_key(invoice_builder.EXISTING_PREFIX)
        )

    def clean_rent_month(self) -> date | None:
        value = self.cleaned_data.get("rent_month") or ""
        return date.fromisoformat(f"{value}-01") if value else None

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if "item" not in data:
            return data
        if self.is_rent:
            if not (data.get("rent_lease") and data.get("rent_month")):
                raise forms.ValidationError(RENT_LINE_INCOMPLETE)
        elif self.is_existing:
            chosen = self.existing.get(invoice_builder.catalogue_key(data["item"]))
            typed = _uuid_or_none(data.get("existing_charge"))
            if chosen is not None:
                data["existing_charge"] = chosen.pk
            elif typed is not None:
                # The script's choice; the service checks it is this lease's open charge.
                data["existing_charge"] = typed
            else:
                raise forms.ValidationError(EXISTING_LINE_UNKNOWN)
        return data

    def is_blank(self) -> bool:
        data = self.cleaned_data
        if self.is_rent or self.is_existing:
            return False
        return not (data.get("item") or data.get("description") or data.get("unit_amount"))

    def line(self) -> invoice_builder.LineInput:
        data = self.cleaned_data
        if self.is_rent:
            return invoice_builder.LineInput(
                item="",
                description=data.get("description") or "",
                quantity=data.get("quantity"),
                unit_amount=data.get("unit_amount"),
                rent_lease=uuid.UUID(str(data["rent_lease"])),
                rent_month=data["rent_month"],
            )
        if self.is_existing:
            return invoice_builder.LineInput(
                item=data.get("item") or "",
                description=data.get("description") or "",
                quantity=data.get("quantity"),
                unit_amount=data.get("unit_amount"),
                existing_charge=data["existing_charge"],
            )
        return invoice_builder.LineInput(
            item=data.get("item") or "",
            description=data.get("description") or "",
            quantity=data.get("quantity"),
            unit_amount=data.get("unit_amount"),
            save_item=data.get("save_item") is True,
        )


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


InvoiceLineFormSet = forms.formset_factory(
    InvoiceLineForm,
    extra=BLANK_LINE_ROWS,
    can_delete=True,
    max_num=invoice_builder.MAX_LINES,
    validate_max=True,
    absolute_max=invoice_builder.MAX_LINES + BLANK_LINE_ROWS,
)


def _removed(form: forms.Form) -> bool:
    """Whether the row's trash button (or its Remove box) set ``DELETE``."""
    return form["DELETE"].value() is True


def kept_rows(formset: forms.BaseFormSet[InvoiceLineForm]) -> list[tuple[int, InvoiceLineForm]]:
    """The rows the user sees (not removed), each with its visible line number."""
    return [
        (number, form)
        for number, form in enumerate(
            (form for form in formset.forms if not _removed(form)), start=1
        )
    ]


def draft_lines(formset: forms.BaseFormSet[InvoiceLineForm]) -> list[invoice_builder.LineInput]:
    """The lines of a valid formset, in order: blank and removed rows left out."""
    return [form.line() for _, form in _filled(formset)]


def line_numbers(formset: forms.BaseFormSet[InvoiceLineForm]) -> list[int]:
    """The visible line number of each line :func:`draft_lines` returns, in order (the
    service numbers its refusals by its own list; PLAN-012d D10)."""
    return [number for number, _ in _filled(formset)]


def _filled(
    formset: forms.BaseFormSet[InvoiceLineForm],
) -> list[tuple[int, InvoiceLineForm]]:
    return [
        (number, form)
        for number, form in kept_rows(formset)
        if form.has_changed() and form.cleaned_data and not form.is_blank()
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


class PauseCollectionForm(forms.Form):
    """Pause collection on a lease (PLAN-015b §3 B, billing): the reason, nothing else."""

    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: why collection is paused.",
        error_messages={"required": "A reason is required: say why collection is paused."},
    )

    def clean_reason(self) -> str:
        # Invisible format characters and Unicode whitespace alone are not a reason.
        reason = visible_text(self.cleaned_data["reason"])
        if not reason:
            raise forms.ValidationError(self.fields["reason"].error_messages["required"])
        return reason


class ChangeStartDateForm(forms.Form):
    """Change start date (PLAN-015d §3 (1)): the new date, the required reason, the money's
    choice when rent paid moves (credit or refund), the deposit's tick and the extra days'
    confirmation (§9.3)."""

    new_start = forms.DateField(
        label="New start date", widget=forms.DateInput(attrs={"type": "date"})
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        help_text="Required: why the start date changes.",
        error_messages={"required": "A reason is required: say why the start date changes."},
    )
    choice = _QuietChoiceField(
        label="Rent already paid",
        choices=[("credit", "Keep as credit"), ("refund", "Refund")],
        required=False,
        widget=forms.RadioSelect,
    )
    move_deposit = forms.BooleanField(label="Move the deposit schedule too", required=False)
    confirm = forms.BooleanField(label="Bill the extra days", required=False)

    def clean_reason(self) -> str:
        reason = visible_text(self.cleaned_data["reason"])
        if not reason:
            raise forms.ValidationError(self.fields["reason"].error_messages["required"])
        return reason


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
    large-debit box; the service re-checks every rule under the charge's row lock. ``key``
    is the page's one-time key (PLAN-012f-b D14: Add payment's, carried in ``?key=``), so a
    second submit of the same page debits nothing."""

    confirm = forms.BooleanField(required=False, widget=_ExactCheckboxInput)
    key = forms.UUIDField(required=False, widget=forms.HiddenInput)


#: The draft form's "How to collect" tiles and their labels, in order (PLAN-012e-2 D3;
#: PLAN-012f-b D15: Save first, no Send by e-mail tile; the e-mail is Save's prompt).
COLLECT_CHOICES: tuple[tuple[str, str], ...] = (
    (invoice_builder.CollectChoice.DRAFT.value, "Save"),
    (invoice_builder.CollectChoice.CHARGE_NOW.value, "Charge now"),
    (invoice_builder.CollectChoice.SCHEDULE.value, "Schedule for a date"),
    (invoice_builder.CollectChoice.RECORD.value, "Record payment now"),
    (invoice_builder.CollectChoice.AUTOPAY.value, "Set up autopay"),
    (invoice_builder.CollectChoice.SETTLEMENT.value, "Schedule settlement"),
)
#: Why Charge now is greyed for billing (PLAN-012f-b D15).
CHARGE_NOW_NEEDS_ADMIN = "Charge now needs an admin."
#: Set up autopay's fields (PLAN-012f-a D12).
AUTOPAY_FIELDS = (
    "autopay_cadence",
    "autopay_first_on",
    "autopay_end_rule",
    "autopay_end_count",
    "autopay_end_on",
    "autopay_confirm_large_debit",
)
#: Schedule settlement's fields (PLAN-012f-b D16).
SETTLEMENT_FIELDS = (
    "settlement_count",
    "settlement_first_on",
    "settlement_cadence",
    "settlement_confirm_large_debit",
)
#: The number of payments Schedule settlement opens with (Split's too).
SETTLEMENT_COUNT_INITIAL = 3
#: Schedule settlement's cadences, in the order of Set up autopay's Repeat (D16).
SETTLEMENT_CADENCE_CHOICES = [
    (value, InstallmentCadence(value).label) for value in installments.SETTLEMENT_CADENCES
]
#: The name of the submit buttons, one per choice: without the page's script each button
#: says which choice it submits; with it the one shown matches the chosen radio.
COLLECT_SUBMIT = "submit_collect"


def unavailable_choices(
    *,
    can_charge_now: bool,
    can_schedule: bool = True,
    charge_now_why: str = "",
    bank_why: str = "",
) -> dict[str, str]:
    """The greyed tiles of How to collect, each with its one line (PLAN-012f-b D15; never
    hidden). ``can_charge_now``: the role may charge now (admin); ``can_schedule``:
    :func:`ledger.services.invoice_builder.can_schedule`; ``charge_now_why``:
    :func:`ledger.services.invoice_builder.charge_now_unavailable` (the pay-an-invoice flag
    off, no bank account on file); ``bank_why``:
    :func:`ledger.services.invoice_builder.bank_unavailable` (no bank account on file).
    Charge now needs an admin, then its flag and the bank; Schedule for a date, Set up
    autopay and Schedule settlement (D16) the bank, then an active lease with one line."""
    choice = invoice_builder.CollectChoice
    why: dict[str, str] = {}
    if not can_charge_now:
        why[choice.CHARGE_NOW.value] = CHARGE_NOW_NEEDS_ADMIN
    elif charge_now_why:
        why[choice.CHARGE_NOW.value] = charge_now_why
    for scheduled in (choice.SCHEDULE.value, choice.AUTOPAY.value, choice.SETTLEMENT.value):
        if bank_why:
            why[scheduled] = bank_why
        elif not can_schedule:
            why[scheduled] = invoice_builder.SCHEDULE_NOT_OFFERED
    return why


def initial_choice(
    value: object,
    *,
    can_charge_now: bool,
    can_schedule: bool = True,
    charge_now_why: str = "",
    bank_why: str = "",
) -> str:
    """The radio ``?collect=`` pre-selects: one of the tiles not greyed, else Save (no
    error)."""
    greyed = unavailable_choices(
        can_charge_now=can_charge_now,
        can_schedule=can_schedule,
        charge_now_why=charge_now_why,
        bank_why=bank_why,
    )
    text = str(value or "")
    if text in dict(COLLECT_CHOICES) and text not in greyed:
        return text
    return invoice_builder.CollectChoice.DRAFT.value


class CollectForm(forms.Form):
    """The draft form's last step, "How to collect" (PLAN-012e-2 D1, D3): the choice, the
    debit date of Schedule for a date (with the large-debit tick) and the received payment
    of Record payment now (method, date, reference, note). Each choice's fields carry
    ``data-shown-when`` (``ledger/shown_when.js``): shown, and sent, only while their radio
    is chosen; without the script every field shows. The pressed button's
    :data:`COLLECT_SUBMIT` wins over the radio (a page without the script). The form checks
    shape and the fields a choice needs; :func:`ledger.services.invoice_builder.collect`
    checks every rule."""

    collect = _QuietChoiceField(required=False, label="How to collect")
    schedule_on = forms.DateField(
        required=False,
        label="Debit on",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    confirm_large_debit = forms.BooleanField(
        required=False, widget=_ExactCheckboxInput, label="I confirm this large debit"
    )
    method = _QuietChoiceField(
        required=False, choices=(("", "—"), *MANUAL_METHOD_CHOICES), label="Received by"
    )
    paid_on = forms.DateField(
        required=False, label="Received on", widget=forms.DateInput(attrs={"type": "date"})
    )
    reference = forms.CharField(
        required=False,
        max_length=PAYMENT_REFERENCE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        label="Reference",
        help_text=REFERENCE_HELP,
    )
    payment_note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=PAYMENT_NOTE_MAX_LENGTH,
        validators=[validate_no_payment_data],
        label="Note",
    )
    autopay_cadence = _QuietChoiceField(
        required=False,
        choices=AutopayCadence.choices,
        initial=AutopayCadence.MONTHLY.value,
        label="Repeat",
    )
    autopay_first_on = forms.DateField(
        required=False, label="First debit", widget=forms.DateInput(attrs={"type": "date"})
    )
    autopay_end_rule = _QuietChoiceField(
        required=False,
        choices=AutopayEndRule.choices,
        initial=AutopayEndRule.COUNT.value,
        label="Ends",
    )
    autopay_end_count = forms.IntegerField(
        required=False,
        min_value=AUTOPAY_COUNT_MIN,
        max_value=AUTOPAY_COUNT_MAX,
        error_messages={
            "invalid": autopay_plans.END_COUNT,
            "min_value": autopay_plans.END_COUNT,
            "max_value": autopay_plans.END_COUNT,
        },
        label="Number of payments",
    )
    autopay_end_on = forms.DateField(
        required=False, label="Last debit on", widget=forms.DateInput(attrs={"type": "date"})
    )
    autopay_confirm_large_debit = forms.BooleanField(
        required=False, widget=_ExactCheckboxInput, label="I confirm this large debit"
    )
    settlement_count = forms.IntegerField(
        required=False,
        min_value=INSTALLMENTS_MIN,
        max_value=INSTALLMENTS_MAX,
        initial=SETTLEMENT_COUNT_INITIAL,
        error_messages={
            "invalid": invoice_builder.SETTLEMENT_COUNT,
            "min_value": invoice_builder.SETTLEMENT_COUNT,
            "max_value": invoice_builder.SETTLEMENT_COUNT,
        },
        label="Number of payments",
    )
    settlement_first_on = forms.DateField(
        required=False,
        label="First debit",
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={"invalid": invoice_builder.SETTLEMENT_FIRST_REQUIRED},
    )
    settlement_cadence = _QuietChoiceField(
        required=False,
        choices=SETTLEMENT_CADENCE_CHOICES,
        initial=InstallmentCadence.MONTHLY.value,
        label="Repeat",
    )
    settlement_confirm_large_debit = forms.BooleanField(
        required=False, widget=_ExactCheckboxInput, label="I confirm these large debits"
    )

    #: ``<id>`` conditions are alternatives (``|``); ``&`` joins the ones that must all hold.
    SHOWN_WHEN: dict[str, str] = {
        "schedule_on": "collect-schedule",
        "confirm_large_debit": "collect-schedule",
        "method": "collect-record",
        "paid_on": "collect-record",
        "reference": "collect-record",
        "payment_note": "collect-record",
        "autopay_cadence": "collect-autopay",
        "autopay_first_on": "collect-autopay",
        "autopay_end_rule": "collect-autopay",
        "autopay_end_count": f"collect-autopay&id_autopay_end_rule={AutopayEndRule.COUNT.value}",
        "autopay_end_on": f"collect-autopay&id_autopay_end_rule={AutopayEndRule.DATE.value}",
        "autopay_confirm_large_debit": "collect-autopay",
        "settlement_count": "collect-settlement",
        "settlement_first_on": "collect-settlement",
        "settlement_cadence": "collect-settlement",
        "settlement_confirm_large_debit": "collect-settlement",
    }

    def __init__(
        self,
        data: Any = None,
        *args: Any,
        can_charge_now: bool,
        can_schedule: bool = True,
        charge_now_why: str = "",
        bank_why: str = "",
        bank_url: str = "",
        charge_now_more: str = "",
        **kwargs: Any,
    ) -> None:
        if data is not None and data.get(COLLECT_SUBMIT):
            data = data.copy()
            data["collect"] = data.get(COLLECT_SUBMIT)
        super().__init__(data, *args, **kwargs)
        #: Each greyed tile's one line (PLAN-012f-b D15; see :func:`unavailable_choices`).
        self.unavailable = unavailable_choices(
            can_charge_now=can_charge_now,
            can_schedule=can_schedule,
            charge_now_why=charge_now_why,
            bank_why=bank_why,
        )
        #: Under a tile greyed by ``bank_why`` (no bank account on file): the operator page's
        #: link, and on Charge now ``charge_now_more`` (the flag off as well; PLAN-013a C3).
        self.bank_why, self.bank_url, self.charge_now_more = bank_why, bank_url, charge_now_more
        field = self.fields["collect"]
        if isinstance(field, forms.ChoiceField):
            field.choices = list(COLLECT_CHOICES)
        for name, radio in self.SHOWN_WHEN.items():
            self.fields[name].widget.attrs["data-shown-when"] = radio
        _label_check_number(self)

    def choice(self) -> invoice_builder.CollectChoice:
        return invoice_builder.CollectChoice(
            self.cleaned_data.get("collect") or invoice_builder.CollectChoice.DRAFT.value
        )

    def clean_payment_note(self) -> str:
        return visible_text(self.cleaned_data["payment_note"])

    def clean_collect(self) -> str:
        # A greyed tile's radio is disabled; a posted one is refused with its line all the same.
        chosen = self.cleaned_data.get("collect") or ""
        if chosen in self.unavailable:
            raise forms.ValidationError(self.unavailable[chosen])
        return str(chosen)

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        chosen = data.get("collect") or invoice_builder.CollectChoice.DRAFT.value
        if chosen == invoice_builder.CollectChoice.SCHEDULE and "schedule_on" not in self.errors:
            refused = invoice_builder.schedule_refusal(
                data.get("schedule_on"), timezone.localdate()
            )
            if refused is not None:
                self.add_error("schedule_on", refused.message)
        if chosen == invoice_builder.CollectChoice.RECORD:
            if not data.get("method"):
                self.add_error("method", rent_run.PAY_METHOD)
            if not data.get("paid_on") and "paid_on" not in self.errors:
                self.add_error("paid_on", "Enter the date the payment was received.")
            if not data.get("payment_note") and "payment_note" not in self.errors:
                self.add_error("payment_note", rent_run.PAY_NOTE_REQUIRED)
        self._drop_ignored_autopay(data, chosen)
        if chosen == invoice_builder.CollectChoice.AUTOPAY:
            self._clean_autopay(data, timezone.localdate())
        self._clean_settlement(data, chosen)
        return data

    def _clean_settlement(self, data: dict[str, Any], chosen: str) -> None:
        """Schedule settlement's fields count only for that choice (a page without the
        script sends every field); for it, the rules of
        :func:`ledger.services.invoice_builder.settlement_refusal` (checked again there)."""
        if chosen != invoice_builder.CollectChoice.SETTLEMENT:
            for name in SETTLEMENT_FIELDS:
                if name in self.errors:
                    del self.errors[name]
                    data[name] = None
            return
        if any(name in self.errors for name in SETTLEMENT_FIELDS):
            return
        if data.get("settlement_count") is None:
            self.add_error("settlement_count", invoice_builder.SETTLEMENT_COUNT)
            return
        refused = invoice_builder.settlement_refusal(self.settlement(), timezone.localdate())
        if refused is not None:
            self.add_error(refused.field, refused.message)

    def settlement(self) -> invoice_builder.SettlementInput:
        """Schedule settlement's debits as the form cleaned them."""
        data = self.cleaned_data
        return invoice_builder.SettlementInput(
            count=data.get("settlement_count"),
            first_on=data.get("settlement_first_on"),
            cadence=data.get("settlement_cadence") or "",
        )

    def _bound(self, name: str) -> Any:
        """A field's value as entered, as its type (``None`` when it is not one)."""
        value = self[name].value()
        if value is None or not isinstance(value, str):
            return value
        try:
            return self.fields[name].to_python(value)
        except forms.ValidationError:
            return None

    def settlement_count_shown(self) -> int:
        """The count Schedule settlement's button names (``Schedule 10 debits``): the one
        entered when it is 2 to 12, else the initial one."""
        count = self._bound("settlement_count")
        if isinstance(count, int) and INSTALLMENTS_MIN <= count <= INSTALLMENTS_MAX:
            return count
        return SETTLEMENT_COUNT_INITIAL

    def settlement_sentence(self, total: Decimal) -> str:
        """Schedule settlement's preview sentence for ``total`` from the values entered
        (:func:`ledger.services.installments.settlement_preview`; the page's script fetches
        it as the fields change), or ``""`` while they break a rule."""
        first = self._bound("settlement_first_on")
        entered = invoice_builder.SettlementInput(
            count=self._bound("settlement_count"),
            first_on=first if isinstance(first, date) else None,
            cadence=str(self["settlement_cadence"].value() or ""),
        )
        if entered.count is None or entered.first_on is None:
            return ""
        if invoice_builder.settlement_refusal(entered, timezone.localdate()) is not None:
            return ""
        try:
            return installments.settlement_preview(
                total, entered.count, entered.first_on, entered.cadence
            )
        except ValueError:
            return ""

    def _drop_ignored_autopay(self, data: dict[str, Any], chosen: str) -> None:
        """A page without the script sends every field: Set up autopay's fields count only
        for that choice, and the count and the date only for the end rule naming them (the
        service ignores them otherwise), so a leftover value there refuses nothing."""
        rule = data.get("autopay_end_rule")
        for name in AUTOPAY_FIELDS:
            ignored = chosen != invoice_builder.CollectChoice.AUTOPAY or (
                (name == "autopay_end_count" and rule != AutopayEndRule.COUNT)
                or (name == "autopay_end_on" and rule != AutopayEndRule.DATE)
            )
            if ignored and name in self.errors:
                del self.errors[name]
                data[name] = None

    def _clean_autopay(self, data: dict[str, Any], today: date) -> None:
        """Set up autopay's repeat, by the rules and sentences of
        :mod:`ledger.services.autopay_plans` (the service checks them again with the line)."""
        if data.get("autopay_cadence") not in AutopayCadence.values:
            self.add_error("autopay_cadence", autopay_plans.CHOOSE_CADENCE)
        first = data.get("autopay_first_on")
        if "autopay_first_on" not in self.errors:
            if first is None:
                self.add_error("autopay_first_on", autopay_plans.FIRST_REQUIRED)
            elif first <= today:
                self.add_error("autopay_first_on", autopay_plans.FIRST_NOT_AFTER_TODAY)
            elif first > today + timedelta(days=autopay_plans.FIRST_HORIZON_DAYS):
                self.add_error("autopay_first_on", autopay_plans.FIRST_TOO_FAR)
        if any(name in self.errors for name in ("autopay_end_count", "autopay_end_on")):
            return
        refused = autopay_plans.end_refusal(
            # Without a first date only the rule and its count or date are checked.
            first if isinstance(first, date) else date.min,
            data.get("autopay_end_rule") or "",
            data.get("autopay_end_count"),
            data.get("autopay_end_on"),
        )
        if refused is not None:
            self.add_error(f"autopay_{refused.field}", refused.message)

    def autopay(self) -> invoice_builder.AutopayInput:
        """Set up autopay's repeat as the form cleaned it."""
        data = self.cleaned_data
        return invoice_builder.AutopayInput(
            cadence=data.get("autopay_cadence") or "",
            first_on=data.get("autopay_first_on"),
            end_rule=data.get("autopay_end_rule") or "",
            end_count=data.get("autopay_end_count"),
            end_on=data.get("autopay_end_on"),
        )

    def cadence_words(self) -> list[dict[str, Any]]:
        """The words of Set up autopay's button, one per cadence (PLAN-012f-a D12): the
        page's script shows the chosen cadence's; without it the bound cadence's shows."""
        chosen = self["autopay_cadence"].value() or AutopayCadence.MONTHLY.value
        return [
            {"value": value, "word": autopay_plans.CADENCE_WORDS[value], "shown": value == chosen}
            for value in AutopayCadence.values
        ]

    def options(self) -> list[dict[str, Any]]:
        """The tiles as the fieldset renders them, with their ids (``collect-<value>``), each
        ``available`` or greyed with ``why`` (PLAN-012f-b D15); a tile greyed for no bank
        account on file carries ``why_url`` (the operator page) and, on Charge now,
        ``why_more`` (PLAN-013a C3). A greyed tile is never the chosen one."""
        chosen = self["collect"].value() or invoice_builder.CollectChoice.DRAFT.value
        if chosen in self.unavailable:
            chosen = invoice_builder.CollectChoice.DRAFT.value
        options = []
        for value, label in COLLECT_CHOICES:
            why = self.unavailable.get(value, "")
            no_bank = bool(self.bank_why) and why == self.bank_why
            charge_now = value == invoice_builder.CollectChoice.CHARGE_NOW.value
            options.append(
                {
                    "value": value,
                    "label": label,
                    "id": f"collect-{value}",
                    "checked": value == chosen,
                    "available": value not in self.unavailable,
                    "why": why,
                    "why_url": self.bank_url if no_bank else "",
                    "why_more": self.charge_now_more if no_bank and charge_now else "",
                }
            )
        return options

    def debit_on(self) -> date | None:
        """Schedule for a date's debit date as entered, for the button's words (``|day``),
        or ``None`` when there is none or it is not a date."""
        value = self["schedule_on"].value()
        if isinstance(value, date):
            return value
        try:
            return self.fields["schedule_on"].to_python(value)
        except forms.ValidationError:
            return None

    def payment(self) -> invoice_builder.PaymentInput | None:
        data = self.cleaned_data
        if not data.get("method") or data.get("paid_on") is None:
            return None
        return invoice_builder.PaymentInput(
            method=data["method"],
            paid_on=data["paid_on"],
            reference=(data.get("reference") or "").strip(),
            note=data.get("payment_note") or "",
        )


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


class VoidManualForm(forms.Form):
    """Billing's void of a payment recorded outside the processor (PLAN-012f-c D2): the
    reason and the one-time key; the service re-checks every rule under the row locks."""

    creation_key = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason",
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=200,
        validators=[validate_no_payment_data],
        help_text="Required: why the payment is voided. Never an account number.",
        error_messages={"required": "A reason is required: say why the payment is voided."},
    )

    def clean_reason(self) -> str:
        reason = visible_text(self.cleaned_data["reason"])
        if not reason:
            raise forms.ValidationError(self.fields["reason"].error_messages["required"])
        return reason

    def holds_payment_data(self) -> bool:
        """Whether the posted reason holds a card or account number (Rule 1): the held
        form then keeps its error but never its value (the Add payment ``reference``
        precedent, :meth:`AddPaymentForm._holds_payment_data`)."""
        try:
            validate_no_payment_data(str(self.data.get("reason") or ""))
        except forms.ValidationError:
            return True
        return False


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

    kind = _QuietChoiceField(choices=DepositResolutionKind.choices, label="Resolution")
    amount = forms.DecimalField(
        min_value=Decimal("0.01"), max_value=RENT_MAX, max_digits=12, decimal_places=2
    )
    resolved_on = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    method = _QuietChoiceField(
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
        #: Another lease can receive the deposit: without one the page leaves the
        #: transfer-to-lease field out (PLAN-012c D8); a posted one is checked as before.
        self.has_targets = bool(targets)
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
    reason = _QuietChoiceField(
        choices=[("", "—"), *LeaseEndReason.choices],
        help_text="Why the lease ends; Other needs a few words below.",
    )
    reason_note = forms.CharField(
        label="Reason in words",
        required=False,
        strip=False,
        # PLAN-012c D8: shown while the reason is Other (ledger/shown_when.js); without
        # the script it stays on the page, and the service's rule is the same either way.
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "maxlength": PAYMENT_NOTE_MAX_LENGTH,
                SHOWN_WHEN: f"id_reason={LeaseEndReason.OTHER.value}",
            }
        ),
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
    """Step 3: the typed deductions from the deposit, one numbered row each; an empty
    row is skipped. The total line under the table sums the amounts typed (PLAN-012d
    D10). The service checks every rule again."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for index in range(1, DEDUCTION_ROWS + 1):
            self.fields[f"kind_{index}"] = _QuietChoiceField(
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
                widget=forms.NumberInput({"class": TOTAL_INPUT_CLASS}),
            )

    def rows(self) -> list[dict[str, Any]]:
        return [
            {
                "number": index,
                **{part: self[f"{part}_{index}"] for part in ("kind", "description", "amount")},
            }
            for index in range(1, DEDUCTION_ROWS + 1)
        ]

    def has_rows(self) -> bool:
        """Whether any row holds a value (saved, suggested or typed): without one, the page
        shows the "Add deduction" control only (PLAN-012c D8)."""
        return any(
            str(self[f"{part}_{index}"].value() or "").strip()
            for index in range(1, DEDUCTION_ROWS + 1)
            for part in ("kind", "description", "amount")
        )

    def total(self) -> Decimal:
        """The sum of the amounts the form holds; an invalid amount counts as nothing."""
        total = Decimal("0.00")
        for index in range(1, DEDUCTION_ROWS + 1):
            bound = self[f"amount_{index}"]
            try:
                value = bound.field.clean(bound.value())
            except forms.ValidationError:
                continue
            if isinstance(value, Decimal):
                total += value
        return total

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

    method = _QuietChoiceField(
        label="Refund method",
        required=False,
        choices=[("", "—"), *RefundMethod.choices],
        help_text=provider_sentence(
            "How the refund is paid: Zelle, wire or check, never through {provider}."
        ),
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
        error_messages={"required": "Check the box to confirm."},
    )


# --- Transfer (PLAN-012e D6) ---------------------------------------------------------------


class TransferOperatorForm(forms.Form):
    """Step 1 of the transfer: the operator picked from the search. The service checks
    that it is not archived and not the outgoing one."""

    operator = forms.UUIDField(
        widget=forms.HiddenInput, error_messages={"required": "Choose an operator."}
    )


class TransferIncomingForm(BillingStepMixin, forms.Form):
    """The incoming lease's terms, its deposit and its setup checklist (one price per
    checklist item that is not a deposit: empty with the catalogue's default price as the
    placeholder; a price means charged, PLAN-012d D9), and its Billing details (PLAN-014
    C4, :class:`BillingStepMixin`: the incoming operator is known, so every ``same:``
    choice is one of its leases). The service checks every rule again."""

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
        # PLAN-012c D8 (Batch D carry-over): shown while "Debited twice a month" is ticked,
        # as on the lease form; the tick is the page's only, the rules are unchanged.
        widget=forms.NumberInput(attrs={SHOWN_WHEN: TWICE_MONTHLY_ID}),
    )
    payment_channel = _QuietChoiceField(
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

    def __init__(
        self,
        *args: Any,
        fee_types: Iterable[FeeType] = (),
        deposit_offered: bool = True,
        accounts: Sequence[SharedAccount] = (),
        enter_refusal: str = "",
        **kwargs: Any,
    ) -> None:
        """``deposit_offered``: the catalogue offers a security deposit. Without one the
        deposit field is not shown (PLAN-012c D8) but stays on the form, so a posted
        amount is refused by the service as before."""
        super().__init__(*args, **kwargs)
        self.fee_types = list(fee_types)
        self.deposit_offered = deposit_offered
        for fee_type in self.fee_types:
            self.fields[f"{_setup_name(fee_type)}_price"] = _setup_price_field(fee_type)
        self.add_billing_step(accounts, enter_refusal=enter_refusal)

    def lease_fields(self) -> list[forms.BoundField]:
        return [
            field
            for field in self.visible_fields()
            if not field.name.startswith(SETUP)
            and field.name not in BILLING_STEP_FIELDS
            and (field.name != "deposit_amount" or self.deposit_offered or field.errors)
        ]

    def twice_monthly(self) -> bool:
        """Whether the page's "Debited twice a month" tick starts ticked (PLAN-012c D8)."""
        return _twice_monthly(self["second_debit_day"])

    def setup_rows(self) -> list[dict[str, Any]]:
        return [
            {"fee_type": fee_type, "price": self[f"{_setup_name(fee_type)}_price"]}
            for fee_type in self.fee_types
        ]

    def setup_total(self) -> Decimal:
        """The sum of the prices typed so far (the total line under the table)."""
        return _setup_total(self, self.fee_types)

    def setup_items(self) -> list[tuple[FeeType, Decimal]]:
        """The rows of a valid form with a price, as (fee type, price), in catalogue
        order (PLAN-012d D9: a price means charged; there is no tick box)."""
        items = []
        for fee_type in self.fee_types:
            price = self.cleaned_data.get(f"{_setup_name(fee_type)}_price")
            if price is not None:
                items.append((fee_type, price))
        return items


class TransferConfirmForm(forms.Form):
    confirmed = forms.BooleanField(
        label="I have read the effects above and want to transfer the store",
        required=True,
        widget=_ExactCheckboxInput,
        error_messages={"required": "Check the box to confirm."},
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


# --- Notes & follow-ups and calendar (PLAN-011b D7, D8) -------------------------------------------

#: The longest contact note accepted.
CONTACT_BODY_MAX_LENGTH = 2000


class ContactEditForm(forms.Form):
    """The fields of a contact entry its author may change (PLAN-012 D2). The form checks
    shape; the service (:mod:`ledger.services.contacts`) decides every rule."""

    kind = _QuietChoiceField(
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


#: The quick outcomes of a call (the owner's "Log contact" dialog, 2026-11-01): the radio's
#: value, its chip's words, and the sentence the entry's note starts with.
CALL_OUTCOMES: dict[str, tuple[str, str]] = {
    "answered": ("Answered", "Answered."),
    "no_answer": ("No answer", "No answer."),
    "left_message": ("Left a message", "Left a message."),
    "wrong_number": ("Wrong number", "Wrong number."),
}
#: An e-mail's note without words of its own.
EMAIL_SENTENCE = "E-mail sent."
#: A call with neither an outcome nor a note.
CALL_NOTE_REQUIRED = "Choose what happened on the call, or write a note."
#: The kind's sentence and the note together exceed the note's limit.
CALL_NOTE_TOO_LONG = (
    f"With its first sentence the note is too long: keep it under {CONTACT_BODY_MAX_LENGTH:,} "
    "characters in all."
)


class ContactEntryForm(ContactEditForm):
    """Add one contact entry to an operator's log (PLAN-011b D7, PLAN-012 D3, D5), the
    "Log contact" dialog (owner, 2026-11-01).

    ``kind``: four radio cards. ``call_outcome``: a call's quick outcome (one of
    :data:`CALL_OUTCOMES`, optional); with one the note may be left empty, and the entry's
    note is the outcome's sentence, then the note when there is one ("No answer. Try
    Friday."). It is read only for a call. An e-mail and a promise to pay need no note either:
    theirs starts "E-mail sent." or "Promised $3,500.00 by Nov 4, 2026." (a promise still
    needs its date and amount). A visit, and a call without an outcome, need a note. The
    promise fields and the outcome chips carry ``data-shown-when`` (ledger/shown_when.js) on
    the kind's radios: shown, and sent, only for their kind; the service refuses promise
    fields on any other kind either way.

    ``lease``: required when the operator has more than one lease in effect
    (``lease_required``). ``previous`` and ``outcome``: offered when the operator has an
    open promise (``promises``); ``previous`` is required, ``outcome`` defaults to "Still
    open" (PLAN-012c-b D1). Each choice of ``promises`` may carry the promise's date and
    amount for the page's "Rescheduled" preselection."""

    lease = _QuietChoiceField(required=False, label="Store")
    kind = _QuietChoiceField(
        choices=ContactKind.choices,
        widget=forms.RadioSelect,
        label="Kind",
        error_messages={"required": "Choose the kind of contact."},
    )
    call_outcome = _QuietChoiceField(
        required=False,
        label="Outcome",
        choices=[(value, words) for value, (words, _) in CALL_OUTCOMES.items()],
        widget=forms.RadioSelect,
    )
    body = forms.CharField(
        required=False,
        label="Note",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "What was said, what happens next"}),
        max_length=CONTACT_BODY_MAX_LENGTH,
        validators=[validate_no_payment_data],
    )
    previous = _QuietChoiceField(required=False, label="Previous promise")
    outcome = _QuietChoiceField(
        required=False,
        label="What happened to it",
        choices=OUTCOME_CHOICES,
        widget=forms.RadioSelect,
        initial="open",
    )
    field_order = [
        "lease",
        "kind",
        "call_outcome",
        "happened_on",
        "promised_date",
        "promised_amount",
        "previous",
        "outcome",
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
        # The kind's radios are "<id>_<n>" in the order of ContactKind.
        kind_id = self.auto_id % "kind" if isinstance(self.auto_id, str) else "kind"
        kinds = list(ContactKind.values)
        call = f"{kind_id}_{kinds.index(ContactKind.CALL)}"
        promise = f"{kind_id}_{kinds.index(ContactKind.PROMISE)}"
        self.fields["call_outcome"].widget.attrs["data-shown-when"] = call
        for name in ("promised_date", "promised_amount"):
            self.fields[name].widget.attrs["data-shown-when"] = promise

    def clean_body(self) -> str:
        return visible_text(self.cleaned_data.get("body") or "")

    def note_required(self) -> bool:
        """Whether the kind shown asks for a note (the dialog's marker): a visit, and a call
        without an outcome."""
        kind = self["kind"].value()
        return kind == ContactKind.VISIT or (
            kind == ContactKind.CALL and not self["call_outcome"].value()
        )

    def _sentence(self, cleaned: dict[str, Any]) -> str | None:
        """The sentence the entry's note starts with (owner, 2026-11-01): a call's outcome,
        "E-mail sent.", or the promise's amount and date; ``""`` when the kind needs a note
        instead; ``None`` when a promise lacks its date or amount (refused on the fields)."""
        kind = cleaned.get("kind")
        if kind == ContactKind.CALL:
            chosen = cleaned.get("call_outcome")
            return CALL_OUTCOMES[chosen][1] if chosen else ""
        if kind == ContactKind.EMAIL:
            return EMAIL_SENTENCE
        if kind == ContactKind.PROMISE:
            on, amount = cleaned.get("promised_date"), cleaned.get("promised_amount")
            if on is not None and amount is not None:
                return f"Promised {format_amount(amount)} by {day_text(on)}."
            if cleaned.get("outcome") == "rescheduled":
                # The service takes the old promise's date and amount; a note says the rest.
                return ""
            for name, value in (("promised_date", on), ("promised_amount", amount)):
                if value is None and name not in self.errors:
                    self.add_error(name, PROMISE_FIELDS_REQUIRED)
            return None
        return ""

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if "body" in self.errors:
            return cleaned
        note = cleaned.get("body") or ""
        sentence = self._sentence(cleaned)
        if sentence is None:
            return cleaned
        if sentence:
            body = f"{sentence} {note}" if note else sentence
            if len(body) > CONTACT_BODY_MAX_LENGTH:
                self.add_error("body", CALL_NOTE_TOO_LONG)
            else:
                cleaned["body"] = body
        elif not note:
            self.add_error(
                "body",
                CALL_NOTE_REQUIRED
                if cleaned.get("kind") == ContactKind.CALL
                else ContactEditForm.base_fields["body"].error_messages["required"],
            )
        return cleaned


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

    lease = _QuietChoiceField(
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


class NoteForm(forms.Form):
    """A store note's body (PLAN-014 B5): required, at most 2,000 characters, no card or
    account number (:mod:`ledger.services.notes` checks the same)."""

    body = forms.CharField(
        label="Note",
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": "Write a note about this store\u2026"}
        ),
        max_length=NOTE_BODY_MAX_LENGTH,
        validators=[validate_no_payment_data],
        error_messages={"required": "Write the note first."},
    )

    def clean_body(self) -> str:
        body = visible_text(self.cleaned_data["body"])
        if not body:
            raise forms.ValidationError(self.fields["body"].error_messages["required"])
        return body


class NoteDeleteForm(forms.Form):
    """Why a store note is deleted (PLAN-014 B5): required, at most 200 characters."""

    reason = forms.CharField(
        label="Reason",
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=NOTE_DELETE_REASON_MAX_LENGTH,
        validators=[validate_no_payment_data],
        error_messages={"required": "A reason is required: say why this note is deleted."},
    )

    def clean_reason(self) -> str:
        reason = visible_text(self.cleaned_data["reason"])
        if not reason:
            raise forms.ValidationError(self.fields["reason"].error_messages["required"])
        return reason


class _CalendarEntryFields(forms.Form):
    """The fields of a calendar note or reminder (D8) that the edit page and the New calendar
    entry dialog share."""

    kind = _QuietChoiceField(
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

    def clean_title(self) -> str:
        title = visible_text(self.cleaned_data["title"])
        if not title:
            raise forms.ValidationError(self.fields["title"].error_messages["required"])
        return title


class CalendarEntryForm(_CalendarEntryFields):
    """Change a calendar note or reminder (D8; the edit page): the Store, Operator and Lease
    selects. Record choices are UUIDs; the view checks they exist."""

    store = _QuietChoiceField(required=False, label="Store")
    operator = _QuietChoiceField(required=False, label="Operator")
    lease = _QuietChoiceField(required=False, label="Lease")

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
                (str(pk), f"{label} from {day_text(started) if started else 'no start date'}")
                for pk, label, started in Lease.objects.order_by(
                    "store__external_label", "started_on", "id"
                ).values_list("pk", "store__external_label", "started_on")
            ],
        }
        for name, options in choices.items():
            field = self.fields[name]
            if isinstance(field, forms.ChoiceField):
                field.choices = [("", "None")] + options


#: What the New calendar entry dialog links an entry to (``link_kind``), with the model.
CALENDAR_LINK_MODELS: dict[str, type[Store] | type[Operator] | type[Lease]] = {
    "store": Store,
    "operator": Operator,
    "lease": Lease,
}
CALENDAR_LINK_UNKNOWN = "Choose a store, operator or lease from the list."


def _calendar_link_exists(kind: str, key: uuid.UUID) -> bool:
    """A record of ``kind`` with ``key`` exists (an operator: not archived)."""
    if kind == "operator":
        return Operator.objects.filter(pk=key, archived_at__isnull=True).exists()
    model = CALENDAR_LINK_MODELS.get(kind)
    return model is not None and model._default_manager.filter(pk=key).exists()


class CalendarEntryDialogForm(_CalendarEntryFields):
    """The New calendar entry dialog (owner, 2026-11-06; ledger/calendar/_entry_dialog.html):
    Kind as radio cards, and one link (``link_kind`` and ``link_id``, hidden, set by the
    type-ahead of ledger/calendar_dialog.js) in place of the three selects. A link must name a
    record that exists (an operator not archived); both empty is no link. The view turns a
    store into the store with its current lease and that lease's operator."""

    kind = _QuietChoiceField(
        choices=CalendarEntryKind.choices,
        widget=forms.RadioSelect,
        label="Kind",
        error_messages={"required": "Choose note or reminder."},
    )
    # Checked together in clean(): a hidden field's own error would show nowhere.
    link_kind = forms.CharField(required=False, widget=forms.HiddenInput)
    link_id = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The dialog writes "09:00 New York" beside the switch in place of the help text.
        morning = self.fields["email_morning"]
        morning.help_text = ""
        morning.widget.attrs.update({"class": "switch", "role": "switch"})

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        kind = cleaned.get("link_kind") or ""
        raw = cleaned.get("link_id") or ""
        cleaned["link_kind"], cleaned["link_id"] = "", None
        if not kind and not raw:
            return cleaned
        try:
            key = uuid.UUID(raw)
        except ValueError:
            key = None
        if key is None or not _calendar_link_exists(kind, key):
            raise forms.ValidationError(CALENDAR_LINK_UNKNOWN)
        cleaned["link_kind"], cleaned["link_id"] = kind, key
        return cleaned


# --- Installment plans (PLAN-012e D10, D11) ----------------------------------------------


def _cadence_field(*, split: bool = False) -> forms.ChoiceField:
    """The cadence select: every cadence (a plan's reschedule), or Split into
    installments' own: the autopay control's Monthly, Weekly, Yearly, as Schedule
    settlement's (PLAN-012f-c D13; every two weeks is not offered there, existing
    biweekly plans keep it)."""
    if split:
        return _QuietChoiceField(
            choices=SETTLEMENT_CADENCE_CHOICES,
            initial=InstallmentCadence.MONTHLY,
            error_messages={
                "required": autopay_plans.CHOOSE_CADENCE,
                "invalid_choice": autopay_plans.CHOOSE_CADENCE,
            },
        )
    return _QuietChoiceField(
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
    cadence = _cadence_field(split=True)


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
