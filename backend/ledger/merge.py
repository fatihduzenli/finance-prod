"""Merge fields of the collection e-mail templates (docs/qa/PLAN-011b D4).

A template's subject and body may name exactly the fields of :data:`MERGE_FIELDS`, each
as ``{name}``; ``{{`` and ``}}`` write a literal brace. Nothing else is accepted: no
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

MERGE_FIELDS: tuple[str, ...] = (
    "operator",
    "store",
    "amount",
    "due_date",
    "days_late",
    "statement_link",
    "company",
)
_ALLOWED = frozenset(MERGE_FIELDS)
FIELDS_TEXT = ", ".join(f"{{{name}}}" for name in MERGE_FIELDS)
UNREADABLE = "Braces must pair: write {{ and }} for a literal brace."
NOT_ALLOWED = "Only these merge fields are allowed, written exactly: " + FIELDS_TEXT + "."


class TemplateRefused(ValueError):  # noqa: N818 - reads as the outcome
    """The text uses something other than the whitelisted merge fields."""


def problems(text: str) -> list[str]:
    """What ``text`` breaks, as messages for staff; empty when it is acceptable."""
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError:
        return [UNREADABLE]
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if name not in _ALLOWED or spec or conversion is not None:
            return [NOT_ALLOWED]
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
    when the text breaks a rule or a value is missing or not a string."""
    if problems(text):
        raise TemplateRefused(NOT_ALLOWED)
    if set(values) != _ALLOWED or not all(isinstance(value, str) for value in values.values()):
        raise TemplateRefused(NOT_ALLOWED)
    try:
        return _WhitelistFormatter().vformat(text, (), dict(values))
    except TemplateRefused:
        raise
    except (KeyError, IndexError, AttributeError, ValueError) as error:
        raise TemplateRefused(NOT_ALLOWED) from error
