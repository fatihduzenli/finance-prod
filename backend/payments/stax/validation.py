"""Local checks that run before any Stax request (PLAN-005 §4.2, docs/CLAUDE.md Rule 3).

A request that Stax would reject is a failed request, and ten failed requests in a
minute block our IP for an hour (TODO-VERIFY-001). Everything that can be checked here
is checked here. Every failure raises :class:`~payments.errors.ValidationFailed`, whose
message names the field and never the value, and the request is never sent.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from collections.abc import Iterable
from decimal import Decimal
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from payments.errors import ValidationFailed
from payments.provider import Amounts, CustomerInput, DateRange, require_money

IDEMPOTENCY_ID_MAX_LENGTH = 255
EXTERNAL_ID_MAX_LENGTH = 255
DATE_RANGE_MAX_DAYS = 366
RULE_PREFIX = "DTSTART="
#: Identifiers are substituted into URL paths, so beyond "non-empty, no whitespace"
#: (§4.2) they are restricted to characters that cannot change the path: no ``/``,
#: ``.``, ``?``, ``#`` or ``%``.
_EXTERNAL_ID = re.compile(r"[A-Za-z0-9_-]+")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_CENT = Decimal("0.01")
#: create-customer: every text field we send is ``Max: 255`` (PLAN-006b §1).
CUSTOMER_TEXT_MAX_LENGTH = 255
#: create-customer: ``phone`` matches ``/[0-9]{10,15}/`` (PLAN-006b §1).
PHONE_MIN_DIGITS = 10
PHONE_MAX_DIGITS = 15
_PHONE_DIGITS = re.compile(rf"[0-9]{{{PHONE_MIN_DIGITS},{PHONE_MAX_DIGITS}}}")
_PHONE_SEPARATORS = re.compile(r"[ .()-]")


def external_id(name: str, value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > EXTERNAL_ID_MAX_LENGTH
        or not _EXTERNAL_ID.fullmatch(value)
    ):
        raise ValidationFailed(f"{name} must be a non-empty identifier without whitespace")
    return value


#: Longest webhook target URL sent.
WEBHOOK_URL_MAX_LENGTH = 2048


def webhook_target_url(value: str) -> str:
    """An absolute ``https`` URL with a host, no credentials, query, fragment or
    whitespace (PLAN-008 D9). The message never includes the value: it holds the token."""
    try:
        parts = urlsplit(value) if isinstance(value, str) else None
    except ValueError:
        parts = None
    if (
        parts is None
        or len(value) > WEBHOOK_URL_MAX_LENGTH
        or parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in value)
        or _CONTROL_CHARACTERS.search(value)
    ):
        raise ValidationFailed("target_url must be an absolute https URL")
    return value


def email(name: str, value: str) -> str:
    try:
        validate_email(value)
    except ValidationError:
        raise ValidationFailed(f"{name} must be a valid e-mail address") from None
    return value


def emails(name: str, values: Iterable[str]) -> list[str]:
    return [email(name, value) for value in values]


def _two_decimals(name: str, value: Decimal) -> None:
    require_money(name, value)
    if value != value.quantize(_CENT):
        raise ValidationFailed(f"{name} must have at most two decimal places")


def positive_amount(name: str, value: Decimal) -> Decimal:
    _two_decimals(name, value)
    if value <= 0:
        raise ValidationFailed(f"{name} must be greater than zero")
    return value


def amounts(value: Amounts) -> Amounts:
    """Positive total and subtotal, non-negative tax, sums that add up exactly."""
    positive_amount("total", value.total)
    positive_amount("subtotal", value.subtotal)
    _two_decimals("tax", value.tax)
    if value.tax < 0:
        raise ValidationFailed("tax must not be negative")
    if value.total != value.subtotal + value.tax:
        raise ValidationFailed("total must equal subtotal plus tax")
    if not value.line_items:
        raise ValidationFailed("line_items must not be empty")
    for item in value.line_items:
        if not item.description.strip():
            raise ValidationFailed("line item description must not be empty")
        if item.quantity <= 0:
            raise ValidationFailed("line item quantity must be greater than zero")
        positive_amount("line item unit_price", item.unit_price)
    if sum((item.total for item in value.line_items), Decimal(0)) != value.subtotal:
        raise ValidationFailed("line items must sum to subtotal")
    return value


def idempotency_id(value: str) -> str:
    """A canonical UUID string, at most 255 characters."""
    if not isinstance(value, str) or len(value) > IDEMPOTENCY_ID_MAX_LENGTH:
        raise ValidationFailed("idempotency_id must be a UUID string")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        raise ValidationFailed("idempotency_id must be a UUID string") from None
    if canonical != value.lower():
        raise ValidationFailed("idempotency_id must be a UUID string")
    return value


def rule(value: str) -> str:
    """A recurrence rule string beginning with ``DTSTART=``."""
    if (
        not isinstance(value, str)
        or not value.startswith(RULE_PREFIX)
        or len(value) == len(RULE_PREFIX)
        or _CONTROL_CHARACTERS.search(value)
    ):
        raise ValidationFailed(f"rule must be a recurrence rule beginning with {RULE_PREFIX}")
    return value


def date_range(value: DateRange) -> DateRange:
    if value.start >= value.end:
        raise ValidationFailed("date range must start before it ends")
    if (value.end - value.start).days > DATE_RANGE_MAX_DAYS:
        raise ValidationFailed(f"date range must span at most {DATE_RANGE_MAX_DAYS} days")
    return value


def positive_int(name: str, value: int | None) -> int | None:
    if value is not None and (isinstance(value, bool) or value < 1):
        raise ValidationFailed(f"{name} must be a positive integer")
    return value


def normalise_phone(value: str) -> str | None:
    """The one phone rule (PLAN-006b §1), shared by the operator form and the adapter.

    Spaces, hyphens, dots, parentheses and one leading ``+`` are removed; what remains
    must be 10 to 15 ASCII digits and is returned. An empty (or blank) value returns
    ``""``. Anything else returns ``None``.
    """
    text = value.strip()
    if not text:
        return ""
    text = _PHONE_SEPARATORS.sub("", text.removeprefix("+"))
    return text if _PHONE_DIGITS.fullmatch(text) else None


def phone(name: str, value: str) -> str:
    """The normalised digits, or ``""`` when empty; anything else is refused."""
    digits = normalise_phone(value) if isinstance(value, str) else None
    if digits is None:
        raise ValidationFailed(f"{name} must be {PHONE_MIN_DIGITS} to {PHONE_MAX_DIGITS} digits")
    return digits


def _text_cap(name: str, value: str) -> None:
    if len(value) > CUSTOMER_TEXT_MAX_LENGTH:
        raise ValidationFailed(f"{name} must be at most {CUSTOMER_TEXT_MAX_LENGTH} characters")


def customer(value: CustomerInput) -> CustomerInput:
    """The 255-character caps and the phone rule; returns the input with normalised digits."""
    _text_cap("first_name", value.first_name)
    _text_cap("last_name", value.last_name)
    _text_cap("company_name", value.company_name)
    _text_cap("operator_email", value.operator_email)
    _text_cap("reference", value.reference)
    return dataclasses.replace(value, phone=phone("phone", value.phone))
