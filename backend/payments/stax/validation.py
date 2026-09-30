"""Local checks that run before any Stax request (PLAN-005 §4.2, docs/CLAUDE.md Rule 3).

A request that Stax would reject is a failed request, and ten failed requests in a
minute block our IP for an hour (TODO-VERIFY-001). Everything that can be checked here
is checked here. Every failure raises :class:`~payments.errors.ValidationFailed`, whose
message names the field and never the value, and the request is never sent.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from payments.errors import ValidationFailed
from payments.provider import Amounts, DateRange, require_money

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


def external_id(name: str, value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > EXTERNAL_ID_MAX_LENGTH
        or not _EXTERNAL_ID.fullmatch(value)
    ):
        raise ValidationFailed(f"{name} must be a non-empty identifier without whitespace")
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
