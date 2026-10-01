"""Rule 1 trimming of provider objects, shared by the adapter and the webhook receiver.

``payments.stax.mapping.summary`` keeps the scalar fields of one object; :func:`redact`
applies the same rules at every depth of a JSON value, for the stored copy of a webhook
body (docs/qa/PLAN-008-webhooks.md D3). It lives outside ``payments.stax`` because the
webhook receiver imports nothing from the adapter package.

Rules (docs/CLAUDE.md Rule 1): a key that names account, routing, bank, card-number,
token, secret or password data is dropped with its value; of a ``payment_method``
object only :data:`PAYMENT_METHOD_KEYS` survive; any string, number or key whose text
holds a card number or a routing and account number is dropped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError

from ledger.validators import validate_no_payment_data

#: docs/CLAUDE.md Rule 1: of a payment-method object, keep at most these keys.
PAYMENT_METHOD_KEYS: tuple[str, ...] = (
    "id",
    "method",
    "card_type",
    "card_last_four",
    "card_exp",
    "nickname",
)
#: Keys whose values never enter a summary, whatever they hold.
SENSITIVE_KEY = re.compile(
    r"(account|routing|bank_|cvv|cvc|card_number|cardnumber|token|secret|password)",
    re.IGNORECASE,
)


class _Dropped:
    """Marker for a value that :func:`redact` removes."""


DROPPED = _Dropped()


def is_safe_text(value: str) -> bool:
    """False when ``value`` holds a card number or a routing and account number."""
    try:
        validate_no_payment_data(value)
    except ValidationError:
        return False
    return True


def payment_method_subset(value: Mapping[str, object]) -> dict[str, str]:
    """The :data:`PAYMENT_METHOD_KEYS` of a payment-method object, as safe strings."""
    return {
        name: str(value[name])
        for name in PAYMENT_METHOD_KEYS
        if isinstance(value.get(name), str | int)
        and not isinstance(value.get(name), bool)
        and is_safe_text(str(value[name]))
    }


def redact(value: object) -> Any:
    """A copy of a JSON value with Rule 1 applied at every depth.

    Returns :data:`DROPPED` when ``value`` itself is removed (an unsafe string or
    number, or a type JSON parsing with ``Decimal`` never produces).
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value if is_safe_text(value) else DROPPED
    if isinstance(value, int | Decimal):
        return value if is_safe_text(str(value)) else DROPPED
    if isinstance(value, Mapping):
        kept: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or SENSITIVE_KEY.search(key) or not is_safe_text(key):
                continue
            if key == "payment_method" and isinstance(item, Mapping):
                kept[key] = payment_method_subset(item)
                continue
            redacted = redact(item)
            if redacted is not DROPPED:
                kept[key] = redacted
        return kept
    if isinstance(value, list | tuple):
        return [item for item in (redact(entry) for entry in value) if item is not DROPPED]
    return DROPPED
