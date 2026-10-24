"""The invoice builder's "How to collect" (docs/qa/PLAN-012e2-fewer-actions.md D1, D3, D4;
§6 A; the row-set parity of each path is ``test_collect_parity.py``, the adversarial cases
are the QA owner's file).

The service (:func:`ledger.services.invoice_builder.collect`): Record payment now and
Schedule for a date with their refusals, a second submit, Charge now's confirmation and
role; Record payment now through the worker (the provider invoice created, then marked
paid as cash). The page: the fieldset, its buttons, the dependent fields' markup, the
query string. The three moved routes: their redirects and role gates, and the removed
templates and form classes. Nothing reaches a network.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.test import override_settings
from django.urls import reverse

from accounts.roles import Role
from ledger import forms as ledger_forms
from ledger.models import (
    AuditLog,
    CollectionStatus,
    FeeType,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    Lease,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
)
from ledger.services import invoice_builder, rent_run
from ledger.services import one_off_charges as one_off_service
from ledger.services.invoice_builder import CollectChoice, LineInput, PaymentInput
from payments import offline_marking
from payments.errors import ProviderUnavailable
from payments.fake import FakeProvider
from payments.models import Job, JobKind, JobStatus
from tests.job_helpers import run_queued_jobs
from tests.test_charge_now import store_methods
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator, calls
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
TEMPLATES = Path(__file__).resolve().parents[1] / "ledger" / "templates" / "ledger"
WIRE = PaymentInput(method="wire", paid_on=TODAY, reference="WIRE-1001", note="Wire received.")


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


def user(role: Role) -> Any:
    return client_for(role)[1]


def a_world(fake: FakeProvider) -> tuple[Operator, Lease]:
    operator = an_operator(fake)
    return operator, make_lease(operator=operator)


def a_draft(operator: Operator, lease: Lease | None, lines: list[LineInput]) -> InvoiceDraft:
    outcome = invoice_builder.create_draft(
        operator, lease, "", None, lines, user(Role.BILLING), today=TODAY
    )
    assert outcome.ok and outcome.draft is not None, outcome.message
    return outcome.draft


def two_lines() -> list[LineInput]:
    return [
        LineInput("Arrears September", "", None, D("100.00")),
        LineInput("Sign repair", "", None, D("75.00")),
    ]


def one_line(amount: str = "150.00") -> list[LineInput]:
    return [LineInput("Sign repair", "North wall", None, D(amount))]


def nothing_written() -> tuple[int, int, int, int]:
    return (
        OneOffCharge.objects.count(),
        Invoice.objects.count(),
        Payment.objects.count(),
        Job.objects.count(),
    )


def collect(draft: InvoiceDraft, choice: str, role: Role = Role.BILLING, **kwargs: Any) -> Any:
    return invoice_builder.collect(draft.pk, choice, actor=user(role), today=TODAY, **kwargs)


# --- Record payment now (D1) --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payment", "message"),
    [
        (PaymentInput("cash", TODAY, "1234", "Cash received."), rent_run.PAY_METHOD),
        (PaymentInput("credit", TODAY, "", "Credit."), rent_run.PAY_METHOD),
        (
            PaymentInput("wire", TODAY + timedelta(days=1), "", "Wire received."),
            rent_run.PAY_FUTURE,
        ),
        (PaymentInput("zelle", TODAY, "", "  "), rent_run.PAY_NOTE_REQUIRED),
        (PaymentInput("zelle", TODAY, "4111 1111 1111 1111", "Zelle."), None),
        (PaymentInput("other", TODAY, "", "Card 4111 1111 1111 1111"), None),
    ],
)
def test_record_refuses_a_bad_payment_and_writes_nothing(
    fake: FakeProvider, payment: PaymentInput, message: str | None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    before = nothing_written()
    outcome = collect(draft, CollectChoice.RECORD, payment=payment)
    assert not outcome.ok
    assert outcome.message == (message or rent_run.PAY_NOTE_PAYMENT_DATA)
    assert "4111" not in outcome.message
    assert nothing_written() == before
    assert InvoiceDraft.objects.get(pk=draft.pk).status == InvoiceDraftStatus.DRAFT


def test_record_without_a_payment_is_refused(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    outcome = collect(draft, CollectChoice.RECORD)
    assert (outcome.ok, outcome.message) == (False, invoice_builder.RECORD_PAYMENT_REQUIRED)
    assert nothing_written() == (0, 0, 0, 0)


def test_record_pays_every_line_at_its_total_and_queues_the_mark_after_the_create(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    outcome = collect(draft, CollectChoice.RECORD, payment=WIRE)
    assert outcome.ok, outcome.message
    assert outcome.invoice is not None and outcome.payment is not None
    assert outcome.message == invoice_builder.RECORDED.format(number=outcome.invoice.invoice_number)
    first, second = outcome.charges
    assert [(row.collection, row.status) for row in (first, second)] == [("manual", "owed")] * 2
    payments = list(Payment.objects.order_by("amount"))
    assert [(row.one_off_charge_id, row.amount) for row in payments] == [
        (second.pk, D("75.00")),
        (first.pk, D("100.00")),
    ]
    assert {row.allocation_group for row in payments} == {payments[0].allocation_group}
    assert all(row.reference == "WIRE-1001" and row.method == "wire" for row in payments)
    assert outcome.payment.one_off_charge_id == first.pk
    statuses = OneOffCharge.objects.values_list("status", flat=True)
    assert set(statuses) == {OneOffChargeStatus.PAID}
    # Only the create is queued: its success queues the mark (never the clock).
    [create] = Job.objects.all()
    assert (create.kind, create.entity_id) == (JobKind.INVOICE_CREATE, first.pk)
    sent = AuditLog.objects.get(action=invoice_builder.SENT_ACTION)
    assert sent.after is not None
    assert (sent.after["choice"], sent.after["collection"]) == ("record", "manual")


def test_record_through_the_worker_creates_the_invoice_unsent_then_marks_it_paid(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    outcome = collect(draft, CollectChoice.RECORD, payment=WIRE)
    assert outcome.ok, outcome.message
    done = run_queued_jobs()
    assert [(job.kind, job.status) for job in done] == [
        (JobKind.INVOICE_CREATE, JobStatus.DONE),
        (JobKind.ONE_OFF_PAYMENT_MARK, JobStatus.DONE),
    ], [job.result for job in done]
    assert len(calls(fake, "create_invoice")) == 1
    assert calls(fake, "send_invoice_email") == []
    [(ref, offline)] = calls(fake, "mark_invoice_paid_cash_or_check")
    assert offline.amount == D("175.00")  # type: ignore[attr-defined]
    assert ref
    charge = OneOffCharge.objects.get(pk=outcome.charges[0].pk)
    assert not charge.needs_review
    mark = Job.objects.get(kind=JobKind.ONE_OFF_PAYMENT_MARK)
    assert mark.params == {"payment": str(outcome.payment.pk)}
    # A create run again (a lost worker) queues no second mark.
    from payments.jobs import runners

    runners.invoice_create_job(Job.objects.get(kind=JobKind.INVOICE_CREATE))
    assert Job.objects.filter(kind=JobKind.ONE_OFF_PAYMENT_MARK).count() == 1


def test_record_whose_create_fails_is_flagged_then_marked_after_the_retry(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    outcome = collect(draft, CollectChoice.RECORD, payment=WIRE)
    assert outcome.ok, outcome.message
    first = outcome.charges[0]
    fake.fail_next("create_invoice", ProviderUnavailable(503, "unavailable"))
    [failed] = run_queued_jobs()
    assert (failed.kind, failed.status) == (JobKind.INVOICE_CREATE, JobStatus.FAILED)
    assert not Job.objects.filter(kind=JobKind.ONE_OFF_PAYMENT_MARK).exists()
    flagged = OneOffCharge.objects.get(pk=first.pk)
    assert flagged.needs_review
    assert flagged.review_message == offline_marking.failure_text(
        offline_marking.MARK_FAILED, "ProviderUnavailable", ""
    )
    assert "Not marked paid in" in one_off_service.review_message(flagged)
    # The charge page's retry queues the create again; its success queues the mark.
    client = client_for(Role.BILLING)[0]
    client.post(f"/one-off-charges/{first.pk}/retry/")
    done = run_queued_jobs()
    assert [(job.kind, job.status) for job in done] == [
        (JobKind.INVOICE_CREATE, JobStatus.DONE),
        (JobKind.ONE_OFF_PAYMENT_MARK, JobStatus.DONE),
    ], [job.result for job in done]
    assert len(calls(fake, "mark_invoice_paid_cash_or_check")) == 1
    cleared = OneOffCharge.objects.get(pk=first.pk)
    assert (cleared.needs_review, cleared.review_message) == (False, "")


def test_a_charge_sent_by_email_never_gets_a_mark_from_its_create(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    assert collect(draft, CollectChoice.EMAIL).ok
    run_queued_jobs()
    assert not Job.objects.filter(kind=JobKind.ONE_OFF_PAYMENT_MARK).exists()


def test_record_twice_makes_one_set_of_payments(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    assert collect(draft, CollectChoice.RECORD, payment=WIRE).ok
    written = nothing_written()
    again = collect(draft, CollectChoice.RECORD, payment=WIRE)
    assert not again.ok and "already sent" in again.message
    assert nothing_written() == written


def test_record_refuses_a_rent_line_and_a_viewer(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    rent = LineInput("", "", None, None, rent_lease=lease.pk, rent_month=date(2026, 10, 1))
    draft = a_draft(operator, lease, [rent, *two_lines()])
    outcome = collect(draft, CollectChoice.RECORD, payment=WIRE)
    assert (outcome.ok, outcome.message) == (False, invoice_builder.RECORD_RENT)
    other = a_draft(operator, lease, two_lines())
    viewer = collect(other, CollectChoice.RECORD, Role.VIEWER, payment=WIRE)
    assert (viewer.ok, viewer.message) == (False, invoice_builder.BILLING_ONLY)
    assert (OneOffCharge.objects.count(), Payment.objects.count()) == (0, 0)


# --- Schedule for a date (D1) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("due", "message"),
    [
        (None, invoice_builder.SCHEDULE_DATE_REQUIRED),
        (TODAY - timedelta(days=1), invoice_builder.SCHEDULE_NOT_AFTER_TODAY),
        (TODAY, invoice_builder.SCHEDULE_NOT_AFTER_TODAY),
        (
            TODAY + timedelta(days=invoice_builder.SCHEDULE_HORIZON_DAYS + 1),
            invoice_builder.SCHEDULE_TOO_FAR,
        ),
    ],
)
def test_schedule_refuses_a_date_not_after_today_or_beyond_the_horizon(
    fake: FakeProvider, due: date | None, message: str
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, one_line())
    outcome = collect(draft, CollectChoice.SCHEDULE, due_date=due)
    assert (outcome.ok, outcome.message, outcome.field) == (False, message, "due_date")
    assert nothing_written() == (0, 0, 0, 0)


def test_schedule_on_the_horizons_last_day_makes_one_autopay_charge(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, one_line())
    due = TODAY + timedelta(days=invoice_builder.SCHEDULE_HORIZON_DAYS)
    outcome = collect(draft, CollectChoice.SCHEDULE, due_date=due)
    assert outcome.ok, outcome.message
    [charge] = outcome.charges
    assert (charge.collection, charge.due_date, charge.price) == ("autopay_due", due, D("150.00"))
    assert outcome.schedule is not None and outcome.schedule.kind == JobKind.SCHEDULE_CREATE
    assert not Invoice.objects.exists() and not InvoiceDraft.objects.exists()
    sent = AuditLog.objects.get(action=invoice_builder.SENT_ACTION)
    assert sent.after is not None
    assert (sent.after["choice"], sent.after["charges"]) == ("schedule", [str(charge.pk)])


def test_schedule_twice_makes_one_schedule(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, one_line())
    due = TODAY + timedelta(days=14)
    assert collect(draft, CollectChoice.SCHEDULE, due_date=due).ok
    again = collect(draft, CollectChoice.SCHEDULE, due_date=due)
    assert (again.ok, again.message) == (False, invoice_builder.NOT_A_DRAFT)
    assert Job.objects.filter(kind=JobKind.SCHEDULE_CREATE).count() == 1


def test_schedule_takes_one_item_line(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    due = TODAY + timedelta(days=14)
    outcome = collect(draft, CollectChoice.SCHEDULE, due_date=due)
    assert (outcome.ok, outcome.message) == (False, invoice_builder.SCHEDULE_ONE_LINE)
    rent = LineInput("", "", None, None, rent_lease=lease.pk, rent_month=date(2026, 10, 1))
    alone = a_draft(operator, lease, [rent])
    assert collect(alone, CollectChoice.SCHEDULE, due_date=due).message == (
        invoice_builder.SCHEDULE_ONE_LINE
    )
    assert nothing_written() == (0, 0, 0, 0)


def test_schedule_needs_the_large_debit_confirmation(fake: FakeProvider, settings: Any) -> None:
    settings.LARGE_DEBIT_WARNING_AMOUNT = D("1000.00")
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, one_line("1500.00"))
    due = TODAY + timedelta(days=14)
    refused = collect(draft, CollectChoice.SCHEDULE, due_date=due)
    assert (refused.ok, refused.field) == (False, "confirm")
    assert nothing_written() == (0, 0, 0, 0)
    assert collect(draft, CollectChoice.SCHEDULE, due_date=due, confirmed=True).ok


def test_schedule_refuses_an_operator_draft(fake: FakeProvider) -> None:
    operator, _ = a_world(fake)
    draft = a_draft(operator, None, one_line())
    outcome = collect(draft, CollectChoice.SCHEDULE, due_date=TODAY + timedelta(days=14))
    assert (outcome.ok, outcome.message) == (False, invoice_builder.SCHEDULE_NO_LEASE)
    assert nothing_written() == (0, 0, 0, 0)
    assert InvoiceDraft.objects.filter(pk=draft.pk).exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"collection_status": CollectionStatus.ENDED, "ended_on": date(2026, 9, 30)},
        {"ended_on": date(2026, 12, 31)},
        {"collection_status": CollectionStatus.PAUSED},
        {"collection_status": CollectionStatus.PROSPECTIVE},
    ],
)
def test_schedule_refuses_a_lease_that_ended_or_is_not_active(
    fake: FakeProvider, changes: dict[str, Any]
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, one_line())
    Lease.objects.filter(pk=lease.pk).update(**changes)
    outcome = collect(draft, CollectChoice.SCHEDULE, due_date=TODAY + timedelta(days=14))
    assert (outcome.ok, outcome.message) == (False, invoice_builder.SCHEDULE_LEASE_NOT_ACTIVE)
    assert "4111" not in outcome.message and str(lease.pk) not in outcome.message
    assert nothing_written() == (0, 0, 0, 0)


def test_schedule_refuses_a_rent_line(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    rent = LineInput("", "", None, None, rent_lease=lease.pk, rent_month=date(2026, 10, 1))
    draft = a_draft(operator, lease, [rent])
    outcome = collect(draft, CollectChoice.SCHEDULE, due_date=TODAY + timedelta(days=14))
    assert (outcome.ok, outcome.message) == (False, invoice_builder.SCHEDULE_ONE_LINE)
    assert nothing_written() == (0, 0, 0, 0)


# --- Charge now and the other choices (D1) --------------------------------------------------------


def test_charge_now_still_needs_the_admin_and_the_large_debit_confirmation(
    fake: FakeProvider, enabled: None, settings: Any
) -> None:
    settings.LARGE_DEBIT_WARNING_AMOUNT = D("100.00")
    operator, lease = a_world(fake)
    store_methods(operator)
    draft = a_draft(operator, lease, two_lines())
    billing = collect(draft, CollectChoice.CHARGE_NOW, confirmed=True)
    assert (billing.ok, billing.message) == (False, invoice_builder.CHARGE_NOW_ADMIN)
    unconfirmed = collect(draft, CollectChoice.CHARGE_NOW, Role.ADMIN)
    assert (unconfirmed.ok, unconfirmed.field) == (False, "confirm")
    assert nothing_written() == (0, 0, 0, 0)
    assert collect(draft, CollectChoice.CHARGE_NOW, Role.ADMIN, confirmed=True).ok


def test_an_unknown_choice_and_draft_on_a_sent_invoice_are_refused(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    unknown = collect(draft, "<script>")
    assert (unknown.ok, unknown.message) == (False, invoice_builder.CHOICE_UNKNOWN)
    assert collect(draft, CollectChoice.DRAFT).ok
    assert collect(draft, CollectChoice.EMAIL).ok
    kept = collect(draft, CollectChoice.DRAFT)
    assert not kept.ok and "already sent" in kept.message


# --- The page (D3) --------------------------------------------------------------------------------


def draft_page(draft: InvoiceDraft, role: Role = Role.BILLING, query: str = "") -> str:
    client = client_for(role)[0]
    response = client.get(f"/invoices/drafts/{draft.pk}/{query}")
    assert response.status_code == 200
    return response.content.decode()


def test_the_buttons_name_the_choice_and_the_amount(fake: FakeProvider, enabled: None) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, one_line("175.00"))
    html = draft_page(draft, Role.ADMIN)
    assert "<legend>How to collect</legend>" in html
    for label in (
        "Save draft",
        "Send by e-mail",
        "Charge now",
        "Schedule for a date",
        "Record payment now",
    ):
        # PLAN-012f-a D5: each choice is a tile, its radio beside its bold name.
        assert f'<span class="collect-name">{label}</span>' in html, label
    assert 'id="save-draft" data-shown-when="collect-draft">Save draft</button>' in html
    assert '>Send <span class="collect-amount">$175.00</span> by e-mail</button>' in html
    assert '>Charge <span class="collect-amount">$175.00</span> now</button>' in html
    assert (
        '>Schedule <span class="collect-amount">$175.00</span> for '
        '<span class="collect-date" data-empty="the date chosen">the date chosen</span></button>'
    ) in html
    assert '>Record <span class="collect-amount">$175.00</span> received</button>' in html
    dated = draft_page(draft, Role.ADMIN, "?collect=schedule")
    assert 'id="collect-schedule" checked' in dated


def test_billing_sees_every_choice_but_charge_now(fake: FakeProvider, enabled: None) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, one_line("175.00"))
    html = draft_page(draft)
    assert 'id="collect-charge_now"' not in html and 'id="draft-charge-now"' not in html
    for value in ("draft", "email", "schedule", "record"):
        assert f'id="collect-{value}"' in html, value
    # Charge now posted by billing anyway is refused by the form; nothing is collected.
    client = client_for(Role.BILLING)[0]
    client.post(f"/invoices/drafts/{draft.pk}/edit/", page_post(lease, collect="charge_now"))
    assert InvoiceDraft.objects.get(pk=draft.pk).status == InvoiceDraftStatus.DRAFT
    assert nothing_written() == (0, 0, 0, 0)


def test_each_choices_fields_depend_on_its_radio_and_show_without_script(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, one_line("175.00"))
    html = draft_page(draft)
    for name, radio in (
        ("schedule_on", "collect-schedule"),
        ("confirm_large_debit", "collect-schedule"),
        ("method", "collect-record"),
        ("paid_on", "collect-record"),
        ("reference", "collect-record"),
        ("payment_note", "collect-record"),
    ):
        assert f'name="{name}"' in html, name
        tag = html[html.index(f'name="{name}"') - 200 : html.index(f'name="{name}"') + 200]
        assert f'data-shown-when="{radio}"' in tag, name
    # Nothing is hidden by the server: the script hides what does not apply.
    block = html[html.index('id="collect-schedule-fields"') : html.index('id="collect-actions"')]
    assert " hidden" not in block
    assert "ledger/shown_when.js" in html
    # The methods are the ones "Add payment" records (PLAN-012e-3 D2: a check is a check).
    assert [value for value, _ in ledger_forms.MANUAL_METHOD_CHOICES] == [
        "zelle",
        "wire",
        "check",
        "other",
    ]


@pytest.mark.parametrize("value", ["<script>alert(1)</script>", "bogus", "", "charge_now"])
def test_an_unknown_collect_value_opens_save_draft_and_is_not_echoed(
    fake: FakeProvider, value: str
) -> None:
    operator, lease = a_world(fake)
    client = client_for(Role.BILLING)[0]
    response = client.get(f"/invoices/new/store/{lease.store_id}/", {"collect": value})
    assert response.status_code == 200
    html = response.content.decode()
    assert 'id="collect-draft" checked' in html
    assert "<script>alert(1)</script>" not in html and "bogus" not in html


def test_item_prefills_line_one_with_a_catalogue_item_else_nothing(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    FeeType.objects.create(code="sign-collect", name="Sign", active=True, default_price=D("75.00"))
    FeeType.objects.create(code="old-collect", name="Old sign", active=False)
    client = client_for(Role.BILLING)[0]
    path = f"/invoices/new/store/{lease.store_id}/"
    filled = client.get(path, {"item": "sign-collect"}).content.decode()
    assert 'name="lines-0-item" value="Sign"' in filled or 'value="Sign"' in filled
    assert 'value="75.00"' in filled
    for wanted in ("old-collect", "../../x", "<b>x</b>"):
        html = client.get(path, {"item": wanted}).content.decode()
        assert 'value="Old sign"' not in html and "../../x" not in html and "<b>x</b>" not in html


def page_post(lease: Lease, **fields: str) -> dict[str, str]:
    data = {
        "creation_key": str(uuid.uuid4()),
        "lease": str(lease.pk),
        "memo": "",
        "due_date": "",
        "lines-TOTAL_FORMS": "2",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "50",
        "lines-0-item": "Arrears September",
        "lines-0-unit_amount": "100.00",
        "lines-1-item": "Sign repair",
        "lines-1-unit_amount": "75.00",
    }
    data.update(fields)
    return data


def test_charge_now_on_the_page_opens_its_confirmation_and_converts_nothing(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    client = client_for(Role.ADMIN)[0]
    response = client.post(
        f"/invoices/new/store/{lease.store_id}/", page_post(lease, submit_collect="charge_now")
    )
    draft = InvoiceDraft.objects.get()
    assert response["Location"] == f"/invoices/drafts/{draft.pk}/charge-now/"
    assert draft.status == InvoiceDraftStatus.DRAFT and not OneOffCharge.objects.exists()


def test_record_on_the_page_collects_and_the_draft_page_shows_the_collection(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    client = client_for(Role.BILLING)[0]
    data = page_post(
        lease,
        collect="record",
        method="zelle",
        paid_on=TODAY.isoformat(),
        reference="Z-77",
        payment_note="Zelle received.",
    )
    refused = client.post(f"/invoices/new/store/{lease.store_id}/", {**data, "payment_note": ""})
    assert refused["Location"] == f"/invoices/new/store/{lease.store_id}/"
    assert not InvoiceDraft.objects.exists()
    response = client.post(f"/invoices/new/store/{lease.store_id}/", data)
    draft = InvoiceDraft.objects.get()
    assert response["Location"] == f"/invoices/drafts/{draft.pk}/"
    assert Payment.objects.count() == 2
    html = client.get(response["Location"]).content.decode()
    assert '<dd id="sent-collection">Record payment now</dd>' in html
    assert 'id="invoice-split"' not in html  # two lines: split each from its charge


def test_a_refused_choice_keeps_the_saved_draft_with_the_sentence(fake: FakeProvider) -> None:
    operator = an_operator(fake, bank=None)
    lease = make_lease(operator=operator)
    client = client_for(Role.BILLING)[0]
    response = client.post(
        f"/invoices/new/store/{lease.store_id}/",
        {
            **page_post(lease, collect="schedule", schedule_on="2026-10-20"),
            "lines-TOTAL_FORMS": "1",
        },
    )
    draft = InvoiceDraft.objects.get()
    assert response["Location"] == f"/invoices/drafts/{draft.pk}/"
    html = client.get(response["Location"]).content.decode()
    assert "no linked bank method" in html and 'id="collect-schedule" checked' in html
    assert not OneOffCharge.objects.exists()


def schedule_offered(html: str) -> bool:
    offered = 'id="collect-schedule"' in html and 'id="schedule-draft"' in html
    note = f'id="collect-schedule-note">{invoice_builder.SCHEDULE_NOT_OFFERED}</p>' in html
    assert offered != note, "the radio or the one line, never both"
    return offered


def test_the_page_offers_schedule_on_an_active_lease_with_one_item_line(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    assert invoice_builder.SCHEDULE_NOT_OFFERED == "Schedule needs an active lease with one line."
    assert schedule_offered(draft_page(a_draft(operator, lease, one_line())))
    assert not schedule_offered(draft_page(a_draft(operator, lease, two_lines())))
    assert not schedule_offered(draft_page(a_draft(operator, None, one_line())))
    rent = LineInput("", "", None, None, rent_lease=lease.pk, rent_month=date(2026, 10, 1))
    assert not schedule_offered(draft_page(a_draft(operator, lease, [rent])))
    client = client_for(Role.BILLING)[0]
    store = client.get(f"/invoices/new/store/{lease.store_id}/").content.decode()
    assert schedule_offered(store)
    alone = client.get(f"/invoices/new/operator/{operator.pk}/").content.decode()
    assert not schedule_offered(alone)
    # ?collect=schedule where it is not offered opens Save draft.
    held = client.get(f"/invoices/new/operator/{operator.pk}/", {"collect": "schedule"})
    assert 'id="collect-draft" checked' in held.content.decode()


def test_the_page_offers_no_schedule_on_an_ended_lease(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, one_line())
    Lease.objects.filter(pk=lease.pk).update(ended_on=date(2026, 12, 31))
    assert not schedule_offered(draft_page(draft))
    # Posted anyway, the form refuses the choice: nothing is scheduled.
    client = client_for(Role.BILLING)[0]
    client.post(
        f"/invoices/drafts/{draft.pk}/edit/",
        {
            **page_post(lease, collect="schedule", schedule_on="2026-10-20"),
            "lines-TOTAL_FORMS": "1",
        },
    )
    assert nothing_written() == (0, 0, 0, 0)
    assert InvoiceDraft.objects.get(pk=draft.pk).status == InvoiceDraftStatus.DRAFT


# --- The moved routes (D4) ------------------------------------------------------------------------


def test_the_three_routes_redirect_to_the_builder_with_the_choice(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    sign = FeeType.objects.create(code="sign-collect", name="Sign", active=True, show_in_setup=True)
    builder = reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id})
    known = f"?store={lease.store_id}&lease={lease.pk}"
    billing, admin = client_for(Role.BILLING)[0], client_for(Role.ADMIN)[0]
    cases = (
        (billing, "ledger:stores:schedule_payment", lease.store_id, f"{known}&collect=schedule"),
        (admin, "ledger:stores:charge_now_pick", lease.store_id, f"{known}&collect=charge_now"),
        (
            billing,
            "ledger:leases:add_setup_charge",
            lease.pk,
            f"{known}&collect=draft&item={sign.code}",
        ),
    )
    for client, name, pk, query in cases:
        url = reverse(name, kwargs={"pk": pk})
        response = client.get(url)
        assert (response.status_code, response["Location"]) == (302, builder + query), name
        # A POST has no rule in the registry (a read-only route): refused there.
        assert client.post(url, {}).status_code == 403, name
    viewer = client_for(Role.VIEWER)[0]
    for _, name, pk, _ in cases:
        assert viewer.get(reverse(name, kwargs={"pk": pk})).status_code == 403, name
    pick = reverse("ledger:stores:charge_now_pick", kwargs={"pk": lease.store_id})
    assert billing.get(pick).status_code == 403
    assert nothing_written() == (0, 0, 0, 0)


def test_the_setup_redirect_carries_a_catalogue_code_only(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    first = FeeType.objects.create(code="a-first", name="First", active=True, show_in_setup=True)
    FeeType.objects.create(code="b-second", name="Second", active=True, show_in_setup=True)
    FeeType.objects.create(code="c-off", name="Off", active=True)
    billing = client_for(Role.BILLING)[0]
    url = reverse("ledger:leases:add_setup_charge", kwargs={"pk": lease.pk})
    assert billing.get(url)["Location"].endswith(f"item={first.code}")
    assert billing.get(url, {"item": "c-off"})["Location"].endswith("item=c-off")
    for wanted in ("../../x", "<script>", "nope"):
        location = billing.get(url, {"item": wanted})["Location"]
        assert location.endswith(f"item={first.code}"), wanted
    OneOffCharge.objects.create(
        operator=operator,
        lease=lease,
        fee_type=first,
        category="setup",
        price=D("1.00"),
        due_date=TODAY,
        status=OneOffChargeStatus.OWED,
        description="First",
    )
    assert billing.get(url)["Location"].endswith("item=b-second")
    other = make_lease(operator=an_operator(fake, customer="cus-other", bank="pm-other"))
    assert billing.get(url, {"store": str(other.store_id)}).status_code == 404


def test_the_old_templates_and_form_classes_are_gone() -> None:
    for name in ("stores/schedule_payment.html", "stores/charge_now_pick.html"):
        assert not (TEMPLATES / name).exists(), name
    form = (TEMPLATES / "one_off_charges" / "form.html").read_text()
    assert "Add setup charge" not in form
    for name in ("SetupChargeAddForm", "SchedulePaymentForm"):
        assert not hasattr(ledger_forms, name), name
