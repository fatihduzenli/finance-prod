"""The monthly rent run screen (PLAN-007 §4.1, 7b).

``run:today`` lists the debits due today with last month's outcome, the leases due
today that are skipped and why, the insurance due in the next seven days, and a banner
when the payment provider may not be called (breaker open, failure budget exhausted).
It reads only: no provider call and no write.

``run:create_charges`` and ``run:match`` need ``billing`` (D10; checked by
``accounts.registry`` and again here). Both end in a redirect. Creating charges acts on
the server's today, never a date from the request, with the outcome as messages; it
also catches up the debit dates since each lease's start that have no charge
(PLAN-012b-b D1), counted as "Created (catch-up)". Each run, the button's and the daily
tick's, writes a ``rent_run.ticked`` audit row; the page lists the latest one's created
charges after the buttons (PLAN-012d D11).
"Match invoices" reads the provider, so it is queued as a ``run_match`` job for the
worker (PLAN-010 D3); the page shows the latest one and its report lines.

``run:export`` (PLAN-011 D5) is the debits due today as CSV, any role.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.db.models import prefetch_related_objects
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from finance.dates import NEW_YORK
from ledger.models import Lease, PaymentChannel, RentCharge, RentChargeStatus
from ledger.services import autopay_plans, insurance, rent_run, stores
from ledger.services.csv_export import CsvWriter
from ledger.views.jobs import busy, job_context, submit
from ledger.views.operators import _actor, _role, csv_response, export_name, role_required
from ledger.wording import provider_sentence
from payments import jobs
from payments.health import provider_health
from payments.models import Job, JobKind, JobStatus, WebhookEvent, WebhookOutcome

#: Most report lines shown after a run; the rest are counted.
MAX_LINES = 20
MATCH_QUEUED = provider_sentence(
    "Updating from {provider}; the report appears below when it is done."
)


def _match_context() -> dict[str, Any]:
    """The latest "Match invoices" job: its state, and its stored report lines once it
    finished (the first line goes with the state)."""
    context = job_context(jobs.RENT_RUN)
    job = context["job"]
    finished = job is not None and job.status in (JobStatus.DONE, JobStatus.FAILED)
    lines = context["job_text"].splitlines() if finished else []
    if lines:
        context["job_text"] = lines[0]
    context["match_lines"] = lines[1:]
    return context


# --- The page's frame (DESIGN-SPEC §4 "Daily charges", PLAN-012f-b E-2) ----------------

#: The daily run's schedule: infra/modules/containerAppJobDaily.bicep fires at 13:00 UTC
#: (09:00 New York in daylight time, 08:00 in standard time).
DAILY_RUN_UTC = time(13, 0)

#: The "Due today" card's filter (``?due=``, read-only): value and label.
DUE_FILTERS = (
    ("all", "All"),
    ("debit", "Bank debit"),
    ("email", "E-mail"),
    ("catchup", "Catch-up"),
)

#: A row's status badge (spec: Created teal, Large debit red, Debit tonight yellow,
#: Skipped grey); ``OWN`` is a charge already past "due", shown with its own status.
CREATED = "created"
LARGE = "large"
TONIGHT = "tonight"
SKIPPED = "skipped"
NOT_CREATED_STATUS = "not_created"
OWN = "own"

#: The result strip's colour: green, yellow (a lease skipped), red (the update failed).
STRIP_OK = "ok"
STRIP_WARN = "warn"
STRIP_ERROR = "error"
STRIP_NONE = "none"


@dataclass(frozen=True, slots=True)
class DueRow:
    """One row of the "Due today" card: a debit due today, a charge the latest run of
    today caught up, or a lease due today that is skipped."""

    lease: Lease
    title: str
    status: str
    debit_day: int | None = None
    seq: int = 1
    #: The caught-up charge's due date; ``None`` on the other rows.
    catch_up_on: date | None = None
    amount: Decimal | None = None
    carried: Decimal | None = None
    charge: RentCharge | None = None
    over_limit: bool = False
    reason: str = ""

    @property
    def by_debit(self) -> bool:
        return bool(self.lease.payment_channel == PaymentChannel.PROVIDER_ACH)

    @property
    def catch_up(self) -> bool:
        return self.catch_up_on is not None


def _due_status(item: rent_run.DueDebit) -> str:
    still_due = item.charge is None or item.charge.status == RentChargeStatus.DUE
    if item.large and still_due:
        return LARGE
    if item.charge is None:
        return NOT_CREATED_STATUS
    if item.charge.status != RentChargeStatus.DUE:
        return OWN
    return TONIGHT if item.lease.payment_channel == PaymentChannel.PROVIDER_ACH else CREATED


def _caught_up_today(last_run: rent_run.RunRecord | None, today: date) -> list[RentCharge]:
    """The charges the latest run caught up, when that run was today's (their operators in
    one query)."""
    if last_run is None or last_run.day != today:
        return []
    charges = [item.charge for item in last_run.charges if item.when == rent_run.WHEN_CATCH_UP]
    prefetch_related_objects([charge.lease for charge in charges], "operator")
    return charges


def _due_rows(run: rent_run.RunDay, caught_up: list[RentCharge]) -> list[DueRow]:
    rows = [
        DueRow(
            lease=item.lease,
            title=stores.title(item.lease.store),
            status=_due_status(item),
            debit_day=item.debit_day,
            seq=item.seq,
            amount=item.amount,
            carried=item.carried_balance,
            charge=item.charge,
            over_limit=item.over_limit,
        )
        for item in run.due
    ]
    rows += [
        DueRow(
            lease=charge.lease,
            title=stores.title(charge.lease.store),
            status=CREATED if charge.status == RentChargeStatus.DUE else OWN,
            seq=charge.seq,
            catch_up_on=charge.due_on,
            amount=charge.amount_due,
            charge=charge,
        )
        for charge in caught_up
    ]
    rows += [
        DueRow(
            lease=item.lease,
            title=stores.title(item.lease.store),
            status=SKIPPED,
            reason=item.reason,
        )
        for item in run.skipped
    ]
    return rows


def _due_filter(request: HttpRequest) -> str:
    value = request.GET.get("due", "")
    return value if value in dict(DUE_FILTERS) else "all"


def _filtered(rows: list[DueRow], chosen: str) -> list[DueRow]:
    if chosen == "debit":
        return [row for row in rows if row.by_debit]
    if chosen == "email":
        return [row for row in rows if not row.by_debit]
    if chosen == "catchup":
        return [row for row in rows if row.catch_up]
    return rows


def _figures(run: rent_run.RunDay, caught_up: list[RentCharge]) -> dict[str, Any]:
    """The four KPI cards, from the debits due today and today's catch-up."""
    debits = sum(1 for item in run.due if item.lease.payment_channel == PaymentChannel.PROVIDER_ACH)
    created = sum(1 for item in run.due if item.charge is not None)
    return {
        "created": created + len(caught_up),
        "created_today": created,
        "caught_up": len(caught_up),
        "due_amount": sum((item.amount for item in run.due), Decimal(0)),
        "debits": debits,
        "others": len(run.due) - debits,
        "skipped": len(run.skipped),
        "reasons": list(dict.fromkeys(item.reason.rstrip(".") for item in run.skipped)),
        "large": sum(1 for item in run.due if item.large),
    }


def _strip(last_run: rent_run.RunRecord | None, job: Job | None) -> str:
    """Red when the latest "Update from …" failed, yellow when the latest run skipped a
    lease, green after a run, grey before the first."""
    if job is not None and job.status == JobStatus.FAILED:
        return STRIP_ERROR
    if last_run is None:
        return STRIP_NONE
    return STRIP_WARN if last_run.skipped else STRIP_OK


def _next_run(now: datetime) -> tuple[str, str]:
    """When the daily run fires next: ("today" or "tomorrow", "09:00" New York)."""
    utc = now.astimezone(UTC)
    at = datetime.combine(utc.date(), DAILY_RUN_UTC, tzinfo=UTC)
    if at <= utc:
        at += timedelta(days=1)
    local = at.astimezone(NEW_YORK)
    word = "today" if local.date() == now.astimezone(NEW_YORK).date() else "tomorrow"
    return word, f"{local:%H:%M}"


def _clock(moment: datetime) -> str:
    return f"{moment.astimezone(NEW_YORK):%H:%M}"


@role_required
@require_safe
def run_today(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    match = _match_context()
    run = rent_run.leases_due(today)
    last_run = rent_run.last_run()
    caught_up = _caught_up_today(last_run, today)
    rows = _due_rows(run, caught_up)
    chosen = _due_filter(request)
    next_word, next_clock = _next_run(timezone.now())
    return render(
        request,
        "ledger/run/today.html",
        {
            **match,
            "run": run,
            "insurance_due": insurance.insurance_due_soon(today),
            "insurance_days": insurance.DUE_SOON_DAYS,
            "health": provider_health(),
            "threshold": settings.LARGE_DEBIT_WARNING_AMOUNT,
            "can_run": meets(_role(request), Role.BILLING),
            "last_run": last_run,
            "last_run_clock": _clock(last_run.at) if last_run is not None else "",
            # PLAN-012f-a D13: the autopay plans' debits the daily run created today.
            "autopay_charges": autopay_plans.made_on(today),
            "catch_up_months": rent_run.CATCH_UP_MONTHS,
            "dead_letters": WebhookEvent.objects.filter(outcome=WebhookOutcome.DEAD_LETTER).count(),
            # PLAN-012f-b E-2: the strip, the four figures, the filtered card, the last runs.
            "strip": _strip(last_run, match["job"]),
            "next_run": f"{next_word} {next_clock}",
            "run_figures": _figures(run, caught_up),
            "due_filters": DUE_FILTERS,
            "due_filter": chosen,
            "due_rows": _filtered(rows, chosen),
            "due_count": len(rows),
            "skipped_rows": [row for row in rows if row.status == SKIPPED],
            "recent_runs": [(record, _clock(record.at)) for record in rent_run.recent_runs()],
        },
    )


EXPORT_HEADER = (
    "Store",
    "Store number",
    "Operator",
    "Debit of the month",
    "Debit day",
    "Channel",
    "Amount (USD)",
    "Carried balance (USD)",
    "Status",
    "Last month status",
    "Last month balance (USD)",
)
NOT_CREATED = "Not created"


@role_required
@require_safe
def run_export(request: HttpRequest) -> StreamingHttpResponse:
    """``ledger:run:export`` (PLAN-011 D5): the debits due today, as the page lists them."""
    run = rent_run.leases_due(timezone.localdate())
    records = (
        [
            item.lease.store.external_label,
            item.lease.store.store_number,
            item.lease.operator.display_name if item.lease.operator is not None else "",
            item.seq,
            item.debit_day,
            item.lease.get_payment_channel_display(),
            item.amount,
            item.carried_balance,
            item.charge.get_status_display() if item.charge is not None else NOT_CREATED,
            item.last_month.get_status_display() if item.last_month is not None else "",
            item.last_month.balance if item.last_month is not None else None,
        ]
        for item in run.due
    )
    return csv_response(CsvWriter(EXPORT_HEADER).lines(records), export_name("rent-run"))


def _billing_only(request: HttpRequest) -> HttpResponse | None:
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    return None


@role_required
@require_POST
def run_create_charges(request: HttpRequest) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    today = timezone.localdate()
    actor = _actor(request)
    report = rent_run.create_charges(today, actor=actor)
    rent_run.record_run(report, today, actor)
    messages.success(
        request,
        f"Charges created: {len(report.created)}. "
        f"Created (catch-up): {len(report.caught_up)}. "
        f"Already created: {len(report.already)}. "
        f"Skipped leases: {len(report.skipped)}.",
    )
    for item in report.skipped[:MAX_LINES]:
        messages.warning(request, f"Skipped {item.lease.store.external_label}: {item.reason}")
    return redirect("ledger:run:today")


@role_required
@require_POST
def run_match(request: HttpRequest) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    health = provider_health()
    if not health.ok:
        for text in health.messages:
            messages.error(request, text)
        return redirect("ledger:run:today")
    if not busy(request, jobs.RENT_RUN) and submit(request, JobKind.RUN_MATCH, jobs.RENT_RUN):
        messages.info(request, MATCH_QUEUED)
    return redirect("ledger:run:today")
