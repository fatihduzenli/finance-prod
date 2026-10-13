"""Merge fields of the e-mail templates (docs/qa/PLAN-011b D4, PLAN-012e D8).

A template's subject and body may name exactly the fields of its code's list, each as
``{name}``: :data:`LADDER_FIELDS` for the collection e-mails, :data:`FINAL_STATEMENT_FIELDS`
for the template ``final_statement`` (:func:`fields_for`); :data:`MERGE_FIELDS` is every
field of either list. ``{{`` and ``}}`` write a literal brace. Nothing else is accepted: no
attribute or index access (``{operator.__dict__}``, ``{operator[0]}``), no conversion
(``{operator!r}``), no format specification (``{amount:>99999}``), no positional or
unknown field (``{}``, ``{0}``, ``{__class__}``). :func:`problems` lists what a text
breaks, for the admin's validation on save; :func:`render` refuses the same texts with
:class:`TemplateRefused` and substitutes values from a mapping of plain strings only.
Rendering is :class:`string.Formatter` with every lookup, conversion and specification
overridden to accept nothing beyond a whitelisted name: Django templates are never used.
"""

from __future__ import annotations

import string
from collections.abc import Mapping, Sequence
from typing import Any

LADDER_FIELDS: tuple[str, ...] = (
    "operator",
    "store",
    "amount",
    "due_date",
    "days_late",
    "statement_link",
    "company",
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
MERGE_FIELDS: tuple[str, ...] = (
    *LADDER_FIELDS,
    *(name for name in FINAL_STATEMENT_FIELDS if name not in LADDER_FIELDS),
)
_ALLOWED = frozenset(MERGE_FIELDS)
#: The exact sets of values :func:`render` accepts.
_VALUE_SETS = (frozenset(LADDER_FIELDS), frozenset(FINAL_STATEMENT_FIELDS), _ALLOWED)
UNREADABLE = "Braces must pair: write {{ and }} for a literal brace."


def fields_text(fields: tuple[str, ...] = MERGE_FIELDS) -> str:
    return ", ".join(f"{{{name}}}" for name in fields)


def not_allowed(fields: tuple[str, ...] = MERGE_FIELDS) -> str:
    return "Only these merge fields are allowed, written exactly: " + fields_text(fields) + "."


def fields_for(code: str) -> tuple[str, ...]:
    """The merge fields a template with this code may use."""
    return FINAL_STATEMENT_FIELDS if code == FINAL_STATEMENT_CODE else LADDER_FIELDS


#: The collection e-mails' fields and refusal, as the admin shows them.
FIELDS_TEXT = fields_text(LADDER_FIELDS)
NOT_ALLOWED = not_allowed(LADDER_FIELDS)


class TemplateRefused(ValueError):  # noqa: N818 - reads as the outcome
    """The text uses something other than the whitelisted merge fields."""


def problems(text: str, fields: tuple[str, ...] = MERGE_FIELDS) -> list[str]:
    """What ``text`` breaks, as messages for staff; empty when it is acceptable. Only
    ``fields`` may be named (every merge field when not given)."""
    allowed = frozenset(fields) & _ALLOWED
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError:
        return [UNREADABLE]
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if name not in allowed or spec or conversion is not None:
            return [not_allowed(fields)]
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
