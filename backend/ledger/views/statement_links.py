"""The operator's own statement page behind a signed link (docs/qa/PLAN-011b D4).

- ``ledger:statement_link``: ``/statement/<token>/``, public (the signed token is the
  credential, :mod:`ledger.services.statement_links`), GET and HEAD only. A bad, expired
  or revoked token answers the same empty 404, marked as logged so that Django's request
  log never writes the path with the token; the view's own log line names the path as
  :data:`LOGGED_PATH`. At most :data:`STATEMENT_LIMIT` requests a minute per client
  address (:mod:`finance.ratelimit`), then 429. The page is read-only and standalone: the
  open charges, the last twelve months of payments and the totals, with no bank data, no
  internal note and no link into the app; ``Cache-Control: private, no-store``. Every
  open is audited with the operator's id only.
- ``ledger:operators:statement_links_revoke`` (billing, POST): refuse every link sent so far.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from finance.ratelimit import SlidingWindowLimiter
from ledger.services import detail_cards, statement_links
from ledger.views.calendar import client_address
from ledger.views.operators import _actor, _load, role_required
from ledger.views.run import _billing_only

#: Requests a minute per client address on the page.
STATEMENT_LIMIT = 30
STATEMENT_WINDOW_SECONDS = 60
STATEMENT_LIMITER = SlidingWindowLimiter(STATEMENT_LIMIT, STATEMENT_WINDOW_SECONDS)
#: How the route appears in log lines: never with the token.
LOGGED_PATH = "/statement/[token]/"
TEMPLATE = "ledger/reports/statement_public.html"

logger = logging.getLogger(__name__)


def _refused(reason: str, status: int = 404) -> HttpResponse:
    logger.warning("Statement link refused (%s): path=%s", reason, LOGGED_PATH)
    response = HttpResponseNotFound() if status == 404 else HttpResponse(status=status)
    if status == 429:
        response["Retry-After"] = str(int(STATEMENT_WINDOW_SECONDS))
    response["Cache-Control"] = "private, no-store"
    # Django's request logger skips a response marked as logged; its line would carry
    # the real path, token included.
    response._has_been_logged = True  # type: ignore[attr-defined]
    return response


def statement_link_page(request: HttpRequest, token: str) -> HttpResponse:
    """``ledger:statement_link``: the operator's statement, read only."""
    if request.method not in ("GET", "HEAD"):
        return _refused("method")
    if not STATEMENT_LIMITER.allow(client_address(request)):
        return _refused("rate limit", 429)
    operator = statement_links.operator_for(token)
    if operator is None:
        return _refused("token")
    today = timezone.localdate()
    report = statement_links.statement(operator, today)
    statement_links.record_open(operator)
    response = render(
        request,
        TEMPLATE,
        {
            "operator": operator,
            "report": report,
            # PLAN-014 C6: the company each store's lease bills.
            "bill_to": detail_cards.lease_accounts(operator),
            "company": settings.COMPANY_NAME,
            "months": statement_links.HISTORY_MONTHS,
        },
    )
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@role_required
@require_POST
def statement_links_revoke(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:operators:statement_links_revoke`` (billing)."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    operator = _load(pk)
    outcome = statement_links.revoke(operator.pk, actor=_actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(
        f"{reverse('ledger:operators:detail', kwargs={'pk': operator.pk})}#statement-links"
    )
