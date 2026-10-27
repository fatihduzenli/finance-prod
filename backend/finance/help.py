"""The help pages (docs/qa/PLAN-012e2-fewer-actions.md D10).

``/help/`` lists the topics and ``/help/<slug>/`` shows one; a slug that is not a topic
is 404. The pages are static: a template per topic under ``finance/help/``, no model, no
form, nothing read from the database beyond what the shell reads for every page (the
company's names). They take the paragraphs the pages lost to the one-line rule
(``tests/test_one_line_help.py``): a page keeps at most one line of help under a block
and links here ("Learn more"); the user menu links the index.

The frame (PLAN-012f-b E-3, DESIGN-SPEC §4 Help): the header holds the title only; a 240 px
topic card (a search field that filters the list in the browser, ``finance/help_filter.js``,
then the topics in the spec's order) beside one article card: the topic's title, "Updated
{date} · {n} min read" (the date a constant of the topic, the minutes its words ÷ 200, at
least one), the body, and "Still stuck? E-mail {support e-mail}" when the company settings
name one. No "Was this helpful?" (it needs a backend; plan D17).

Access: a signed-in, verified user with any role (the middleware answers 401 without a
session; :func:`~ledger.views.operators.role_required` 403 without a role). Both routes
are read-only in ``accounts.registry``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.views.decorators.http import require_safe

from finance.context_processors import company_settings
from ledger.services import invoice_builder, rent_run
from ledger.views.operators import role_required


@dataclass(frozen=True, slots=True)
class Topic:
    slug: str
    title: str
    #: The day the topic's text last changed: shown as "Updated …" (update it with the text).
    updated: date

    @property
    def template(self) -> str:
        return f"finance/help/{self.slug}.html"


#: The topics, in the spec's order (DESIGN-SPEC §4 Help: stores, invoices, payments and
#: autopay, promises, lease ends, needs review); the spec's "Getting around" and "Reports" have
#: no topic yet.
TOPICS: tuple[Topic, ...] = (
    Topic("store-costs", "Store costs and margin", date(2026, 10, 26)),
    Topic("collecting-an-invoice", "How an invoice is collected", date(2026, 10, 25)),
    Topic("payments", "Payments", date(2026, 10, 27)),
    Topic("invoice-lines", "The lines of an invoice", date(2026, 10, 22)),
    Topic("autopay-and-daily-charges", "Autopay and the daily charges", date(2026, 10, 24)),
    Topic("promises-to-pay", "Promises to pay", date(2026, 10, 22)),
    Topic("lease-ends-and-transfers", "Lease ends and transfers", date(2026, 10, 22)),
    Topic("needs-review", "Needs review", date(2026, 10, 22)),
)
_BY_SLUG: dict[str, Topic] = {topic.slug: topic for topic in TOPICS}
#: Reading speed of the "n min read" line, in words a minute.
WORDS_PER_MINUTE = 200


def read_minutes(html: str) -> int:
    """The minutes a topic's body takes to read: its words ÷ :data:`WORDS_PER_MINUTE`,
    rounded up, at least one. A space before each tag keeps the words of adjacent elements
    (``<dt>Save</dt><dd>Saves``) apart."""
    words = len(strip_tags(html.replace("<", " <")).split())
    return max(1, math.ceil(words / WORDS_PER_MINUTE))


def _frame(current: Topic | None) -> dict[str, object]:
    """What the help frame's topic card reads on both pages: the topics and the open one
    (``None`` on the index)."""
    return {"topics": TOPICS, "current": current}


@role_required
@require_safe
def index(request: HttpRequest) -> HttpResponse:
    """``finance:help``: the list of topics."""
    return render(request, "finance/help/index.html", _frame(None))


@role_required
@require_safe
def topic(request: HttpRequest, slug: str) -> HttpResponse:
    """``finance:help_topic``: one topic; 404 for any other slug."""
    found = _BY_SLUG.get(slug)
    if found is None:
        raise Http404
    body = render_to_string(
        found.template,
        {
            # Constants of the code and the settings, never a row.
            "catch_up_months": rent_run.CATCH_UP_MONTHS,
            "large_debit_threshold": settings.LARGE_DEBIT_WARNING_AMOUNT,
            "schedule_horizon_days": invoice_builder.SCHEDULE_HORIZON_DAYS,
        },
        request,
    )
    return render(
        request,
        "finance/help/topic.html",
        {
            **_frame(found),
            "topic": found,
            "body": body,
            "minutes": read_minutes(body),
            # The footer's address ("" hides it): the request's own company settings row
            # (the shell reads it anyway), no query of its own.
            "support_email": company_settings(request).support_email,
        },
    )
