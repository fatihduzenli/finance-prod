"""``{{ charge|kind_label }}``: what a one-off charge's kind reads on a page (PLAN-012f-b
§10 D19): "Bank debit" for a rent charge's bank debit from Add payment, the category's
label otherwise (:func:`ledger.services.one_off_charges.kind_label`, the one rule)."""

from __future__ import annotations

from django import template

from ledger.models import OneOffCharge
from ledger.services.one_off_charges import kind_label as charge_kind_label

register = template.Library()


@register.filter(name="kind_label")
def kind_label(charge: OneOffCharge) -> str:
    """The charge's kind as staff read it."""
    return charge_kind_label(charge)
