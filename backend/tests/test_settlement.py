"""PLAN-012f-b Batch D, slice D-2 (D16; the owner's finding 6): Schedule settlement, the
invoice builder's choice that splits the invoice total into 2 to 12 equal bank debits
through the installment engine. The arithmetic and the dates (the rounding on the last;
monthly, weekly, yearly), the preview sentence and its JSON route, the conversion (one
manual charge with a settlement plan of autopay installments, nothing at the provider for
the charge itself), the refusals with nothing written, the "Settlement plan · k of N"
labels on the four surfaces, the greyed tile, the migration and the guard lists.
Fable's C2-4 is tested in ``test_add_payment_page.py``. Nothing reaches a network.
"""

from __future__ import annotations

import importlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.urls import reverse

from accounts import registry
from accounts.roles import Role
from ledger import forms as ledger_forms
from ledger.models import (
    AuditLog,
    CollectionMethod,
    InstallmentCadence,
    InstallmentCollection,
    InstallmentOrigin,
    InstallmentPlan,
    InvoiceDraft,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
)
from ledger.services import installments, invoice_builder
from ledger.services import one_off_charges as one_off_service
from ledger.services.invoice_builder import CollectChoice, LineInput, SettlementInput
from payments.fake import FakeProvider
from payments.models import Job, JobKind
from tests.test_builder_save_prompt import banked, draft_html, greyed
from tests.test_how_to_collect import (
    a_draft,
    nothing_written,
    one_line,
    page_post,
    two_lines,
    user,
)
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
NOV_1 = date(2026, 11, 1)
MONTHLY = InstallmentCadence.MONTHLY.value
WEEKLY = InstallmentCadence.WEEKLY.value
YEARLY = InstallmentCadence.YEARLY.value
PREVIEW = "/invoices/settlement-preview.json"


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def ten_thousand() -> list[LineInput]:
    return [LineInput("Settlement of arrears", "", None, D("10000.00"))]


def settle(
    draft: InvoiceDraft,
    count: int | None = 10,
    first_on: date | None = NOV_1,
    cadence: str = MONTHLY,
    *,
    confirmed: bool = False,
    role: Role = Role.BILLING,
) -> invoice_builder.Outcome:
    return invoice_builder.collect(
        draft.pk,
        CollectChoice.SETTLEMENT,
        actor=user(role),
        today=TODAY,
        confirmed=confirmed,
        settlement=SettlementInput(count=count, first_on=first_on, cadence=cadence),
    )


def written() -> tuple[int, int, int, int, int, int]:
    return (
        *nothing_written(),
        InstallmentPlan.objects.count(),
        InvoiceDraft.objects.count(),
    )


# --- The arithmetic and the dates -----------------------------------------------------------


def test_ten_thousand_in_ten_monthly_is_ten_even_debits_on_the_first() -> None:
    lines = installments.settlement_lines(D("10000.00"), 10, NOV_1, MONTHLY)
    assert [line.amount for line in lines] == [D("1000.00")] * 10
    assert [line.due for line in lines] == [
        date(2026 + (10 + i) // 12, (10 + i) % 12 + 1, 1) for i in range(10)
    ]
    assert lines[0].due == NOV_1 and lines[-1].due == date(2027, 8, 1)


def test_the_rounding_difference_goes_on_the_last_debit() -> None:
    lines = installments.settlement_lines(D("100.00"), 3, NOV_1, MONTHLY)
    assert [line.amount for line in lines] == [D("33.33"), D("33.33"), D("33.34")]
    assert sum(line.amount for line in lines) == D("100.00")


def test_yearly_is_the_same_day_each_year_and_feb_29_falls_back_to_feb_28() -> None:
    assert installments.schedule_dates(date(2026, 11, 15), YEARLY, 3) == [
        date(2026, 11, 15),
        date(2027, 11, 15),
        date(2028, 11, 15),
    ]
    assert installments.schedule_dates(date(2028, 2, 29), YEARLY, 3) == [
        date(2028, 2, 29),
        date(2029, 2, 28),
        date(2030, 2, 28),
    ]


def test_weekly_is_every_seven_days() -> None:
    dates = installments.schedule_dates(date(2026, 11, 2), WEEKLY, 4)
    assert [later - earlier for earlier, later in zip(dates, dates[1:], strict=False)] == [
        timedelta(days=7)
    ] * 3


@pytest.mark.parametrize(
    ("total", "count", "first", "cadence", "sentence"),
    [
        (
            "10000.00",
            10,
            NOV_1,
            MONTHLY,
            "10 debits of $1,000.00 on the 1st: Nov 1, 2026 … Aug 1, 2027",
        ),
        (
            "100.00",
            3,
            NOV_1,
            MONTHLY,
            "3 debits of $33.33 on the 1st: Nov 1, 2026 … Jan 1, 2027, the last $33.34",
        ),
        (
            "150.00",
            3,
            date(2026, 11, 2),
            WEEKLY,
            "3 debits of $50.00 every week: Nov 2, 2026 … Nov 16, 2026",
        ),
        (
            "3000.00",
            2,
            date(2026, 11, 22),
            YEARLY,
            "2 debits of $1,500.00 on the 22nd: Nov 22, 2026 … Nov 22, 2027",
        ),
        (
            "200.00",
            2,
            date(2026, 11, 3),
            MONTHLY,
            "2 debits of $100.00 on the 3rd: Nov 3, 2026 … Dec 3, 2026",
        ),
        (
            "200.00",
            2,
            date(2026, 11, 13),
            MONTHLY,
            "2 debits of $100.00 on the 13th: Nov 13, 2026 … Dec 13, 2026",
        ),
    ],
)
def test_the_preview_sentence(
    total: str, count: int, first: date, cadence: str, sentence: str
) -> None:
    assert installments.settlement_preview(D(total), count, first, cadence) == sentence


@pytest.mark.parametrize("count", [1, 13])
def test_the_engine_refuses_a_count_outside_two_to_twelve(count: int) -> None:
    with pytest.raises(ValueError):
        installments.settlement_preview(D("100.00"), count, NOV_1, MONTHLY)
    refused = invoice_builder.settlement_refusal(SettlementInput(count, NOV_1, MONTHLY), TODAY)
    assert refused is not None and refused.message == invoice_builder.SETTLEMENT_COUNT


def test_the_cadences_offered_and_the_split_page_unchanged() -> None:
    assert installments.SETTLEMENT_CADENCES == (MONTHLY, WEEKLY, YEARLY)
    assert [value for value, _ in ledger_forms.SETTLEMENT_CADENCE_CHOICES] == [
        MONTHLY,
        WEEKLY,
        YEARLY,
    ]
    # Split into installments offers what it did: no yearly there.
    split = ledger_forms.SplitForm().fields["cadence"]
    assert [value for value, _ in split.choices] == ["weekly", "biweekly", "monthly"]  # type: ignore[attr-defined]
    assert InstallmentCadence.YEARLY.label == "Yearly"
    refused = invoice_builder.settlement_refusal(
        SettlementInput(3, NOV_1, InstallmentCadence.BIWEEKLY), TODAY
    )
    assert refused is not None and refused.field == "settlement_cadence"


# --- The conversion ---------------------------------------------------------------------------


def test_a_settlement_makes_one_manual_charge_with_ten_autopay_installments(
    fake: FakeProvider,
) -> None:
    operator, lease = banked(fake)
    draft = a_draft(operator, lease, ten_thousand())
    made = settle(draft)
    assert made.ok, made.message
    assert made.message == "10 debits scheduled; the first on Nov 1, 2026."
    [parent] = made.charges
    assert parent.collection == CollectionMethod.MANUAL and parent.price == D("10000.00")
    # Nothing of the charge itself is at the provider: no invoice, no job of its own.
    assert not parent.invoices.exists()
    assert not Job.objects.filter(entity_id=parent.pk).exists()
    plan = made.settlement
    assert plan is not None and plan.origin == InstallmentOrigin.SETTLEMENT
    assert (plan.count, plan.cadence, plan.collection) == (
        10,
        MONTHLY,
        InstallmentCollection.AUTOPAY_DUE,
    )
    rows = installments.installments_of(plan)
    assert [row.price for row in rows] == [D("1000.00")] * 10
    assert [row.due_date for row in rows][0] == NOV_1
    assert rows[-1].due_date == date(2027, 8, 1)
    assert {row.collection for row in rows} == {CollectionMethod.AUTOPAY_DUE}
    assert {row.category for row in rows} == {OneOffCategory.INSTALLMENT}
    # Each installment's one-occurrence schedule is queued at once (nothing to release).
    kinds = list(
        Job.objects.filter(entity_id__in=[row.pk for row in rows]).values_list("kind", flat=True)
    )
    assert kinds == [JobKind.SCHEDULE_CREATE] * 10
    assert not InvoiceDraft.objects.filter(pk=draft.pk).exists()
    sent = AuditLog.objects.get(action=invoice_builder.SENT_ACTION, entity_id=str(draft.pk))
    assert sent.after is not None and sent.after["choice"] == "settlement"
    assert sent.after["installment_plan"] == str(plan.pk)
    created = AuditLog.objects.get(action=installments.CREATED_ACTION, entity_id=str(plan.pk))
    assert created.after is not None and created.after["origin"] == "settlement"
    # The plan carries the balance: the parent is out of every open balance.
    assert parent.installment_plans.get() == plan


def test_a_hundred_in_three_carries_the_cent_on_the_last(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    made = settle(a_draft(operator, lease, one_line("100.00")), 3)
    assert made.ok and made.settlement is not None, made.message
    rows = installments.installments_of(made.settlement)
    assert [row.price for row in rows] == [D("33.33"), D("33.33"), D("33.34")]


def test_yearly_and_weekly_settlements_through_the_service(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    yearly = settle(a_draft(operator, lease, one_line("300.00")), 3, date(2026, 11, 15), YEARLY)
    assert yearly.ok and yearly.settlement is not None, yearly.message
    assert [row.due_date for row in installments.installments_of(yearly.settlement)] == [
        date(2026, 11, 15),
        date(2027, 11, 15),
        date(2028, 11, 15),
    ]
    weekly = settle(a_draft(operator, lease, one_line("90.00")), 3, TODAY, WEEKLY)
    assert weekly.ok and weekly.settlement is not None, weekly.message
    assert [row.due_date for row in installments.installments_of(weekly.settlement)] == [
        TODAY,
        TODAY + timedelta(days=7),
        TODAY + timedelta(days=14),
    ]


@pytest.mark.parametrize(
    ("count", "first_on", "cadence", "message"),
    [
        (1, NOV_1, MONTHLY, invoice_builder.SETTLEMENT_COUNT),
        (13, NOV_1, MONTHLY, invoice_builder.SETTLEMENT_COUNT),
        (None, NOV_1, MONTHLY, invoice_builder.SETTLEMENT_COUNT),
        (3, TODAY - timedelta(days=1), MONTHLY, invoice_builder.SETTLEMENT_FIRST_IN_PAST),
        (3, TODAY + timedelta(days=366), MONTHLY, invoice_builder.SETTLEMENT_FIRST_TOO_FAR),
        (3, None, MONTHLY, invoice_builder.SETTLEMENT_FIRST_REQUIRED),
        (3, NOV_1, "biweekly", invoice_builder.SETTLEMENT_CADENCE),
        (3, NOV_1, "daily", invoice_builder.SETTLEMENT_CADENCE),
    ],
)
def test_a_bad_count_date_or_cadence_is_refused_with_nothing_written(
    fake: FakeProvider, count: int | None, first_on: date | None, cadence: str, message: str
) -> None:
    operator, lease = banked(fake)
    draft = a_draft(operator, lease, one_line("300.00"))
    before = written()
    refused = settle(draft, count, first_on, cadence)
    assert (refused.ok, refused.message) == (False, message)
    assert written() == before


def test_two_lines_are_refused_before_any_write(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    draft = a_draft(operator, lease, two_lines())
    before = written()
    refused = settle(draft, 3)
    assert (refused.ok, refused.message) == (False, invoice_builder.SETTLEMENT_ONE_LINE)
    assert refused.message == "Schedule settlement takes an invoice with one line."
    assert written() == before
    assert InvoiceDraft.objects.filter(pk=draft.pk).exists()


def test_a_share_below_a_cent_is_refused_with_nothing_written(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    draft = a_draft(operator, lease, one_line("0.05"))
    before = written()
    refused = settle(draft, 12)
    assert (refused.ok, refused.message) == (False, invoice_builder.SETTLEMENT_TOO_SMALL)
    assert written() == before


def test_no_bank_method_is_refused_by_the_engine_with_nothing_written(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake, customer="cus-nb", bank=None)
    lease = make_lease(operator=operator)
    draft = a_draft(operator, lease, one_line("300.00"))
    before = written()
    refused = settle(draft, 3)
    assert (refused.ok, refused.message) == (False, installments.NO_BANK_METHOD)
    assert written() == before


def test_each_large_debit_needs_the_confirmation(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("500.00")):
        draft = a_draft(operator, lease, one_line("3000.00"))
        before = written()
        refused = settle(draft, 3)
        assert not refused.ok and refused.field == "settlement_confirm_large_debit"
        assert written() == before
        made = settle(draft, 3, confirmed=True)
    assert made.ok, made.message


def test_viewer_cannot_settle_and_a_second_submit_finds_no_draft(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    draft = a_draft(operator, lease, one_line("300.00"))
    refused = settle(draft, 3, role=Role.VIEWER)
    assert (refused.ok, refused.message) == (False, invoice_builder.BILLING_ONLY)
    assert settle(draft, 3).ok
    again = settle(draft, 3)
    assert (again.ok, again.message) == (False, invoice_builder.NOT_A_DRAFT)
    assert InstallmentPlan.objects.count() == 1


def test_the_plan_completes_and_the_parent_is_paid_through_its_installments(
    fake: FakeProvider,
) -> None:
    operator, lease = banked(fake)
    made = settle(a_draft(operator, lease, one_line("100.00")), 2)
    assert made.ok and made.settlement is not None
    for row in installments.installments_of(made.settlement):
        paid = one_off_service.record_payment(
            row.pk,
            method="wire",
            amount=row.price,
            paid_on=TODAY,
            note="Wire received.",
            actor=user(Role.BILLING),
            today=TODAY,
        )
        assert paid.ok, paid.message
    [parent] = made.charges
    parent.refresh_from_db()
    assert parent.status == OneOffChargeStatus.PAID


# --- The preview route --------------------------------------------------------------------------


def preview_query(**params: str) -> str:
    values = {"total": "10000.00", "count": "10", "first": "2026-11-01", "cadence": "monthly"}
    values.update(params)
    return PREVIEW + "?" + "&".join(f"{key}={value}" for key, value in values.items())


def test_the_preview_route_answers_billing_with_the_sentence_and_writes_nothing(
    fake: FakeProvider,
) -> None:
    assert reverse("ledger:invoices:settlement_preview") == PREVIEW
    before = (written(), AuditLog.objects.count())
    response = client_for(Role.BILLING)[0].get(preview_query())
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "sentence": "10 debits of $1,000.00 on the 1st: Nov 1, 2026 … Aug 1, 2027",
    }
    assert (written(), AuditLog.objects.count()) == before


def test_the_preview_route_refuses_a_viewer_and_a_signed_out_visitor() -> None:
    assert client_for(Role.VIEWER)[0].get(preview_query()).status_code == 403
    from django.test import Client

    assert Client().get(preview_query()).status_code == 401
    # Registered read-only: an unsafe method has no rule.
    assert "ledger:invoices:settlement_preview" in registry.READ_ONLY_ROUTE_NAMES
    assert client_for(Role.BILLING)[0].post(preview_query()).status_code in (403, 405)


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"total": ""}, invoice_builder.SETTLEMENT_TOTAL),
        ({"total": "abc"}, invoice_builder.SETTLEMENT_TOTAL),
        ({"total": "-5"}, invoice_builder.SETTLEMENT_TOTAL),
        ({"total": "0"}, invoice_builder.SETTLEMENT_TOTAL),
        ({"total": "1.234"}, invoice_builder.SETTLEMENT_TOTAL),
        ({"total": "NaN"}, invoice_builder.SETTLEMENT_TOTAL),
        ({"total": "10%0A"}, invoice_builder.SETTLEMENT_TOTAL),
        ({"count": "3%0A"}, invoice_builder.SETTLEMENT_COUNT),
        ({"total": "1000000.00"}, invoice_builder.SETTLEMENT_TOTAL),
        ({"count": "1"}, invoice_builder.SETTLEMENT_COUNT),
        ({"count": "13"}, invoice_builder.SETTLEMENT_COUNT),
        ({"count": "x"}, invoice_builder.SETTLEMENT_COUNT),
        ({"count": "999999999999"}, invoice_builder.SETTLEMENT_COUNT),
        ({"first": "2026-10-05"}, invoice_builder.SETTLEMENT_FIRST_IN_PAST),
        ({"first": "2027-12-01"}, invoice_builder.SETTLEMENT_FIRST_TOO_FAR),
        ({"first": "2026-02-30"}, invoice_builder.SETTLEMENT_FIRST_REQUIRED),
        ({"first": "<script>"}, invoice_builder.SETTLEMENT_FIRST_REQUIRED),
        ({"first": ""}, invoice_builder.SETTLEMENT_FIRST_REQUIRED),
        ({"cadence": "biweekly"}, invoice_builder.SETTLEMENT_CADENCE),
        ({"cadence": ""}, invoice_builder.SETTLEMENT_CADENCE),
        ({"total": "0.05", "count": "12"}, invoice_builder.SETTLEMENT_TOO_SMALL),
    ],
)
def test_the_preview_route_refuses_each_bad_parameter(params: dict[str, str], message: str) -> None:
    response = client_for(Role.BILLING)[0].get(preview_query(**params))
    assert response.status_code == 400
    # The preview answers its own short sentence for the form's (finding D2-2, slice D-3).
    short = invoice_builder.SETTLEMENT_PREVIEW_SENTENCES.get(message, message)
    assert response.json() == {"ok": False, "message": short}
    assert "Nothing was scheduled" not in short


# --- The page ---------------------------------------------------------------------------------


def settlement_post(lease: Lease, **fields: str) -> dict[str, str]:
    return {
        **page_post(
            lease,
            collect="settlement",
            settlement_count="10",
            settlement_first_on="2026-11-01",
            settlement_cadence="monthly",
        ),
        "lines-TOTAL_FORMS": "1",
        "lines-0-item": "Settlement of arrears",
        "lines-0-unit_amount": "10000.00",
        **fields,
    }


def test_the_tile_its_fields_and_its_button(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    assert [value for value, _ in ledger_forms.COLLECT_CHOICES][-1] == "settlement"
    assert dict(ledger_forms.COLLECT_CHOICES)["settlement"] == "Schedule settlement"
    html = draft_html(a_draft(operator, lease, one_line("300.00")))
    assert greyed(html, "settlement") == ""
    assert '<span class="collect-name">Schedule settlement</span>' in html
    assert 'id="collect-settlement-fields"' in html
    for name in ("settlement_count", "settlement_first_on", "settlement_cadence"):
        control = re.search(rf'<[^>]*id="id_{name}"[^>]*>', html)
        assert control is not None and 'data-shown-when="collect-settlement"' in control.group()
    cadence = re.search(r'<select name="settlement_cadence"[^>]*>(.*?)</select>', html, re.S)
    assert cadence is not None
    assert re.findall(r'value="(\w+)"', cadence.group(1)) == [MONTHLY, WEEKLY, YEARLY]
    assert 'id="settlement-preview" data-preview-url="/invoices/settlement-preview.json"' in html
    assert (
        'id="settlement-draft" data-shown-when="collect-settlement">Schedule '
        '<span class="collect-count">3</span> debits</button>'
    ) in html


def test_the_page_schedules_and_lands_on_the_charge(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    client = client_for(Role.BILLING)[0]
    response = client.post(f"/invoices/new/store/{lease.store_id}/", settlement_post(lease))
    plan = InstallmentPlan.objects.get()
    parent = plan.parent
    assert response.status_code == 302
    assert response["Location"] == reverse(
        "ledger:one_off_charges:detail", kwargs={"pk": parent.pk}
    )
    page = client.get(response["Location"]).content.decode()
    assert "10 debits scheduled; the first on Nov 1, 2026." in page
    assert plan.origin == InstallmentOrigin.SETTLEMENT and plan.count == 10


def test_a_refusal_reopens_the_form_with_the_sentence_server_side(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    client = client_for(Role.BILLING)[0]
    with override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("500.00")):
        response = client.post(f"/invoices/new/store/{lease.store_id}/", settlement_post(lease))
        draft = InvoiceDraft.objects.get()
        assert not InstallmentPlan.objects.exists() and not OneOffCharge.objects.exists()
        page = client.get(response["Location"]).content.decode()
    sentence = "10 debits of $1,000.00 on the 1st: Nov 1, 2026 … Aug 1, 2027"
    assert f'aria-live="polite">{sentence}</p>' in page
    assert '<span class="collect-count">10</span> debits' in page
    assert f"/invoices/drafts/{draft.pk}/" in response["Location"]


def test_the_page_refuses_a_count_outside_the_range_before_anything_is_converted(
    fake: FakeProvider,
) -> None:
    operator, lease = banked(fake)
    client = client_for(Role.BILLING)[0]
    for count in ("1", "13"):
        client.post(
            f"/invoices/new/store/{lease.store_id}/", settlement_post(lease, settlement_count=count)
        )
    assert not InstallmentPlan.objects.exists() and not OneOffCharge.objects.exists()


def test_the_tile_is_greyed_with_no_bank_and_not_by_the_pay_flag(fake: FakeProvider) -> None:
    no_bank = str(one_off_service.DEBIT_NO_METHOD)
    operator = an_operator(fake, customer="cus-nb", bank=None)
    lease = make_lease(operator=operator)
    html = draft_html(a_draft(operator, lease, one_line()))
    assert greyed(html, "settlement") == no_bank
    assert 'id="settlement-draft"' not in html and 'id="collect-settlement-fields"' not in html
    # The pay-an-invoice flag gates Charge now's call alone; the debits are schedules
    # (D-1's rule for Schedule for a date and Set up autopay).
    banked_operator, banked_lease = banked(fake)
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        flag_off = draft_html(a_draft(banked_operator, banked_lease, one_line()), Role.ADMIN)
    assert greyed(flag_off, "charge_now") == str(one_off_service.DEBIT_OFF)
    assert greyed(flag_off, "settlement") == ""
    # Two lines: the lease-and-one-line rule.
    two = draft_html(a_draft(banked_operator, banked_lease, two_lines()))
    assert greyed(two, "settlement") == invoice_builder.SCHEDULE_NOT_OFFERED


def test_a_posted_greyed_settlement_is_refused_and_nothing_is_written(fake: FakeProvider) -> None:
    operator = an_operator(fake, customer="cus-nb", bank=None)
    lease = make_lease(operator=operator)
    before = written()
    client_for(Role.BILLING)[0].post(
        f"/invoices/new/store/{lease.store_id}/", settlement_post(lease)
    )
    assert written() == before


# --- The labels ---------------------------------------------------------------------------------


def test_plan_label_is_the_one_wording(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    made = settle(a_draft(operator, lease, one_line("300.00")), 3)
    assert made.settlement is not None
    assert installments.plan_label(made.settlement, 2) == "Settlement plan · 2 of 3"
    split = InstallmentPlan(count=4, origin=InstallmentOrigin.SPLIT)
    assert installments.plan_label(split, 2) == "Installment 2 of 4"
    assert InstallmentPlan._meta.get_field("origin").default == InstallmentOrigin.SPLIT


def test_the_labels_on_the_four_surfaces(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    made = settle(a_draft(operator, lease, one_line("300.00")), 3)
    assert made.settlement is not None
    [parent] = made.charges
    rows = installments.installments_of(made.settlement)
    paid = one_off_service.record_payment(
        rows[0].pk,
        method="wire",
        amount=rows[0].price,
        paid_on=TODAY,
        note="Wire received.",
        actor=user(Role.BILLING),
        today=TODAY,
    )
    assert paid.ok, paid.message
    client = client_for(Role.BILLING)[0]
    # The charge page: the plan card.
    charge_page = client.get(
        reverse("ledger:one_off_charges:detail", kwargs={"pk": parent.pk})
    ).content.decode()
    assert "<h2>Settlement plan</h2>" in charge_page
    assert "Settlement plan · 1 of 3 paid, $100.00 of $300.00" in charge_page
    assert "Installment plan:" not in charge_page
    # A line's own page.
    line_page = client.get(
        reverse("ledger:one_off_charges:detail", kwargs={"pk": rows[2].pk})
    ).content.decode()
    found = re.search(r'<p id="installment-of">(.*?)</p>', line_page, re.S)
    assert found is not None and "Settlement plan · 3 of 3" in found.group(1)
    assert "installment plan</a>" not in found.group(1)
    # The store's Autopay tab and Charges tab.
    store = lease.store_id
    autopay_tab = client.get(f"/stores/{store}/?tab=schedules").content.decode()
    plans = autopay_tab[autopay_tab.index('id="store-plans"') :]
    assert "Settlement plan · 1 of 3, monthly" in plans[: plans.index("</table>")]
    charges_tab = client.get(f"/stores/{store}/?tab=charges&filter=all").content.decode()
    charges = charges_tab[charges_tab.index('id="store-charges"') :]
    charges = charges[: charges.index("</table>")]
    for number in (1, 2, 3):
        assert f'<span class="charge-kind">Settlement plan · {number} of 3</span>' in charges


def test_a_split_plan_keeps_its_words(fake: FakeProvider) -> None:
    operator, lease = banked(fake)
    charge = one_off_service.create_charge(
        operator,
        lease,
        one_off_service.ChargeInput(
            fee_type=None,
            price=D("300.00"),
            cost=None,
            vendor="",
            due_date=NOV_1,
            note="Arrears.",
            category=OneOffCategory.ARREARS,
            collection=CollectionMethod.MANUAL,
            description="Arrears",
        ),
        user(Role.BILLING),
        today=TODAY,
        with_invoice=False,
    ).charge
    assert charge is not None
    lines, refused = installments.preview(charge, 3, NOV_1, MONTHLY, today=TODAY)
    assert refused is None
    made = installments.create_plan(charge, 3, NOV_1, MONTHLY, lines, None, today=TODAY)
    assert made.ok and made.plan is not None and made.plan.origin == InstallmentOrigin.SPLIT
    page = (
        client_for(Role.BILLING)[0]
        .get(reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk}))
        .content.decode()
    )
    assert "<h2>Installment plan</h2>" in page and "Installment plan: 0 of 3 paid" in page
    # Split's form does not offer yearly (Schedule settlement's).
    form = ledger_forms.SplitForm({"count": "3", "first_due": "2026-11-01", "cadence": YEARLY})
    assert not form.is_valid() and "cadence" in form.errors


# --- The migration and the guard lists ----------------------------------------------------------


def _migrate(target: tuple[str, str]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])


def _plan_table() -> str:
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, "installment_plans")
        columns = [
            column.name
            for column in connection.introspection.get_table_description(
                cursor, "installment_plans"
            )
        ]
        names = sorted(name for name in constraints if name.startswith("installment_plans_"))
        if connection.vendor == "sqlite":
            cursor.execute("SELECT sql FROM sqlite_master WHERE name = 'installment_plans'")
            text = str(cursor.fetchone()[0])
        else:
            cursor.execute(
                "SELECT string_agg(pg_get_constraintdef(oid), ' ') FROM pg_constraint "
                "WHERE conrelid = 'installment_plans'::regclass"
            )
            text = str(cursor.fetchone()[0])
    return json.dumps({"columns": columns, "constraints": names}) + text


@pytest.mark.django_db(transaction=True)
def test_migration_0054_adds_yearly_and_origin_and_reverses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        _migrate(("ledger", "0053_cash_method"))
        before = _plan_table()
        assert "'yearly'" not in before and '"origin"' not in before.split("]")[0]
        _migrate(("ledger", "0054_installment_settlement"))
        after = _plan_table()
        assert "'yearly'" in after and "'settlement'" in after
        assert "installment_plans_origin_valid" in after
        _migrate(("ledger", "0053_cash_method"))
        assert "'yearly'" not in _plan_table()
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaves)
    assert "'yearly'" in _plan_table()


def test_migration_0054_guards_no_column_and_is_on_both_guard_lists() -> None:
    module = importlib.import_module("ledger.migrations.0054_installment_settlement")
    assert module.GUARDED_COLUMNS == {}
    for path in ("test_card_number_guard.py", "test_migrations_roundtrip.py"):
        text = (registry_tests_dir() / path).read_text(encoding="utf-8")
        assert '"ledger.migrations.0054_installment_settlement",' in text, path


def registry_tests_dir() -> Any:
    from pathlib import Path

    return Path(__file__).resolve().parent
