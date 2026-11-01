"""Placeholders of the message templates (docs/qa/PLAN-011b D4, PLAN-012e D8, PLAN-014 D4).

A template's subject and body may name exactly the placeholders of its code's list, each as
``{name}``: :data:`FIELDS` (the closed set of PLAN-014 D4) for every message about a charge,
:data:`FINAL_STATEMENT_FIELDS` for the template ``final_statement``, and for the two portal
messages of PLAN-014 H2 :data:`PORTAL_INVITE_FIELDS` (``portal_invite``, whose own placeholder
``{set_password_link}`` no other template may use) and :data:`PORTAL_RESET_FIELDS`
(``portal_reset``, with ``{reset_link}``) (:func:`fields_for`); :data:`MERGE_FIELDS` is every
placeholder of any list. ``{{`` and ``}}`` write a literal
brace. Nothing else is accepted: no attribute or index access (``{operator_name.__dict__}``,
``{amount[0]}``), no conversion (``{amount!r}``), no format specification
(``{amount:>99999}``), no positional or unknown placeholder (``{}``, ``{0}``,
``{__class__}``). :func:`problems` lists what a text breaks, for the refusal on save (an
unknown placeholder is named, nothing else of the text is repeated); :func:`render` refuses
the same texts with :class:`TemplateRefused` and substitutes values from a mapping of plain
strings only. Rendering is :class:`string.Formatter` with every lookup, conversion and
specification overridden to accept nothing beyond a whitelisted name: Django templates are
never used.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping, Sequence
from typing import Any

#: The placeholders of a message about a charge (PLAN-014 D4), in the order the edit page
#: lists them.
FIELDS: tuple[str, ...] = (
    "operator_name",
    "store_number",
    "store_name",
    "amount",
    "charge_name",
    "due_date",
    "debit_date",
    "retry_date",
    "days_late",
    "bank_last4",
    "return_reason",
    "pay_link",
    "store_link",
    "support_email",
    "company_name",
)
#: The template code of the final statement e-mail (PLAN-012e D8) and its fields.
FINAL_STATEMENT_CODE = "final_statement"
FINAL_STATEMENT_FIELDS: tuple[str, ...] = (
    "operator",
    "store",
    "company",
    "refund_due",
    "amount_owed",
    "end_date",
)
#: PLAN-014 H2: the portal invitation (``portal_invite``) and the password reset
#: (``portal_reset``), each with its one-time link, which only that template may name.
PORTAL_INVITE_CODE = "portal_invite"
PORTAL_RESET_CODE = "portal_reset"
PORTAL_INVITE_FIELDS: tuple[str, ...] = (
    "operator_name",
    "company_name",
    "support_email",
    "set_password_link",
)
PORTAL_RESET_FIELDS: tuple[str, ...] = (
    "operator_name",
    "company_name",
    "support_email",
    "reset_link",
)
PORTAL_CODES: frozenset[str] = frozenset({PORTAL_INVITE_CODE, PORTAL_RESET_CODE})
_CODE_FIELDS: dict[str, tuple[str, ...]] = {
    FINAL_STATEMENT_CODE: FINAL_STATEMENT_FIELDS,
    PORTAL_INVITE_CODE: PORTAL_INVITE_FIELDS,
    PORTAL_RESET_CODE: PORTAL_RESET_FIELDS,
}
MERGE_FIELDS: tuple[str, ...] = tuple(
    dict.fromkeys((*FIELDS, *FINAL_STATEMENT_FIELDS, *PORTAL_INVITE_FIELDS, *PORTAL_RESET_FIELDS))
)
_ALLOWED = frozenset(MERGE_FIELDS)
#: The exact sets of values :func:`render` accepts.
_VALUE_SETS = (
    frozenset(FIELDS),
    frozenset(FINAL_STATEMENT_FIELDS),
    frozenset(PORTAL_INVITE_FIELDS),
    frozenset(PORTAL_RESET_FIELDS),
    _ALLOWED,
)
UNREADABLE = "Braces must pair: write {{ and }} for a literal brace."
UNKNOWN = "{{{name}}} is not a placeholder this template can use."
UNKNOWN_UNNAMED = "A placeholder in this text is not one this template can use."
NO_FORMAT = "Write {{{name}}} as it is: a placeholder takes no format or conversion."
#: How an unknown placeholder may be named back: a plain word of a reasonable length; any
#: other text in braces is refused without being repeated.
_NAMEABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")


def fields_text(fields: tuple[str, ...] = MERGE_FIELDS) -> str:
    return ", ".join(f"{{{name}}}" for name in fields)


def not_allowed(fields: tuple[str, ...] = MERGE_FIELDS) -> str:
    return "Only these placeholders are allowed, written exactly: " + fields_text(fields) + "."


def fields_for(code: str) -> tuple[str, ...]:
    """The placeholders a template with this code may use."""
    return _CODE_FIELDS.get(code, FIELDS)


#: The refusal :func:`render` raises.
NOT_ALLOWED = not_allowed(MERGE_FIELDS)


class TemplateRefused(ValueError):  # noqa: N818 - reads as the outcome
    """The text uses something other than the whitelisted merge fields."""


def problems(text: str, fields: tuple[str, ...] = MERGE_FIELDS) -> list[str]:
    """What ``text`` breaks, as messages for staff; empty when it is acceptable. Only
    ``fields`` may be named (every placeholder when not given). An unknown placeholder is
    named when it is a plain word (:data:`UNKNOWN`), never otherwise; no other part of the
    text is repeated."""
    allowed = frozenset(fields) & _ALLOWED
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError:
        return [UNREADABLE]
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if name not in allowed:
            if _NAMEABLE.fullmatch(name):
                return [UNKNOWN.format(name=name)]
            return [UNKNOWN_UNNAMED]
        if spec or conversion is not None:
            return [NO_FORMAT.format(name=name)]
    return []


class _WhitelistFormatter(string.Formatter):
    """A formatter that resolves only whitelisted names in a mapping of strings."""

    def get_field(self, field_name: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Any:
        if field_name not in _ALLOWED:
            raise TemplateRefused(NOT_ALLOWED)
        return kwargs[field_name], field_name

    def get_value(self, key: int | str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Any:
        if not isinstance(key, str) or key not in _ALLOWED:
            raise TemplateRefused(NOT_ALLOWED)
        return kwargs[key]

    def convert_field(self, value: Any, conversion: str | None) -> Any:
        if conversion is not None:
            raise TemplateRefused(NOT_ALLOWED)
        return value

    def format_field(self, value: Any, format_spec: str) -> Any:
        if format_spec or not isinstance(value, str):
            raise TemplateRefused(NOT_ALLOWED)
        return value


def render(text: str, values: Mapping[str, str]) -> str:
    """``text`` with each merge field replaced by its value; :class:`TemplateRefused`
    when the text breaks a rule, names a field ``values`` has not, or the values are
    not exactly one code's fields (or every field), each a string."""
    if frozenset(values) not in _VALUE_SETS:
        raise TemplateRefused(NOT_ALLOWED)
    if problems(text, tuple(values)):
        raise TemplateRefused(NOT_ALLOWED)
    if not all(isinstance(value, str) for value in values.values()):
        raise TemplateRefused(NOT_ALLOWED)
    try:
        return _WhitelistFormatter().vformat(text, (), dict(values))
    except TemplateRefused:
        raise
    except (KeyError, IndexError, AttributeError, ValueError) as error:
        raise TemplateRefused(NOT_ALLOWED) from error
