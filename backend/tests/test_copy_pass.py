"""PLAN-012c Batch D (docs/qa/PLAN-012c-design-pass.md D9, copy list §4, test plan §6 D): the
approved copy list, no technical term on screen, money formatted everywhere.

- The list (§4.2 to §4.7): each pair is checked in the template (or the Python module) that
  holds it, the old text absent and the new one present, and on the rendered page where a
  simple state shows it.
- The forbidden words of §4.1 are looked for in the visible text (text nodes, ``title``,
  ``aria-label`` and ``placeholder``) of the main pages, rendered with data.
- Money (§4.8): the ``money`` filter's cases, ``format_amount``, a grep of the templates for a
  bare ``${{`` and of the Python code for a ``"${`` or a ``:,.2f`` outside
  ``ledger.wording``, the grid cells, the dashboard figures, the promise cells and the
  "Closed … as …" sentence, the morning e-mail's amount.
- The choice labels shown (§4.5) with the stored values unchanged; the processor's name from
  the company settings row, read once per request; the sandbox banner; CSV headers unchanged.
- Carry-over (a) of Batches B and C: the transfer's incoming step has the lease form's
  "Debited twice a month" tick.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from html import unescape
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.roles import Role
from finance.templatetags.money import money
from ledger import wording
from ledger.forms import SHOWN_WHEN, TWICE_MONTHLY_ID, LinkForm, TransferIncomingForm
from ledger.models import (
    CollectionMethod,
    CollectionSchedule,
    CompanySettings,
    DisputeEvidenceStatus,
    InstallmentCollection,
    MessageStatus,
    MoneyMethod,
    OneOffCategory,
    PaymentChannel,
    PaymentMethod,
    RentSource,
    ScheduleKind,
    ScheduleState,
    SyncFailure,
    SyncStatus,
)
from ledger.services import audit, calendar, contacts, grid, leases, workspace
from payments.models import Job, JobKind, JobStatus, MismatchKind, WebhookEvent, WebhookOutcome
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_operator,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

BACKEND = Path(settings.BASE_DIR)
TEMPLATES = BACKEND / "ledger" / "templates" / "ledger"
BASE = BACKEND / "finance" / "templates" / "finance"
SEPTEMBER = date(2026, 9, 1)
P = "{{ company.provider_name }}"
BANNER = "SANDBOX — test system, no real money"


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def source(name: str) -> str:
    """A template's text without its ``{% comment %}`` blocks (they may quote old wording)."""
    path = BASE / name.removeprefix("finance/") if name.startswith("finance/") else TEMPLATES / name
    return re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", path.read_text(), flags=re.S)


# --- money (§4.8) -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (0, "$0.00"),
        (Decimal("0"), "$0.00"),
        (0.5, "$0.50"),
        (Decimal("0.5"), "$0.50"),
        (Decimal("1234.5"), "$1,234.50"),
        (1000000, "$1,000,000.00"),
        (Decimal("2000.00"), "$2,000.00"),
        (Decimal("-2000"), "-$2,000.00"),
        (Decimal("-0.5"), "-$0.50"),
        (Decimal("-0.004"), "$0.00"),
        ("1234.56", "$1,234.56"),
        (None, ""),
        ("", ""),
        ("not a number", ""),
    ],
)
def test_the_money_filter(value: object, shown: str) -> None:
    assert money(value) == shown


def test_format_amount_is_the_filter_and_never_writes_dollar_minus() -> None:
    assert wording.format_amount(Decimal("-1234.5")) == "-$1,234.50"
    assert wording.format_amount(Decimal("-0.00")) == "$0.00"
    assert wording.format_amount(Decimal("999999.99")) == "$999,999.99"
    # The old helpers are the one function now.
    assert vars(leases)["format_amount"] is wording.format_amount


_LOAD = re.compile(r"\{%\s*load\s+([^%]+)%\}")


def test_no_template_prints_a_bare_dollar_amount_and_every_money_user_loads_it() -> None:
    offenders = []
    for path in [*TEMPLATES.rglob("*.html"), *BASE.rglob("*.html")]:
        text = path.read_text()
        if re.search(r"\$\s*\{\{|\$\{%", text) or "floatformat" in text:
            offenders.append(f"{path.name}: bare amount")
        if "|money" in text and not any("money" in found.split() for found in _LOAD.findall(text)):
            offenders.append(f"{path.name}: |money without {{% load money %}}")
    assert offenders == []


def test_no_python_text_formats_an_amount_by_hand() -> None:
    """A sentence or markup built in Python writes its amounts with ``format_amount``: no
    ``"${`` nor ``:,.2f`` outside ``ledger/wording.py`` (CSV keeps plain numbers through
    ``csv_export.money``, which formats no ``$``)."""
    pattern = re.compile(r"\"\$\{|f\"[^\"\n]*\$\{|:,\.2f")
    offenders = []
    for package in ("ledger", "finance", "payments", "mail", "accounts"):
        for path in (BACKEND / package).rglob("*.py"):
            if "migrations" in path.parts or path.name == "wording.py":
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(BACKEND)}:{number}")
    assert offenders == []


def test_the_grid_cells_and_the_column_totals_carry_the_dollar_and_the_separators() -> None:
    lease = a_lease(a_store("D9-1"), an_operator("Grid Co"), channel=PaymentChannel.WIRE)
    charge = a_rent_charge(lease, SEPTEMBER, status="paid", due="2000.00", paid="2000.00")
    a_payment(charge, "2000.00", date(2026, 9, 5))
    a_rent_charge(lease, date(2026, 8, 1), status="partial", due="3500.00", paid="1200.00")
    viewer, _ = client_for(Role.VIEWER)
    html = page(viewer, reverse("ledger:grid:page") + "?from=2026-08&to=2026-09")
    # PLAN-012f D6: the cell block's amount over its status line.
    assert '<span class="cell-amount">$2,000.00</span>' in html
    assert '<span class="cell-amount">$1,200.00 of $3,500.00</span>' in html
    assert "</span> Partly paid</span>" in html
    # The September column's collected total over its counts (PLAN-012f D9).
    assert '$2,000.00<span class="cell-subline column-counts">' in html
    assert "paid 2,000.00" not in html and "partial 1,200.00" not in html
    assert grid.CELL_STATUSES[4] == ("partial", "Partly paid")
    assert grid.CELL_STATUSES[6] == ("not_due", "Not yet due")
    assert ">Monthly rent overview</h1>" in html


def test_the_dashboard_figures_are_formatted() -> None:
    lease = a_lease(a_store("D9-2"), an_operator("Figures Co"), channel=PaymentChannel.WIRE)
    a_rent_charge(lease, SEPTEMBER, due="12345.60")
    viewer, _ = client_for(Role.VIEWER)
    figures = page(viewer, "/").split('id="figures-card"')[1].split("</section>")[0]
    # PLAN-012f D6: the figures are KPI cards ({% kpi %}).
    # PLAN-012f D7: Unpaid balances in the unpaid red.
    assert '<span class="kpi-value tone-unpaid">$12,345.60</span>' in figures
    assert '<span class="kpi-value">$0.00</span>' in figures  # paid this month


def test_the_promise_cells_and_the_closed_sentence_are_formatted() -> None:
    from tests.test_contact_log import closing, promise

    operator = an_operator("Promise Co")
    store = a_store("D9-3")
    lease = a_lease(store, operator)
    kept = promise(operator, lease=lease, by=date(2026, 10, 1), amount="2000.00")
    closer = closing(operator, kept, "kept", lease=lease, body="Paid.")
    open_one = promise(operator, lease=lease, by=date(2026, 10, 20), amount="12500.00")
    kept.refresh_from_db()
    assert contacts.closes_sentence(kept) == "Closed $2,000.00 by Oct 1, 2026 as Kept"
    # The CSV's form keeps ISO (DESIGN-SPEC §5).
    assert contacts.closes_csv_text(kept) == "Closed $2,000.00 by 2026-10-01 as Kept"
    client, _ = client_for(Role.BILLING)
    log = page(client, f"/operators/{operator.pk}/contacts/")
    assert "Closed $2,000.00 by Oct 1, 2026 as Kept" in log
    assert "$12,500.00 by Oct 20, 2026" in log
    header = page(client, f"/stores/{store.pk}/").split('id="workspace-header"')[1]
    header = header.split("</section>")[0]
    assert "$12,500.00 by Oct 20, 2026" in header  # the open promise
    assert '<dd id="header-rent"><strong>$3,500.00</strong> / month</dd>' in header  # 12f D8
    assert closer.pk and open_one.pk


def test_the_morning_e_mail_writes_the_promised_amount_with_its_separator() -> None:
    from tests.test_contact_log import promise

    operator = an_operator("Morning Co")
    row = promise(operator, by=date(2026, 10, 1), amount="2000.00")
    message = calendar._promise_message(row, "staff@example.com")
    assert "promised to pay $2,000.00 by Oct 1, 2026." in message.text
    assert message.subject == "Promise to pay due today: Morning Co"


# --- choice labels (§4.5): what the screen says, the stored value unchanged -------------


@pytest.mark.parametrize(
    ("choice_set", "value", "shown"),
    [
        (ScheduleState, ScheduleState.NONE, "Not set up"),
        (ScheduleState, ScheduleState.ACTIVE, "On"),
        (ScheduleState, ScheduleState.PAUSED, "Paused"),
        (ScheduleState, ScheduleState.FAILED, "Failed"),
        (ScheduleState, ScheduleState.ENDED, "Off"),
        (SyncStatus, SyncStatus.NOT_APPLICABLE, "Not needed"),
        (SyncStatus, SyncStatus.AWAITING_LINK, "Waiting to connect"),
        (SyncStatus, SyncStatus.PENDING, "In progress"),
        (SyncStatus, SyncStatus.LINKED, "Connected"),
        (SyncStatus, SyncStatus.FAILED, "Failed"),
        (SyncFailure, SyncFailure.PROVIDER_ERROR, "Error"),
        (SyncFailure, SyncFailure.VALIDATION_FAILED, "Not sent"),
        (SyncFailure, SyncFailure.NOT_PERMITTED_HERE, "Not allowed here"),
        (SyncFailure, SyncFailure.FEATURE_DISABLED, "Turned off"),
        (SyncFailure, SyncFailure.CIRCUIT_OPEN, "Paused after repeated errors"),
        (SyncFailure, SyncFailure.FAILURE_BUDGET_EXHAUSTED, "Too many errors today"),
        (SyncFailure, SyncFailure.RATE_LIMIT_WAIT_EXCEEDED, "Busy, try later"),
        (SyncFailure, SyncFailure.PROVIDER_REJECTED, "Rejected"),
        (SyncFailure, SyncFailure.PROVIDER_UNAVAILABLE, "Unavailable"),
        (SyncFailure, SyncFailure.TRANSPORT_ERROR, "Could not be reached"),
        (SyncFailure, SyncFailure.IDENTITY_MISMATCH, "Wrong account"),
        (SyncFailure, SyncFailure.ALREADY_LINKED_ELSEWHERE, "Connected to another operator"),
        (PaymentMethod, PaymentMethod.ACH, "Bank debit (ACH)"),
        (MoneyMethod, MoneyMethod.ACH, "Bank debit (ACH)"),
        (PaymentChannel, PaymentChannel.PROVIDER_ACH, "Bank debit"),
        (OneOffCategory, OneOffCategory.NSF_FEE, "Returned payment (NSF) fee"),
        (InstallmentCollection, InstallmentCollection.EMAIL, "Send by e-mail"),
        (CollectionMethod, CollectionMethod.EMAIL, "Send by e-mail"),
        (JobStatus, JobStatus.QUEUED, "Waiting"),
        (JobStatus, JobStatus.RUNNING, "In progress"),
        (JobStatus, JobStatus.DONE, "Done"),
        (JobStatus, JobStatus.FAILED, "Failed"),
        (JobStatus, JobStatus.DEAD, "Gave up"),
        (WebhookOutcome, WebhookOutcome.DEAD_LETTER, "Gave up"),
        (WebhookOutcome, WebhookOutcome.PROCESSED, "Processed"),
        (MismatchKind, MismatchKind.DEPOSIT_UNMATCHED, "Bank deposit not matched"),
        (MismatchKind, MismatchKind.RETURN_MISSED_BY_WEBHOOK, "Return not received"),
        (MismatchKind, MismatchKind.PENDING_TOO_OLD, "Pending too long"),
        (MessageStatus, MessageStatus.QUEUED, "Waiting"),
        (MessageStatus, MessageStatus.CATCH_ALL, "Sent to the test inbox"),
        (DisputeEvidenceStatus, DisputeEvidenceStatus.QUEUED, "Waiting"),
        (RentSource, RentSource.SHEET_CURRENT, "From the spreadsheet"),
        (RentSource, RentSource.SHEET_INFERRED, "From the spreadsheet (inferred)"),
        (RentSource, RentSource.STAFF, "Entered by staff"),
        (JobKind, JobKind.LEASE_SYNC, "Updating the lease"),
        (JobKind, JobKind.LEASE_RETRY, "Updating the lease again"),
        (JobKind, JobKind.LEASE_LINK, "Connecting the lease"),
        (JobKind, JobKind.LEASE_METHODS_REFRESH, "Updating bank methods"),
        (JobKind, JobKind.LEASE_METHOD_ATTACH, "Connecting a bank account"),
        (JobKind, JobKind.LEASE_ACCOUNT_SWITCH, "Switching to the lease's own account"),
        (JobKind, JobKind.PAYMENT_MARK, "Marking the invoice paid"),
        (JobKind, JobKind.INVOICE_CREATE, "Creating the invoice"),
        (JobKind, JobKind.SCHEDULE_CREATE, "Setting up autopay"),
        (JobKind, JobKind.WEBHOOK_REPLAY, "Processing a message again"),
        (JobKind, JobKind.RECONCILE_RUN, "Running the nightly check"),
    ],
)
def test_the_label_shown_for_each_stored_value(choice_set: type, value: str, shown: str) -> None:
    assert choice_set(value).label == shown


def test_no_job_kind_label_names_the_machinery() -> None:
    for value in JobKind.values:
        shown = str(JobKind(value).label)
        assert not re.search(r"provider|sync|webhook|reconcil|schedule", shown, re.I), shown


def test_the_models_show_the_labels_and_store_the_values() -> None:
    lease = a_lease(a_store("D9-4"), an_operator("Label Co"))
    assert lease.payment_channel == "stax"
    assert lease.get_payment_channel_display() == "Bank debit"
    schedule = CollectionSchedule(lease=lease, kind=ScheduleKind.RENT, seq=1)
    assert schedule.state == "none" and schedule.get_state_display() == "Not set up"
    job = Job(kind=JobKind.LEASE_SYNC, status=JobStatus.QUEUED)
    assert (job.get_kind_display(), job.get_status_display()) == (
        "Updating the lease",
        "Waiting",
    )
    event = WebhookEvent(outcome="ignored: no handler")
    assert event.outcome_words == "Ignored: no handler"
    assert WebhookEvent(outcome="dead_letter").outcome_words == "Gave up"
    assert WebhookEvent(outcome="").outcome_words == ""
    assert WebhookEvent(outcome="something_new: x").outcome_words == "something_new: x"
    assert lease.operator is not None
    assert lease.operator.get_sync_failure_display() == ""
    # An unknown stored value is shown as stored, never an error.
    lease.operator.sync_failure = "SomethingNew"
    assert lease.operator.get_sync_failure_display() == "SomethingNew"


def test_the_forms_offer_the_labels_shown() -> None:
    from ledger.forms import LeaseForm

    channels = dict(LeaseForm().fields["payment_channel"].choices)  # type: ignore[attr-defined]
    assert channels["stax"] == "Bank debit"
    incoming = dict(TransferIncomingForm().fields["payment_channel"].choices)  # type: ignore[attr-defined]
    assert incoming["stax"] == "Bank debit" and "unknown" not in incoming
    form = LinkForm()
    assert str(form.fields["customer_id"].label) == "Stax customer ID"
    assert str(form.fields["payment_method_id"].label) == "Stax bank method ID"


# --- the processor's name from the company settings row ---------------------------------------


def _rename(name: str) -> None:
    row = CompanySettings.current()
    row.processor_name = name
    row.save()


def test_the_sentences_follow_the_company_settings_row() -> None:
    _rename("PayCo")
    assert str(leases.NO_PROVIDER_CUSTOMER) == "The operator is not connected to PayCo."
    lease = a_lease(a_store("D9-6"), an_operator("PayCo Label Co"))
    admin, _ = client_for(Role.ADMIN)
    assert "Bank debit through PayCo" in page(admin, f"/leases/{lease.pk}/")
    from ledger.services import installments

    assert installments.RELEASE_PENDING_INVOICE.format(number="I-7") == (
        "Waiting for PayCo to cancel invoice I-7."
    )


def test_a_processor_name_with_braces_survives_a_later_format() -> None:
    _rename("Pay{number}Co")
    from ledger.services import installments

    assert installments.RELEASE_PENDING_INVOICE.format(number="I-7") == (
        "Waiting for Pay{number}Co to cancel invoice I-7."
    )


def test_inside_a_request_the_name_is_read_with_the_shell_once() -> None:
    lease = a_lease(a_store("D9-5"), an_operator("Once Co"))
    admin, _ = client_for(Role.ADMIN)
    admin.get("/")  # warm the session
    with CaptureQueriesContext(connection) as queries:
        html = page(admin, f"/leases/{lease.pk}/")
    reads = [query for query in queries.captured_queries if "company_settings" in query["sql"]]
    assert len(reads) == 1, reads
    assert "Bank debit through Stax" in html


def test_outside_a_request_the_reader_can_be_given() -> None:
    with wording.reading_from(lambda: "Given"):
        with CaptureQueriesContext(connection) as queries:
            assert wording.provider_name() == "Given"
            assert str(leases.PROVIDER_REJECTED) == "Given refused the request."
    assert queries.captured_queries == []


# --- the approved list, template by template (§4.2 to §4.7) -----------------------------------

#: (template, old text, new text) as the copy list states them; each pair is checked in the
#: template that holds it. PLAN-014 C3: the link, retry and update wording of the operator
#: page moved, with the provider connection, to the lease's Edit billing details dialog.
PAIRS: list[tuple[str, str, str]] = [
    # §4.2 navigation and shell
    # PLAN-012f D11 (pending the owner): the sidebar item reads "Monthly rent", as the
    # screenshots write it; the page title stays "Monthly rent overview".
    ("finance/base.html", ">Grid</a>", "<span>Monthly rent</span></a>"),
    ("finance/base.html", ">Arrears</a>", ">Unpaid balances</a>"),
    ("finance/base.html", ">Returns</a>", ">Returned payments</a>"),
    # PLAN-012f D3: the Admin group's links are one partial, in the sidebar and the More sheet.
    ("finance/_admin_links.html", ">Rent run</a>", ">Daily charges</a>"),
    ("finance/_admin_links.html", ">Audit</a>", ">Activity log</a>"),
    ("finance/_admin_links.html", ">Webhooks</a>", f">Messages from {P}</a>"),
    ("finance/_admin_links.html", ">Reconciliation</a>", ">Nightly check</a>"),
    ("finance/_admin_links.html", ">Django admin</a>", ">System settings</a>"),
    ("finance/banner.html", "SANDBOX — NO REAL MONEY", BANNER),
    # §4.3 page titles
    # PLAN-012f-b C-2: one Add payment page for the rent and the one-off charge.
    ("payments/add_payment.html", "Record a payment", "Add payment"),
    ("stores/record_payment.html", "Record a payment", "Add payment"),
    ("deposits/resolve.html", "Resolve deposit", "Return deposit"),
    ("charges/link_invoice.html", "Link an invoice", f"Attach {P} invoice"),
    ("one_off_charges/link_invoice.html", "Link an invoice", f"Attach {P} invoice"),
    ("leases/enrol.html", "Autopay for", "Set up autopay"),
    ("leases/enrol.html", "Enable autopay", "Set up autopay"),
    ("leases/stop_autopay.html", "Stop autopay", "Turn off autopay"),
    ("leases/_billing_dialog.html", "Link payment provider IDs", f"Connect to {P}"),
    ("run/today.html", "Rent run", "Daily charges, "),
    ("arrears/page.html", "{% block title %}Arrears", "{% block title %}Unpaid balances"),
    ("grid/page.html", "{% block title %}Grid", "{% block title %}Monthly rent overview"),
    ("reports/aging.html", "A/R aging", "Unpaid by age"),
    ("reports/index.html", "A/R aging", "Unpaid by age"),
    ("reports/deposits.html", "Deposits versus transactions", "Bank account check"),
    ("reports/index.html", "Deposits versus transactions", "Bank account check"),
    ("reconciliation/page.html", "<h1>Reconciliation</h1>", "<h1>Nightly check</h1>"),
    ("webhooks/list.html", "Webhook events", f"Messages from {P}"),
    ("webhooks/detail.html", "Webhook event", f"Message from {P}: "),
    ("audit/page.html", "Audit log", "Activity log"),
    ("lease_ends/blockers.html", "live items", f"still open at {P}"),
    ("transfers/blockers.html", "live items and balances", f"still open at {P}, and balances"),
    ("charges/detail.html", "Charge {{ charge.lease", "Rent {{ charge.period_month|month }}"),
    ("payments/detail.html", "Payment of ${", "of {{ payment.amount|money }}"),
    ("invoices/cancel.html", "{% else %}Cancel {{ draft.number }}", "Delete draft"),
    ("one_off_charges/gone.html", "Create one-off charge", "This form has moved"),
    # §4.4 buttons and action links
    ("payments/add_payment.html", ">Record payment<", ">Add payment<"),
    ("charges/link_invoice.html", ">Link invoice<", f">Attach {P} invoice<"),
    ("leases/_billing_dialog.html", "Link provider IDs", f"Connect to {P}"),
    ("leases/_billing_dialog.html", ">Link</button>", f">Connect to {P}</button>"),
    ("leases/enrol.html", ">Enable autopay<", ">Set up autopay<"),
    ("leases/detail.html", "Stop autopay", "Turn off autopay"),
    ("run/today.html", "Match invoices", f"Update from {P}"),
    ("one_off_charges/detail.html", "Delete provider invoice", f"Cancel in {P}"),
    ("leases/_billing_dialog.html", "Retry sync", "Try again"),
    ("charges/detail.html", "Retry marking the invoice paid", f"Mark paid in {P} again"),
    ("one_off_charges/detail.html", "Retry marking the invoice paid", f"Mark paid in {P} again"),
    ("stores/_tab_charges.html", "Export rent charges CSV", "Download rent charges"),
    ("stores/_tab_charges.html", "Export one-off charges CSV", "Download one-off charges"),
    ("grid/page.html", "Export CSV", "Download spreadsheet"),
    ("payments/refund.html", "Refund at the payment provider", f"Refund through {P}"),
    ("payments/void.html", "Void at the payment provider", f"Void through {P}"),
    ("webhooks/detail.html", ">Replay<", ">Process again<"),
    ("reconciliation/page.html", ">Run now<", ">Run the nightly check now<"),
    ("returns/list.html", "Resolve with a note", "Close with a note"),
    ("reconciliation/page.html", "Resolve with note", "Close with a note"),
    ("returns/list.html", ">Reschedule<", ">Debit again<"),
    ("invoices/charge_now.html", "Charge ${", "|money }} now"),
    ("one_off_charges/charge_now.html", "Charge ${", "|money }} now"),
    # PLAN-012e-2 D3: the draft's Send is a button of How to collect (in the SUMMARY card
    # since PLAN-012f-a D5).
    ("leases/enrol.html", "Refresh methods", f"Update bank methods from {P}"),
    ("disputes/page.html", ">Refresh<", f">Update from {P}<"),
    ("disputes/page.html", "Store and send", f"Save and send to {P}"),
    # PLAN-012e-2 D3, D4: Schedule a payment is How to collect's Schedule for a date.
    ("invoices/_summary.html", ">Schedule payment<", '>Schedule <span class="collect-amount">'),
    ("stores/_tab_history.html", "Open in the audit log", "Open in the activity log"),
    ("stores/_tab_payments.html", "Open in the payments ledger", "Open in Payments"),
    (
        "operators/detail.html",
        "Revoke every statement link sent",
        "Cancel every statement link sent",
    ),
    # §4.6 template sentences
    ("_job_status.html", " UTC", "started {{ job.created_at|moment }}"),
    # PLAN-012f-b E-4: the sentence is the failed-attempts empty state's line.
    (
        "dashboard/home.html",
        "ATTEMPTED",
        'text="Invoices "|add:company.provider_name|add:" could not collect are listed here."',
    ),
    ("dashboard/home.html", "Last reconciliation", "Last nightly check"),
    ("disputes/page.html", "Last synced", f"Last updated from {P}"),
    ("disputes/page.html", "Not synced yet.", "Not updated yet."),
    ("stores/_tab_schedules.html", "the provider runs the schedule", f"{P} runs the autopay"),
    ("leases/detail.html", "the provider runs the schedule", f"{P} runs the autopay"),
    (
        "leases/enrol.html",
        "One payment provider schedule is created per debit day.",
        f"One autopay is set up in {P} per debit day.",
    ),
    ("leases/enrol.html", "DTSTART", "First debit"),
    ("leases/enrol.html", "Methods as of", "Bank methods as of"),
    ("leases/stop_autopay.html", "re-enrol", "set it up again to restart."),
    ("lease_ends/confirm.html", "tick the box", "check the box"),
    ("transfers/confirm.html", "tick the box", "check the box"),
    (
        "lease_ends/confirm.html",
        "Nothing is sent to the payment provider.",
        f"Nothing is sent to {P}.",
    ),
    (
        "transfers/confirm.html",
        "Nothing is sent to the payment provider.",
        f"Nothing is sent to {P}.",
    ),
    ("leases/_billing_dialog.html", "Provider customer updated", f"Updated from {P}"),
    ("operators/detail.html", "SMS phone", "Text-message phone"),
    ("operators/detail.html", "SMS consent given on", "Text-message consent given on"),
    (
        "reports/deposits.html",
        "nightly reconciliation's deposit step",
        "nightly check's bank-deposit step",
    ),
    # PLAN-012f-b E-1: DESIGN-SPEC §4 Profit and loss (the owner's copy of 2026-10-24) names
    # the column Flags again; its header cell carries attributes now.
    ("reports/pnl.html", "<th>Notes</th>", ">Flags</th>"),
    ("run/today.html", "dead-lettered", f"from {P}"),
    (
        "run/today.html",
        "The payment provider's schedules generate the invoices",
        f"{P}'s autopay makes the invoices",
    ),
    (
        "run/today.html",
        "<code>generate_insurance_charges</code>",
        "the yearly insurance charges run",
    ),
    ("webhooks/detail.html", "Payload (redacted)", "Content (without account data)"),
    ("webhooks/detail.html", "the body was not JSON", "the message could not be read"),
    ("audit/page.html", "Time (UTC)", "<th>Time</th>"),
    # §4.7 empty states
    ("contacts/_log.html", "No contact entries.", "No entries yet. Add one."),  # PLAN-012f D6
    ("documents/_section.html", "No documents.", "No documents yet. Upload one below."),
    ("leases/list.html", "No leases.", "No lease yet."),
    # The leases list has no action bar: the header's "New lease" is the next step there.
    ("leases/list.html", "from the action bar", "No lease yet.{% if can_create %}"),
    ("stores/detail.html", "No leases.", "No lease yet. New lease from the action bar."),
    ("stores/_tab_payments.html", "No payments.<", "No payments yet. Add one from the action bar."),
    ("reconciliation/page.html", "No reconciliation has run yet.", "No nightly check has run yet."),
    ("dashboard/home.html", "No reconciliation has run yet.", "No nightly check has run yet."),
    ("webhooks/list.html", "No webhook events match.", f"No messages from {P} match."),
    (
        "lease_ends/blockers.html",
        "Nothing is live at the payment provider",
        f"Nothing is still open in {P}",
    ),
    (
        "transfers/blockers.html",
        "Nothing is live at the payment provider",
        f"Nothing is still open in {P}",
    ),
]


@pytest.mark.parametrize(("template", "old", "new"), PAIRS)
def test_the_approved_pair_is_applied_in_its_template(template: str, old: str, new: str) -> None:
    text = source(template)
    assert old not in text, (template, old)
    assert new in text, (template, new)


def test_no_template_or_sentence_offers_export_csv_any_more() -> None:
    for path in [*TEMPLATES.rglob("*.html"), *BASE.rglob("*.html")]:
        assert "Export CSV" not in path.read_text(), path.name


def test_the_workspace_names_its_tabs_and_actions_as_the_list_says() -> None:
    tabs = dict(workspace.TABS)
    assert (tabs[workspace.TAB_SCHEDULES], tabs[workspace.TAB_HISTORY]) == ("Autopay", "Activity")
    text = (BACKEND / "ledger" / "services" / "workspace.py").read_text()
    assert '"Record a payment"' not in text and '"Add payment"' in text
    # PLAN-012e-2 D5: the bar's menu is "Lease"; Add entry is the Contact log tab's.
    assert '"Lease"' in text and '"Add contact entry"' not in text and '"Add entry"' not in text
    contacts_tab = BACKEND / "ledger" / "templates" / "ledger" / "stores" / "_tab_contacts.html"
    assert ">Add entry</a>" in contacts_tab.read_text()
    charges_tab = BACKEND / "ledger" / "templates" / "ledger" / "stores" / "_tab_charges.html"
    assert ">Create missing charges</button>" in charges_tab.read_text()


@pytest.mark.parametrize(
    ("path_name", "present", "absent"),
    [
        (
            "ledger:run:today",
            ["Daily charges, ", "Update from Stax"],
            ["Rent run", "Match invoices"],
        ),
        ("ledger:arrears:page", ["<h1>Unpaid balances</h1>", "Download spreadsheet"], ["Arrears<"]),
        ("ledger:audit:page", ["<h1>Activity log</h1>", "<th>Time</th>"], ["Time (UTC)"]),
        ("ledger:webhooks:list", ["<h1>Messages from Stax</h1>"], ["Webhook events"]),
        (
            "ledger:reconciliation:page",
            ["<h1>Nightly check</h1>", "No nightly check has run yet."],
            ["Reconciliation<", "Run now<"],
        ),
        ("ledger:reports:aging", ["<h1>Unpaid by age</h1>"], ["A/R aging"]),
        ("ledger:reports:deposits", ["<h1>Bank account check</h1>"], ["Deposits versus"]),
        ("ledger:disputes:page", ["Update from Stax", "Not updated yet."], ["Refresh<"]),
    ],
)
def test_the_renamed_pages_render_their_new_names(
    path_name: str, present: list[str], absent: list[str]
) -> None:
    admin, _ = client_for(Role.ADMIN)
    html = page(admin, reverse(path_name))
    for text in present:
        assert text in html, (path_name, text)
    for text in absent:
        assert text not in html, (path_name, text)
    assert "<title>[SANDBOX] Monvera · " in html


def test_the_store_page_tabs_and_action_bar_render_the_new_names() -> None:
    lease = a_lease(a_store("D9-6"), an_operator("Tabs Co"))
    admin, _ = client_for(Role.ADMIN)
    html = page(admin, f"/stores/{lease.store_id}/")
    assert ">Autopay<" in html and ">Activity<" in html
    assert ">Schedules<" not in html and ">History<" not in html
    assert "Add payment" in html and "Record a payment" not in html
    # PLAN-012e-2 D5, D6: "Lease ▾" on the bar, "Create missing charges" on the Charges tab,
    # "Add entry" on the Contact log tab, "More ▾" on the operator page.
    assert ">Lease ▾</summary>" in html and ">Create missing charges</button>" in html
    contacts = page(admin, f"/stores/{lease.store_id}/?tab=contacts")
    assert ">Add entry<" in contacts and "Add contact entry" not in contacts
    operator = page(admin, f"/operators/{lease.operator_id}/")
    assert ">More ▾</summary>" in operator


# --- no technical term on screen (§4.1) ----------------------------------------------------

#: The forbidden words of §4.1, as they could appear in a page's visible text.
FORBIDDEN: dict[str, re.Pattern[str]] = {
    "payment provider": re.compile(r"payment provider", re.I),
    "provider": re.compile(r"\bproviders?\b", re.I),
    "worker": re.compile(r"\bworkers?\b", re.I),
    "queued / queue": re.compile(r"\bqueued?\b", re.I),
    "job": re.compile(r"\bjobs?\b", re.I),
    "request waiting": re.compile(r"request waiting", re.I),
    "tick": re.compile(r"\btick(ed)?\b", re.I),
    "flagged": re.compile(r"\bflagged\b", re.I),
    "sync": re.compile(r"\bsync(ed|ing)?\b", re.I),
    "reconciliation": re.compile(r"\breconcil", re.I),
    "webhook": re.compile(r"\bwebhooks?\b", re.I),
    "dead-letter": re.compile(r"dead[- ]letter", re.I),
    "payload": re.compile(r"\bpayloads?\b", re.I),
    "legacy": re.compile(r"\blegacy\b", re.I),
    "UTC": re.compile(r"\bUTC\b"),
    "TODO-VERIFY": re.compile(r"TODO-VERIFY"),
    "DTSTART": re.compile(r"DTSTART"),
    "email (spelling)": re.compile(r"\bemail\b", re.I),
    "Export CSV": re.compile(r"Export CSV"),
    "Record a payment": re.compile(r"Record (a )?payment"),
}
_SHOWN_ATTRIBUTE = re.compile(r'\b(?:title|aria-label|placeholder)="([^"]*)"')


def visible_text(html: str) -> str:
    """The text a person reads or hears: text nodes and the shown attributes, without
    scripts, styles and comments."""
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    shown = " ".join(_SHOWN_ATTRIBUTE.findall(html))
    return unescape(re.sub(r"<[^>]+>", " ", html) + " " + shown)


def main_pages() -> list[str]:
    operator = an_operator("Words Co")
    store = a_store("D9-7")
    lease = a_lease(store, operator)
    charge = a_rent_charge(lease, SEPTEMBER, due="2000.00")
    a_payment(charge, "500.00", date(2026, 9, 5))
    a_one_off_charge(operator, "150.00", SEPTEMBER, lease=lease)
    store_page = f"/stores/{store.pk}/"
    return [
        "/",
        reverse("ledger:stores:list"),
        *(f"{store_page}?tab={tab}" for tab, _ in workspace.TABS),
        reverse("ledger:operators:list"),
        reverse("ledger:operators:detail", kwargs={"pk": operator.pk}),
        reverse("ledger:operators:statement", kwargs={"pk": operator.pk}),
        reverse("ledger:leases:list"),
        reverse("ledger:leases:detail", kwargs={"pk": lease.pk}),
        reverse("ledger:charges:detail", kwargs={"pk": charge.pk}),
        reverse("ledger:grid:page"),
        reverse("ledger:arrears:page"),
        reverse("ledger:reports:index"),
        reverse("ledger:reports:pnl"),
        reverse("ledger:reports:aging"),
        reverse("ledger:reports:revenue"),
        reverse("ledger:reports:payments_by_method"),
        reverse("ledger:reports:deposits"),
        reverse("ledger:payments:list"),
        reverse("ledger:returns:list"),
        reverse("ledger:calendar:page"),
        reverse("ledger:disputes:page"),
        reverse("ledger:run:today"),
        reverse("ledger:audit:page"),
        reverse("ledger:webhooks:list"),
        reverse("ledger:reconciliation:page"),
    ]


def test_no_forbidden_word_is_on_the_main_pages() -> None:
    admin, _ = client_for(Role.ADMIN)
    found = []
    for path in main_pages():
        text = visible_text(page(admin, path))
        for word, pattern in FORBIDDEN.items():
            for match in pattern.finditer(text):
                around = text[max(0, match.start() - 40) : match.end() + 40]
                found.append(f"{path}: {word}: {' '.join(around.split())}")
    assert found == []


def test_the_forbidden_word_check_finds_a_word() -> None:
    """The check above is not vacuous: a page with a forbidden word is caught."""
    text = visible_text('<p title="Synced at 10:00 UTC">The payment provider</p>')
    assert FORBIDDEN["payment provider"].search(text)
    assert FORBIDDEN["sync"].search(text) and FORBIDDEN["UTC"].search(text)
    assert not FORBIDDEN["email (spelling)"].search("E-mail ops@example.com")


# --- the sandbox banner, CSV headers, the e-mail status line -------------------------------


def test_the_banner_says_test_system_no_real_money_and_keeps_the_title_prefix() -> None:
    viewer, _ = client_for(Role.VIEWER)
    html = page(viewer, "/")
    assert html.count(BANNER) == 1 and "NO REAL MONEY" not in html
    assert "<title>[SANDBOX] " in html


def test_csv_headers_and_column_keys_are_unchanged() -> None:
    """§4: nothing in the list changes a CSV header (an importer or a reader of an older
    export depends on it); the on-screen "Time (UTC)" became "Time", the CSV keeps both."""
    assert grid.CSV_FIXED_HEADER == ("Store number", "Store", "State", "Operator", "Channel")
    assert audit.CSV_HEADER[:3] == ("Audit entry", "Time (UTC)", "Time (New York)")
    from ledger.views import reconciliation, webhooks

    assert reconciliation.EXPORT_HEADER[1] == "Mismatch"
    assert webhooks.EXPORT_HEADER[3] == "Provider object (masked)"


def test_the_collection_log_names_the_test_inbox() -> None:
    from ledger.services import collections

    assert collections.SENT_TEXT[MessageStatus.CATCH_ALL] == "Sent to the test inbox."


# --- carry-over (a): the transfer's incoming step has the twice-a-month tick ---------------


def test_the_incoming_step_shows_the_second_debit_day_behind_the_tick() -> None:
    from tests.test_forms_show_what_applies import an_incoming_step, field_of

    client, user = client_for(Role.ADMIN)
    row = an_incoming_step(user)
    html = page(client, f"/transfers/{row.pk}/incoming/")
    tick = field_of(html, TWICE_MONTHLY_ID)
    assert "name=" not in tick and " checked" not in tick
    assert 'id="twice-monthly-field" hidden data-reveal' in html
    assert "Debited twice a month" in html
    assert f'{SHOWN_WHEN}="{TWICE_MONTHLY_ID}"' in field_of(html, "id_second_debit_day")
    assert "ledger/shown_when.js" in html
    # A refused or typed second day starts the tick ticked, as on the lease form.
    bound = TransferIncomingForm(data={"second_debit_day": "15"})
    assert bound.twice_monthly() is True
    assert TransferIncomingForm().twice_monthly() is False
