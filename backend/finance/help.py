"""The help pages (docs/qa/PLAN-012e2-fewer-actions.md D10).

``/help/`` lists the topics and ``/help/<slug>/`` shows one; a slug that is not a topic
is 404. The pages are static: a template per topic under ``finance/help/``, no model, no
form, nothing read from the database beyond what the shell reads for every page (the
company's names). They take the paragraphs the pages lost to the one-line rule
(``tests/test_one_line_help.py``): a page keeps at most one line of help under a block
and links here ("Learn more"); the user menu links the index.

Access: a signed-in, verified user with any role (the middleware answers 401 without a
session; :func:`~ledger.views.operators.role_required` 403 without a role). Both routes
are read-only in ``accounts.registry``.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe

from ledger.services import invoice_builder, rent_run
from ledger.views.operators import role_required


@dataclass(frozen=True, slots=True)
class Topic:
    slug: str
    title: str

    @property
    def template(self) -> str:
        return f"finance/help/{self.slug}.html"


#: The topics, in the index's order.
TOPICS: tuple[Topic, ...] = (
    Topic("store-costs", "Store costs and margin"),
    Topic("collecting-an-invoice", "How an invoice is collected"),
    Topic("invoice-lines", "The lines of an invoice"),
    Topic("autopay-and-daily-charges", "Autopay and the daily charges"),
    Topic("needs-review", "Needs review"),
    Topic("promises-to-pay", "Promises to pay"),
    Topic("lease-ends-and-transfers", "Lease ends and transfers"),
)
_BY_SLUG: dict[str, Topic] = {topic.slug: topic for topic in TOPICS}


@role_required
@require_safe
def index(request: HttpRequest) -> HttpResponse:
    """``finance:help``: the list of topics."""
    return render(request, "finance/help/index.html", {"topics": TOPICS})


@role_required
@require_safe
def topic(request: HttpRequest, slug: str) -> HttpResponse:
    """``finance:help_topic``: one topic; 404 for any other slug."""
    found = _BY_SLUG.get(slug)
    if found is None:
        raise Http404
    return render(
        request,
        "finance/help/topic.html",
        {
            "topic": found,
            "topics": TOPICS,
            # Constants of the code and the settings, never a row.
            "catch_up_months": rent_run.CATCH_UP_MONTHS,
            "large_debit_threshold": settings.LARGE_DEBIT_WARNING_AMOUNT,
            "schedule_horizon_days": invoice_builder.SCHEDULE_HORIZON_DAYS,
        },
    )
