"""PLAN-015j tasks 1 and 2: a "When" per Move-in charges row of New lease — with the first
invoice (the default, as before), due on a date, or in installments — for the deposit and for
every other one-time charge (docs/qa/15/PLAN-015j-move-in-charge-timing.md §4, §6, §9, §10).

Today is 2026-11-03 (the New lease page tests' ``today``); the lease starts 2026-11-15 and is
saved prospective; rent $2,000.00, prorated (Nov 15-30: $1,066.67)."""

from __future__ import annotations

import html
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from ledger.forms import SETUP_DUE_REQUIRED, LeaseCreateForm
from ledger.models import (
    InstallmentCollection,
    InstallmentOrigin,
    InstallmentPlan,
    InvoiceDraft,
    Lease,
    MessageRule,
    OneOffCategory,
    OneOffCharge,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services import (
    charge_timing,
    deposits,
    installments,
    invoice_builder,
    one_off_charges,
    pnl,
    receivables,
    reminders,
    workspace,
)
from ledger.services.charge_timing import ChargeTiming, When
from tests import test_new_lease_page as new_lease
from tests.test_new_lease_page import (
    create_data,
    create_path,
    page,
    post_create,
    shared_world,
    vacant_store,
)

pytestmark = pytest.mark.django_db

D = Decimal
today = new_lease.today
admin = new_lease.admin
TODAY = new_lease.TODAY  # 2026-11-03
START = date(2026, 11, 15)
DEP = "setup_security_deposit"
FEE = "setup_sign_fee"
ACCESS = "setup_access_fee"
FIRST_MONTH = D("1066.67")


def priced(**fields: str) -> dict[str, str]:
    """A $3,000.00 deposit and a $500.00 sign fee, with ``fields``."""
    return {f"{DEP}_price": "3000.00", f"{FEE}_price": "500.00", **fields}


def save(client: Client, operator: Any = None, **fields: str) -> Lease | None:
    """The New lease POST; the lease saved, or ``None`` when the page refused it."""
    store = vacant_store()
    who = operator or new_lease.an_operator_with("Timing Person", email="timing@example.com")
    post_create(client, store, create_data(store, who, **fields))
    return Lease.objects.filter(store=store).first()


def charge(lease: Lease, code: str) -> OneOffCharge:
    return OneOffCharge.objects.get(lease=lease, fee_type__code=code, plan__isnull=True)


def draft_lines(lease: Lease) -> list[Any]:
    return invoice_builder.lines_of(InvoiceDraft.objects.get(lease=lease))


def draft_charge_ids(lease: Lease) -> list[Any]:
    return [line.existing_charge_id for line in draft_lines(lease)[1:]]


def draft_total(lease: Lease) -> Decimal:
    return invoice_builder.total_of(draft_lines(lease))


def fee_type_pk(code: str) -> Any:
    from ledger.models import FeeType

    return FeeType.objects.get(code=code).pk


def plan_of(item: OneOffCharge) -> InstallmentPlan:
    return InstallmentPlan.objects.get(one_off_charge=item)


def bound(**fields: str) -> LeaseCreateForm:
    store = vacant_store(900 + len(fields), "Form Store")
    operator = new_lease.an_operator_with("Form Person", email="form@example.com")
    data = create_data(store, operator, **fields)
    data["store"] = str(store.pk)
    return LeaseCreateForm(data, today=TODAY)


def on_date(prefix: str, on: str) -> dict[str, str]:
    return {f"{prefix}_when": "on_date", f"{prefix}_on": on}


def in_installments(
    prefix: str, count: str, first: str, cadence: str = "monthly"
) -> dict[str, str]:
    return {
        f"{prefix}_when": "installments",
        f"{prefix}_count": count,
        f"{prefix}_first_due": first,
        f"{prefix}_cadence": cadence,
    }


def _rules_for(item: OneOffCharge, day: date) -> list[str]:
    return [
        found.rule.code
        for found in reminders.plan(day)
        if found.row is not None and found.row.charge_id == item.pk
    ]


# --- The type, the labels, the preview sentence ----------------------------------------------


def test_the_three_choices_their_labels_and_the_cadences() -> None:
    assert [value for value, _ in charge_timing.WHEN_CHOICES] == [
        "with_invoice",
        "on_date",
        "installments",
    ]
    assert [label for _, label in charge_timing.WHEN_CHOICES] == [
        "With the first invoice",
        "Due on a date",
        "In installments",
    ]
    assert charge_timing.CADENCES == [
        ("monthly", "Monthly"),
        ("biweekly", "Every two weeks"),
        ("weekly", "Weekly"),
    ]
    assert ChargeTiming() == charge_timing.WITH_INVOICE
    assert ChargeTiming().on_invoice and ChargeTiming().due_date is None
    assert ChargeTiming(When.ON_DATE, due_on=START).due_date == START
    assert ChargeTiming(When.INSTALLMENTS, count=3, first_due=TODAY).due_date == TODAY
    assert not hasattr(deposits, "DepositTiming") and not hasattr(deposits, "FIRST_BEFORE_START")


@pytest.mark.parametrize(
    ("total", "count", "first", "cadence", "sentence"),
    [
        (
            "1500.00",
            3,
            date(2026, 12, 1),
            "monthly",
            "3 payments of $500.00: Dec 1, 2026 … Feb 1, 2027",
        ),
        (
            "1000.00",
            3,
            date(2026, 12, 1),
            "monthly",
            "3 payments of $333.33: Dec 1, 2026 … Feb 1, 2027, the last $333.34",
        ),
        (
            "150.00",
            3,
            date(2026, 11, 2),
            "weekly",
            "3 payments of $50.00 every week: Nov 2, 2026 … Nov 16, 2026",
        ),
        (
            "100.00",
            2,
            date(2026, 11, 2),
            "biweekly",
            "2 payments of $50.00 every two weeks: Nov 2, 2026 … Nov 16, 2026",
        ),
        (
            "0.24",
            12,
            date(2026, 1, 31),
            "monthly",
            "12 payments of $0.02: Jan 31, 2026 … Dec 31, 2026",
        ),
    ],
)
def test_the_payments_sentence(
    total: str, count: int, first: date, cadence: str, sentence: str
) -> None:
    assert installments.payments_preview(D(total), count, first, cadence) == sentence
    # The sentence's amounts are the plan's own (settlement_lines, the engine's split).
    lines = installments.settlement_lines(D(total), count, first, cadence)
    assert sum(line.amount for line in lines) == D(total)


@pytest.mark.parametrize(("total", "count"), [("1000.00", 1), ("1000.00", 13), ("0.01", 12)])
def test_the_payments_sentence_refuses_what_the_engine_refuses(total: str, count: int) -> None:
    with pytest.raises(ValueError):
        installments.payments_preview(D(total), count, date(2026, 12, 1), "monthly")


# --- Unchanged behaviour ---------------------------------------------------------------------


def test_a_post_with_no_when_field_saves_what_main_saved(admin: Client, today: date) -> None:
    """§6 "Unchanged behaviour", §8.3: both charges due on the start, not dated, both lines of
    the move-in draft after the rent line; no plan (main's rows, pinned field by field)."""
    lease = save(admin, **priced())
    assert lease is not None
    rows: list[tuple[Any, ...]] = list(
        OneOffCharge.objects.filter(lease=lease)
        .order_by("fee_type__sort_order", "fee_type__name")
        .values_list(
            "fee_type__code",
            "category",
            "collection",
            "price",
            "due_date",
            "dated",
            "status",
            "plan",
        )
    )
    expected: list[tuple[Any, ...]] = sorted(
        [
            ("security_deposit", "setup", "none", D("3000.00"), START, False, "owed", None),
            ("sign_fee", "setup", "none", D("500.00"), START, False, "owed", None),
        ]
    )
    assert sorted(rows) == expected
    lines = draft_lines(lease)
    assert (lines[0].rent_lease_id, lines[0].rent_month) == (lease.pk, date(2026, 11, 1))
    assert set(draft_charge_ids(lease)) == set(
        OneOffCharge.objects.filter(lease=lease).values_list("pk", flat=True)
    )
    assert draft_total(lease) == FIRST_MONTH + D("3500.00")
    assert InvoiceDraft.objects.get(lease=lease).due_date == START
    assert not InstallmentPlan.objects.exists()
    form = bound(**priced())
    assert form.is_valid(), form.errors
    assert set(form.timings().values()) == {charge_timing.WITH_INVOICE}
    assert [(item.due_date, item.dated) for item in form.setup_items()] == [(START, False)] * 2


def test_an_existing_dated_deposit_and_deposit_plan_read_as_before(
    admin: Client, today: date
) -> None:
    """§6: the lease page's words of the deposit are 015d's (``deposits.card_text``)."""
    dated = save(admin, **priced(**on_date(DEP, "2026-12-01")))
    assert dated is not None
    assert deposits.card_text(dated) == "$3,000.00 · due Dec 1"
    assert workspace._deposit_text(dated)[0] == "$3,000.00 · due Dec 1"
    store = vacant_store(438, "Plan Store")
    operator = new_lease.an_operator_with("Plan Person", email="plan@example.com")
    post_create(
        admin,
        store,
        create_data(store, operator, **priced(**in_installments(DEP, "3", "2026-11-15"))),
    )
    planned = Lease.objects.get(store=store)
    assert deposits.card_text(planned) == "$3,000.00 · 0 of 3 installments paid"
    assert workspace._deposit_text(planned)[0] == "$3,000.00 · 0 of 3 installments paid"


# --- Happy paths: the deposit and a non-deposit row each ----------------------------------


@pytest.mark.parametrize(("prefix", "code"), [(DEP, "security_deposit"), (FEE, "sign_fee")])
def test_with_the_first_invoice_the_charge_is_a_move_in_line(
    admin: Client, today: date, prefix: str, code: str
) -> None:
    lease = save(admin, **priced(**{f"{prefix}_when": "with_invoice"}))
    assert lease is not None
    item = charge(lease, code)
    assert (item.due_date, item.dated) == (START, False)
    assert item.pk in draft_charge_ids(lease)
    assert charge_timing.lease_words(item) == ""


@pytest.mark.parametrize(("prefix", "code"), [(DEP, "security_deposit"), (FEE, "sign_fee")])
def test_due_on_a_date_is_its_own_dated_charge_off_the_draft(
    admin: Client, today: date, prefix: str, code: str
) -> None:
    lease = save(admin, **priced(**on_date(prefix, "2026-12-15")))
    assert lease is not None
    item = charge(lease, code)
    assert (item.due_date, item.dated, item.price) == (
        date(2026, 12, 15),
        True,
        D("3000.00") if code == "security_deposit" else D("500.00"),
    )
    assert item.pk not in draft_charge_ids(lease)
    assert not InstallmentPlan.objects.exists()
    assert charge_timing.lease_words(item) == "due Dec 15, 2026"
    # A receivable by that date: the reminder ladder follows it (B three days before).
    if code == "security_deposit":
        assert [row.charge_id for row in receivables.dated_deposits()].count(item.pk) == 1
    else:
        found = [row for row in receivables.open_balances(TODAY) if row.charge_id == item.pk]
        assert [(row.due_on, row.balance) for row in found] == [(date(2026, 12, 15), D("500.00"))]
    assert _rules_for(item, date(2026, 12, 12)) == [MessageRule.B]
    assert _rules_for(item, date(2026, 12, 13)) == []


@pytest.mark.parametrize(
    ("prefix", "code", "origin"),
    [
        (DEP, "security_deposit", InstallmentOrigin.DEPOSIT),
        (FEE, "sign_fee", InstallmentOrigin.SPLIT),
    ],
)
def test_in_installments_makes_a_plan_off_the_draft(
    admin: Client, today: date, prefix: str, code: str, origin: str
) -> None:
    fields = priced(**{f"{FEE}_price": "1000.00"}, **in_installments(prefix, "3", "2026-12-01"))
    lease = save(admin, **fields)
    assert lease is not None
    item = charge(lease, code)
    plan = plan_of(item)
    assert (plan.origin, plan.count, plan.first_due, plan.cadence, plan.collection) == (
        origin,
        3,
        date(2026, 12, 1),
        "monthly",
        InstallmentCollection.MANUAL,
    )
    rows = installments.installments_of(plan)
    price = D(item.price)
    share = (price / 3).quantize(D("0.01"), rounding="ROUND_DOWN")
    assert [(row.due_date, row.price) for row in rows] == [
        (date(2026, 12, 1), share),
        (date(2027, 1, 1), share),
        (date(2027, 2, 1), price - 2 * share),
    ]
    assert sum(row.price for row in rows) == price
    assert {row.category for row in rows} == {OneOffCategory.INSTALLMENT}
    # The charge's own due date is the first installment's; it is not dated.
    assert (item.due_date, item.dated) == (date(2026, 12, 1), False)
    assert item.pk not in draft_charge_ids(lease)
    assert charge_timing.lease_words(item) == "0 of 3 installments paid"


def test_a_sign_fee_in_three_installments_of_1000_carries_the_cent_on_the_last(
    admin: Client, today: date
) -> None:
    lease = save(
        admin, **priced(**{f"{FEE}_price": "1000.00"}, **in_installments(FEE, "3", "2026-12-01"))
    )
    assert lease is not None
    rows = installments.installments_of(plan_of(charge(lease, "sign_fee")))
    assert [row.price for row in rows] == [D("333.33"), D("333.33"), D("333.34")]


def test_a_plan_on_a_linked_account_is_debited_by_the_bank_for_every_charge(
    admin: Client, today: date
) -> None:
    """§9.4: installments of every charge are collected by the deposit's rule."""
    holder, operator = shared_world()
    lease = save(
        admin,
        operator,
        account=f"same:{holder.pk}",
        **priced(
            **in_installments(DEP, "2", "2026-11-15"), **in_installments(FEE, "2", "2026-11-20")
        ),
    )
    assert lease is not None
    assert plan_of(charge(lease, "security_deposit")).collection == (
        InstallmentCollection.AUTOPAY_DUE
    )
    assert plan_of(charge(lease, "sign_fee")).collection == InstallmentCollection.AUTOPAY_DUE


def test_the_charge_pages_own_split_still_collects_as_its_parent(
    admin: Client, today: date
) -> None:
    """§3 Out: Split into installments on a charge's page is unchanged — a setup charge's
    plan is collected as the charge is ("manual"), origin ``split``, on a linked lease too."""
    holder, operator = shared_world()
    lease = save(admin, operator, account=f"same:{holder.pk}", **priced())
    assert lease is not None
    fee = charge(lease, "sign_fee")
    InvoiceDraft.objects.filter(lease=lease).delete()
    lines, refused = installments.preview(fee, 2, date(2026, 12, 1), "monthly", TODAY)
    assert refused is None
    made = installments.create_plan(
        fee, 2, date(2026, 12, 1), "monthly", lines, None, confirmed=True, today=TODAY
    )
    assert made.ok, made.message
    assert (made.plan.origin, made.plan.collection) == (  # type: ignore[union-attr]
        InstallmentOrigin.SPLIT,
        InstallmentCollection.MANUAL,
    )
    assert installments.split_refusal(charge(lease, "security_deposit"), TODAY) == (
        installments.DEPOSIT
    )


# --- Mixed ---------------------------------------------------------------------------------


MIXED = {
    f"{DEP}_price": "3000.00",
    f"{FEE}_price": "500.00",
    f"{ACCESS}_price": "250.00",
    **in_installments(DEP, "3", "2026-12-01"),
    **on_date(FEE, "2026-12-15"),
    f"{ACCESS}_when": "with_invoice",
}


def test_three_rows_three_choices_on_one_lease(admin: Client, today: date) -> None:
    lease = save(admin, **MIXED)
    assert lease is not None
    deposit, fee, access = (
        charge(lease, code) for code in ("security_deposit", "sign_fee", "access_fee")
    )
    assert draft_charge_ids(lease) == [access.pk]
    # The first invoice is the rent plus only the rows with the first invoice.
    assert draft_total(lease) == FIRST_MONTH + D("250.00")
    assert plan_of(deposit).origin == InstallmentOrigin.DEPOSIT
    assert (fee.dated, fee.due_date) == (True, date(2026, 12, 15))
    assert (access.dated, access.due_date) == (False, START)
    assert InstallmentPlan.objects.count() == 1


def test_the_summary_counts_only_the_first_invoice_and_lists_the_others(today: date) -> None:
    form = bound(**MIXED)
    assert form.is_valid(), form.errors
    summary = form.summary()
    assert summary["move_in"] == "$250.00"
    assert summary["total"] == "$1,316.67"
    assert summary["timed"] == [
        "Security deposit · $3,000.00 · 3 payments from Dec 1, 2026",
        "Sign fee · $500.00 · due Dec 15, 2026",
    ]
    assert form.setup_total() == D("3750.00")


def test_the_summary_of_a_form_with_no_when_lists_nothing_off_the_invoice(today: date) -> None:
    form = bound(**priced())
    form.is_valid()
    summary = form.summary()
    assert (summary["move_in"], summary["timed"]) == ("$3,500.00", [])


def test_the_page_re_rendered_shows_the_summary_lines_and_the_payments_sentence(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    operator = new_lease.an_operator_with("Rerender", email="rerender@example.com")
    # Refused (no rent), so the page is drawn again from what was posted.
    post_create(admin, store, create_data(store, operator, rent="", **MIXED))
    text = page(admin, create_path(store))
    assert 'id="setup-total">$250.00<' in text
    assert "<li>Sign fee · $500.00 · due Dec 15, 2026</li>" in text
    assert "<li>Security deposit · $3,000.00 · 3 payments from Dec 1, 2026</li>" in text
    assert (
        'id="setup-security_deposit-preview">3 payments of $1,000.00: Dec 1, 2026 … Feb 1, 2027<'
        in text
    )


def test_the_page_has_a_when_per_row_and_no_due_date_field(admin: Client, today: date) -> None:
    text = page(admin, create_path(vacant_store()))
    for prefix in (DEP, FEE, ACCESS):
        assert f'name="{prefix}_when"' in text
        assert f'name="{prefix}_due_date"' not in text
        for part in ("on", "count", "first_due", "cadence"):
            assert f'name="{prefix}_{part}"' in text
    assert text.count(">With the first invoice</option>") >= 3
    assert ">Due on a date</option>" in text and ">In installments</option>" in text
    assert 'name="deposit_when"' not in text and 'id="deposit-when"' not in text
    # §9.5: the hint beside "Due on a date" on rows that are not the deposit.
    hint = html.escape(charge_timing.NOT_ON_INVOICE)
    sign = text[text.index('id="setup-sign_fee"') : text.index('id="setup-sign_fee-when"') + 3000]
    assert hint in sign
    deposit = text[text.index('id="setup-security_deposit"') : text.index('id="setup-sign_fee"')]
    assert hint not in deposit


# --- Boundaries ----------------------------------------------------------------------------


@pytest.mark.parametrize(("count", "ok"), [("1", False), ("2", True), ("12", True), ("13", False)])
@pytest.mark.parametrize("prefix", [DEP, FEE])
def test_the_count_is_2_to_12(today: date, prefix: str, count: str, ok: bool) -> None:
    form = bound(**priced(**in_installments(prefix, count, "2026-12-01")))
    assert form.is_valid() is ok
    if not ok:
        assert list(form.errors) == [f"{prefix}_count"]


@pytest.mark.parametrize("prefix", [DEP, FEE])
@pytest.mark.parametrize("when", ["on_date", "installments"])
@pytest.mark.parametrize(
    ("day", "ok"),
    [("2026-11-03", True), ("2026-11-10", True), ("2026-11-02", False)],
    ids=["today", "before-the-start", "yesterday"],
)
def test_a_date_from_today_is_accepted_also_before_the_start(
    admin: Client, today: date, prefix: str, when: str, day: str, ok: bool
) -> None:
    """§10: "Due on a date" and a first installment may be any date from today on, also
    before the lease's start; yesterday is refused, and nothing is saved."""
    timing = on_date(prefix, day) if when == "on_date" else in_installments(prefix, "2", day)
    lease = save(admin, **priced(**timing))
    if not ok:
        assert lease is None
        assert not OneOffCharge.objects.exists() and not InvoiceDraft.objects.exists()
        form = bound(**priced(**timing))
        assert not form.is_valid()
        field = f"{prefix}_on" if when == "on_date" else f"{prefix}_first_due"
        expected = (
            charge_timing.DUE_BEFORE_TODAY if when == "on_date" else installments.FIRST_IN_PAST
        )
        assert form.errors == {field: [expected]}
        return
    assert lease is not None and lease.collection_status == "prospective"
    code = "security_deposit" if prefix == DEP else "sign_fee"
    item = charge(lease, code)
    assert item.due_date == date.fromisoformat(day)
    assert item.pk not in draft_charge_ids(lease)
    if when == "installments":
        assert plan_of(item).first_due == date.fromisoformat(day)


def test_a_cent_in_twelve_installments_is_refused_by_the_engine_naming_the_row(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    operator = new_lease.an_operator_with("Cent Person", email="cent@example.com")
    data = create_data(
        store,
        operator,
        **priced(**{f"{FEE}_price": "0.01"}, **in_installments(FEE, "12", "2026-12-01")),
    )
    response = post_create(admin, store, data)
    assert response.status_code == 302
    # The whole save is rolled back: no lease, no charge, no plan, no draft.
    assert not Lease.objects.filter(store=store).exists()
    assert not OneOffCharge.objects.exists()
    assert not InstallmentPlan.objects.exists() and not InvoiceDraft.objects.exists()
    expected = f"Sign fee: {installments.CENT_TOO_SMALL} The lease was not saved."
    assert html.escape(expected) in page(admin, create_path(store))


def test_the_service_refuses_a_plan_for_a_lease_without_an_operator_and_saves_nothing(
    today: date,
) -> None:
    form = bound(**priced(**in_installments(FEE, "2", "2026-12-01")))
    assert form.is_valid(), form.errors
    fields = {**form.cleaned_data, "operator": None}
    with pytest.raises(one_off_charges.SetupRefused) as raised:
        charge_timing.create_lease(
            fields,
            rent=form.cleaned_data["rent"],
            creation_key=uuid.uuid4(),
            items=form.setup_items(),
            actor=None,
            timings=form.timings(),
            today=TODAY,
        )
    assert raised.value.fee_type is not None and raised.value.fee_type.code == "sign_fee"
    assert raised.value.message == installments.NO_OPERATOR
    assert not Lease.objects.exists() and not OneOffCharge.objects.exists()
    assert not InstallmentPlan.objects.exists()


def test_the_service_refuses_installments_without_a_first_date_naming_the_row(
    today: date,
) -> None:
    form = bound(**priced())
    assert form.is_valid(), form.errors
    with pytest.raises(one_off_charges.SetupRefused) as raised:
        charge_timing.create_lease(
            form.cleaned_data,
            rent=form.cleaned_data["rent"],
            creation_key=form.cleaned_data["creation_key"],
            items=form.setup_items(),
            actor=None,
            timings={fee_type_pk("sign_fee"): ChargeTiming(When.INSTALLMENTS, count=2)},
            today=TODAY,
        )
    assert (raised.value.field, raised.value.message) == (
        "setup_sign_fee_first_due",
        installments.FIRST_REQUIRED,
    )
    assert not Lease.objects.exists()


PAST_9999 = [("9999-12-01", "monthly"), ("9999-12-28", "weekly"), ("9999-12-20", "biweekly")]


@pytest.mark.parametrize(("first", "cadence"), PAST_9999)
def test_the_service_refuses_a_plan_past_the_year_9999_on_the_first_date(
    today: date, first: str, cadence: str
) -> None:
    """Fable's finding on 015j: installments after the year 9999 are an Outcome on
    ``first_due``, not a server error; the New lease service names the row and saves nothing."""
    form = bound(**priced(**in_installments(FEE, "12", first, cadence)))
    assert form.is_valid(), form.errors
    with pytest.raises(one_off_charges.SetupRefused) as raised:
        charge_timing.create_lease(
            form.cleaned_data,
            rent=form.cleaned_data["rent"],
            creation_key=form.cleaned_data["creation_key"],
            items=form.setup_items(),
            actor=None,
            timings=form.timings(),
            today=TODAY,
        )
    assert (raised.value.field, raised.value.message) == (
        "setup_sign_fee_first_due",
        installments.PAST_CALENDAR,
    )
    assert not Lease.objects.exists() and not OneOffCharge.objects.exists()
    assert not InstallmentPlan.objects.exists() and not InvoiceDraft.objects.exists()


@pytest.mark.parametrize(("first", "cadence"), PAST_9999)
def test_the_page_after_a_plan_past_the_year_9999_shows_the_sentence_and_no_preview(
    admin: Client, today: date, first: str, cadence: str
) -> None:
    store = vacant_store()
    operator = new_lease.an_operator_with("Far Person", email="far@example.com")
    data = create_data(store, operator, **priced(**in_installments(FEE, "12", first, cadence)))
    response = post_create(admin, store, data)
    assert response.status_code == 302
    assert not Lease.objects.filter(store=store).exists()
    text = page(admin, create_path(store))
    expected = f"Sign fee: {installments.PAST_CALENDAR} The lease was not saved."
    assert html.escape(expected) in text
    assert 'hidden id="setup-sign_fee-preview"></p>' in text


# --- Errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", [DEP, FEE])
def test_a_when_without_a_price_is_ignored(admin: Client, today: date, prefix: str) -> None:
    """A row without a price ignores its When fields (even a count out of range) and saves
    nothing; the lease is saved."""
    fields = {**in_installments(prefix, "9999", "2026-11-01"), f"{prefix}_on": "not a date"}
    lease = save(admin, **fields)
    assert lease is not None
    assert not OneOffCharge.objects.filter(lease=lease).exists()
    assert not InstallmentPlan.objects.exists()


def test_a_row_ignores_the_fields_of_the_choices_it_did_not_make(today: date) -> None:
    """Without the script every field is posted; only the chosen When's fields count."""
    form = bound(
        **priced(
            **{f"{FEE}_when": "on_date", f"{FEE}_on": "2026-12-15", f"{FEE}_count": "99"},
            **{f"{DEP}_count": "1", f"{DEP}_first_due": "2020-01-01", f"{DEP}_on": "x"},
        )
    )
    assert form.is_valid(), form.errors
    assert form.timings()[fee_type_pk("sign_fee")] == ChargeTiming(
        When.ON_DATE, due_on=date(2026, 12, 15)
    )
    assert form.timings()[fee_type_pk("security_deposit")] == (charge_timing.WITH_INVOICE)


@pytest.mark.parametrize(
    ("fields", "errors"),
    [
        (on_date(FEE, ""), {f"{FEE}_on": [charge_timing.DUE_REQUIRED]}),
        (
            {f"{FEE}_when": "installments", f"{FEE}_first_due": "2026-12-01"},
            {f"{FEE}_count": [installments.COUNT_RANGE]},
        ),
        (
            {f"{FEE}_when": "installments", f"{FEE}_count": "3"},
            {f"{FEE}_first_due": [installments.FIRST_REQUIRED]},
        ),
        (
            {f"{DEP}_when": "installments"},
            {
                f"{DEP}_count": [installments.COUNT_RANGE],
                f"{DEP}_first_due": [installments.FIRST_REQUIRED],
            },
        ),
    ],
    ids=["date-missing", "count-missing", "first-missing", "both-missing"],
)
def test_an_incomplete_choice_is_refused_under_its_row(
    admin: Client, today: date, fields: dict[str, str], errors: dict[str, list[str]]
) -> None:
    form = bound(**priced(**fields))
    assert not form.is_valid()
    assert form.errors == errors
    assert save(admin, **priced(**fields)) is None
    assert not OneOffCharge.objects.exists()


def test_a_charge_with_the_first_invoice_needs_the_start_date(today: date) -> None:
    form = bound(**priced(started_on="", **on_date(FEE, "2026-12-15")))
    assert not form.is_valid()
    # The sign fee has its own date; only the deposit (with the first invoice) needs a start.
    assert form.errors[f"{DEP}_when"] == [SETUP_DUE_REQUIRED]
    assert f"{FEE}_when" not in form.errors


def test_a_cadence_left_empty_is_monthly(today: date) -> None:
    form = bound(**priced(**in_installments(FEE, "2", "2026-12-01", cadence="")))
    assert form.is_valid(), form.errors
    assert form.timings()[fee_type_pk("sign_fee")].cadence == "monthly"


@pytest.mark.parametrize(("cadence", "second"), [("weekly", 8), ("biweekly", 15)])
def test_the_cadence_is_kept_on_every_row(
    admin: Client, today: date, cadence: str, second: int
) -> None:
    lease = save(admin, **priced(**in_installments(FEE, "2", "2026-12-01", cadence=cadence)))
    assert lease is not None
    plan = plan_of(charge(lease, "sign_fee"))
    assert plan.cadence == cadence
    assert [row.due_date for row in installments.installments_of(plan)] == [
        date(2026, 12, 1),
        date(2026, 12, second),
    ]


# --- The lease page's words --------------------------------------------------------------


def test_the_lease_words_of_a_setup_charge(admin: Client, today: date) -> None:
    lease = save(admin, **priced(**on_date(FEE, "2026-12-15")))
    assert lease is not None
    fee = charge(lease, "sign_fee")
    deposit = charge(lease, "security_deposit")
    assert charge_timing.lease_words(fee) == "due Dec 15, 2026"
    assert charge_timing.lease_words(deposit) == ""  # with the first invoice
    # Re-pinned (Opus, PLAN-015j §13, the second MAJOR finding): an older "More → Due date"
    # kept a due date that is not the start; a line of the first invoice (here its draft)
    # keeps the row's usual words (it read "due Nov 20, 2026").
    OneOffCharge.objects.filter(pk=deposit.pk).update(due_date=date(2026, 11, 20))
    deposit.refresh_from_db()
    assert deposit.pk in draft_charge_ids(lease)
    assert charge_timing.lease_words(deposit) == ""
    # Paid: no words.
    Payment.objects.create(
        one_off_charge=fee,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ZELLE,
        amount=D("500.00"),
        status=PaymentStatus.SETTLED,
        initiated_on=TODAY,
        settled_on=TODAY,
        note="Paid by Zelle.",
    )
    one_off_charges.recompute(fee)
    fee.refresh_from_db()
    assert charge_timing.lease_words(fee) == ""


def test_the_lease_words_of_a_plan_count_the_installments_paid(admin: Client, today: date) -> None:
    lease = save(admin, **priced(**in_installments(FEE, "2", "2026-12-01")))
    assert lease is not None
    fee = charge(lease, "sign_fee")
    first = installments.installments_of(plan_of(fee))[0]
    Payment.objects.create(
        one_off_charge=first,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ZELLE,
        amount=first.price,
        status=PaymentStatus.SETTLED,
        initiated_on=TODAY,
        settled_on=TODAY,
        note="Paid by Zelle.",
    )
    one_off_charges.recompute(first)
    assert charge_timing.lease_words(fee) == "1 of 2 installments paid"
    assert charge_timing.lease_words(first) == ""  # an installment is not a setup charge


# --- Reports pinned ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "timing",
    [{}, on_date(FEE, "2026-12-15"), in_installments(FEE, "2", "2026-12-01")],
    ids=["with_invoice", "on_date", "installments"],
)
def test_the_pnl_fees_billed_over_the_lease_are_the_same_whichever_when(
    admin: Client, today: date, timing: dict[str, str]
) -> None:
    lease = save(admin, **{f"{FEE}_price": "500.00", **timing})
    assert lease is not None
    found = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 11, 1), date(2027, 1, 1))))
    assert found.fees_billed == D("500.00")
