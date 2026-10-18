"""What the screens call things (docs/qa/PLAN-012c-design-pass.md D9, copy list §4).

**Amounts.** :func:`format_amount` is the one way an amount is written for a person to read:
``$2,000.00``, a thousands separator and two decimals, a leading minus for a negative
(``-$2,000.00``, never ``$-``), and a negative that rounds to zero written as ``$0.00``
(§4.8). The template filter ``money`` (``finance/templatetags/money.py``) is this function.
CSV exports keep plain numbers (``ledger.services.csv_export``).

**The processor's name.** The app never names its payment processor in code (docs/CLAUDE.md
Rule 8): a sentence that names it carries :data:`PROVIDER` where the name goes, and the name
is the company settings row's (``CompanySettings.provider_name``, seeded from
``ledger.company_defaults``), so it follows a change of processor. :func:`provider_name`
reads it from the request's row when a request is being answered
(``finance.middleware.ProviderNameMiddleware`` points :func:`reading_from` at it, so the
shell and the sentences share one read) and with one query otherwise (a job, a management
command). :func:`with_provider` fills the name in at once; :func:`provider_sentence` is the
same, lazily, for module-level sentence constants: the name is read when the sentence is
shown, compared or formatted, never at import.

**Choice labels.** A stored choice's on-screen label is its ``TextChoices`` label (copy list
§4.5), read through ``get_<field>_display`` or the class's ``choices``; a label never names
the processor (it is fixed at import), so a page that needs the name writes
``company.provider_name`` next to it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

from django.utils.functional import lazy

#: Where a sentence names the payment processor.
PROVIDER = "{provider}"

_reader: ContextVar[Callable[[], str] | None] = ContextVar("provider_name_reader", default=None)


def format_amount(amount: Decimal) -> str:
    """``$1,234.56``; ``-$1,234.56`` for a negative; ``$0.00`` for a negative that rounds
    to zero."""
    digits = f"{abs(amount):,.2f}"
    if amount < 0 and digits != "0.00":
        return f"-${digits}"
    return f"${digits}"


def provider_name() -> str:
    """The processor's display name from the company settings row."""
    reader = _reader.get()
    if reader is not None:
        return reader()
    from ledger.models import CompanySettings

    return CompanySettings.read().provider_name


@contextmanager
def reading_from(reader: Callable[[], str]) -> Iterator[None]:
    """:func:`provider_name` asks ``reader`` while the block runs (one request)."""
    token = _reader.set(reader)
    try:
        yield
    finally:
        _reader.reset(token)


def with_provider(text: str) -> str:
    """``text`` with the processor's name for each :data:`PROVIDER`. When the rest of
    ``text`` still has ``{field}`` places for a later ``str.format``, braces in the name are
    doubled so that the format call writes them as typed."""
    if PROVIDER not in text:
        return text
    name = provider_name()
    if "{" in text.replace(PROVIDER, ""):
        name = name.replace("{", "{{").replace("}", "}}")
    return text.replace(PROVIDER, name)


#: :func:`with_provider`, evaluated when the sentence is used, not when it is defined.
provider_sentence = lazy(with_provider, str)
