"""Operator forms (docs/qa/PLAN-006-operators.md §4.1).

Server-side validation is what counts; the browser checks (``type=email``,
``required``) are a convenience. Every text field keeps the model's card-number guard,
which runs when the form validates the model instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django import forms
from django.core.validators import RegexValidator

from ledger.models import Operator

if TYPE_CHECKING:
    _OperatorFormBase = forms.ModelForm[Operator]
else:  # ModelForm is only generic in the type stubs.
    _OperatorFormBase = forms.ModelForm

IDENTIFIER_MAX_LENGTH = 255
#: Letters, digits, hyphen and underscore only, 1 to 255 characters.
IDENTIFIER_VALIDATOR = RegexValidator(
    r"\A[A-Za-z0-9_-]{1,255}\Z",
    message="Use letters, digits, hyphens and underscores only.",
    code="identifier",
)


class OperatorForm(_OperatorFormBase):
    """Create and edit. The e-mail is lower-cased and stripped; empty means none."""

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
