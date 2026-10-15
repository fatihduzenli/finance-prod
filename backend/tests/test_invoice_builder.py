"""The invoice builder (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md D6, D7, D9;
§6 items of Batch B; the adversarial cases are the QA owner's file).

The service (:mod:`ledger.services.invoice_builder`), the worker's single create with
every line (:mod:`payments.one_off_invoices`, run through the queue), the allocation of
a payment on the invoice across its lines' charges (real webhook deliveries to a
:class:`~payments.fake.FakeProvider`), cancel, Charge now, the pages and their role gates,
the P&L and the open balances. Nothing reaches a network.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from accounts.roles import Role
from ledger.models import (
    NSF_FEE_FIRST,
    AuditLog,
    CollectionMethod,
    FeeType,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    InvoiceLineItem,
    InvoiceLineKind,
    InvoiceStatus,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentStatus,
    RentCharge,
    Store,
    StoreStatus,
)
from ledger.services import (
    invoice_builder,
    nsf,
    payments_ledger,
    pnl,
    receivables,
    rent_run,
    reports,
)
from ledger.services import one_off_charges as one_off_service
from ledger.services.invoice_builder import LineInput
from payments import offline_marking
from payments.errors import ProviderUnavailable
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind, JobStatus
from payments.provider import Amounts, InvoiceInput, PayWithStoredMethod
from payments.stax import validation
from tests.job_helpers import run_queued_jobs
from tests.test_charge_now import store_methods
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import CUSTOMER, an_operator, calls
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import ach_return, deliver, provider_txn, settle

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
CARD = "4111 1111 1111 1111"
MEMO = "Works of October at the store."


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


@pytest.fixture
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


# --- Builders ----------------------------------------------------------------------------


def user(role: Role) -> Any:
    return client_for(role)[1]


def line(
    item: str = "Sign repair",
    unit: str | None = "100.00",
    quantity: str | None = None,
    description: str = "",
    save_item: bool = False,
) -> LineInput:
    """A line: ``item`` is the name typed in the Item box (a catalogue item's name links
    the line to it, PLAN-012b D1); ``description`` the optional detail."""
    return LineInput(
        item=item,
        description=description,
        quantity=D(quantity) if quantity is not None else None,
        unit_amount=D(unit) if unit is not None else None,
        save_item=save_item,
    )


def a_world(fake: FakeProvider) -> tuple[Operator, Lease]:
    operator = an_operator(fake)
    return operator, make_lease(operator=operator)


def a_draft(
    operator: Operator,
    lease: Lease | None,
    lines: list[LineInput],
    *,
    due: date | None = None,
    memo: str = MEMO,
) -> InvoiceDraft:
    outcome = invoice_builder.create_draft(
        operator, lease, memo, due, lines, user(Role.BILLING), today=TODAY
    )
    assert outcome.ok and outcome.draft is not None, outcome.message
    return outcome.draft


def sent(draft: InvoiceDraft) -> invoice_builder.Outcome:
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert outcome.ok, outcome.message
    return outcome


def fresh(row: Any) -> Any:
    return type(row).objects.get(pk=row.pk)


def charges_of(draft: InvoiceDraft) -> list[OneOffCharge]:
    return invoice_builder.sent_charges(fresh(draft))


def created_invoices(fake: FakeProvider) -> list[InvoiceInput]:
    return [args[0] for args in calls(fake, "create_invoice")]  # type: ignore[misc]


def provider_ref(invoice: Invoice) -> str:
    return str(
        ExternalRef.objects.get(
            entity_type="invoice", entity_id=invoice.pk, external_kind="invoice"
        ).external_id
    )


def pay(fake: FakeProvider, invoice: Invoice, amount: str, txn: str | None = None) -> str:
    """A settled ACH payment of ``amount`` on the invoice, through the webhooks."""
    txn = txn or f"txn-{uuid.uuid4().hex[:8]}"
    provider_txn(
        fake,
        txn,
        total=D(amount),
        invoice_external_id=provider_ref(invoice),
        customer_external_id=CUSTOMER,
    )
    deliver("create_transaction", id=txn)
    settle(fake, txn)
    assert deliver("update_transaction_settled", id=txn) == "processed"
    return txn


def paid_states(draft: InvoiceDraft) -> list[tuple[Decimal, str]]:
    return [(charge.amount_paid, charge.status) for charge in charges_of(draft)]


def two_lines() -> list[LineInput]:
    return [line("Arrears September", "100.00"), line("Sign repair", "50.00")]


def sent_two(fake: FakeProvider) -> tuple[InvoiceDraft, Invoice]:
    """A two-line draft (100.00 then 50.00) sent, its invoice created by the worker."""
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    return draft, fresh(invoice)


def a_fee_type(code: str, name: str) -> FeeType:
    return FeeType.objects.create(code=code, name=name, sort_order=500)


# --- Drafts: lines, totals, due date (§6) -------------------------------------------------


def test_a_draft_is_local_only_until_it_is_sent(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    assert draft.status == InvoiceDraftStatus.DRAFT and draft.number.startswith("DRAFT-")
    assert [row.line_total for row in invoice_builder.lines_of(draft)] == [D("100.00"), D("50.00")]
    assert not fake.calls and not Job.objects.exists() and not OneOffCharge.objects.exists()
    assert AuditLog.objects.filter(action=invoice_builder.CREATED_ACTION).count() == 1


@pytest.mark.parametrize("count", [1, 20])
def test_one_line_and_twenty_lines_make_one_invoice_with_every_line(
    fake: FakeProvider, worker: None, count: int
) -> None:
    operator, lease = a_world(fake)
    lines = [line(f"Item {index}", f"{10 + index}.01") for index in range(1, count + 1)]
    draft = a_draft(operator, lease, lines)
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    [request] = created_invoices(fake)
    assert len(request.amounts.line_items) == count
    expected = sum((D(f"{10 + index}.01") for index in range(1, count + 1)), D("0"))
    assert request.amounts.total == expected == fresh(invoice).total
    validation.amounts(request.amounts)
    charges = charges_of(draft)
    assert len(charges) == count == InvoiceLineItem.objects.filter(invoice=invoice).count()
    assert [charge.description for charge in charges] == [f"Item {i}" for i in range(1, count + 1)]


def test_quantity_half_and_three_are_totalled_and_named_on_the_charge(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(
        operator,
        lease,
        [line("Cleaning hours", "0.25", quantity="0.5"), line("Poster", "33.33", quantity="3")],
    )
    half, three = invoice_builder.lines_of(draft)
    # 0.5 x 0.25 = 0.125, rounded half up to the cent; 3 x 33.33 = 99.99 exactly.
    assert (half.line_total, three.line_total) == (D("0.13"), D("99.99"))
    assert (half.quantity_text, three.quantity_text) == ("0.5", "3")
    sent(draft)
    first, second = charges_of(draft)
    assert (first.price, first.description) == (D("0.13"), "Cleaning hours (0.5 × 0.25)")
    assert (second.price, second.description) == (D("99.99"), "Poster (3 × 33.33)")
    run_queued_jobs()
    [request] = created_invoices(fake)
    lines = request.amounts.line_items
    # A quantity whose product is not the cent total goes as 1 at the total, named.
    assert (lines[0].description, lines[0].quantity, lines[0].unit_price) == (
        "Cleaning hours (0.5 × 0.25)",
        D(1),
        D("0.13"),
    )
    assert (lines[1].description, lines[1].quantity, lines[1].unit_price) == (
        "Poster",
        D(3),
        D("33.33"),
    )
    assert request.amounts.total == D("100.12")
    validation.amounts(request.amounts)


def test_line_totals_sum_to_the_cent(fake: FakeProvider, worker: None) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Ten cents", "0.10"), line("Twenty cents", "0.20")])
    invoice = sent(draft).invoice
    assert invoice is not None and invoice.total == D("0.30") and invoice.subtotal == D("0.30")
    run_queued_jobs()
    [request] = created_invoices(fake)
    assert (
        request.amounts.total
        == D("0.30")
        == sum((item.total for item in request.amounts.line_items), D("0"))
    )


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (line(unit="0.00"), invoice_builder.LINE_UNIT_AMOUNT),
        (line(unit="-5.00"), invoice_builder.LINE_UNIT_AMOUNT),
        (line(unit="1.005"), invoice_builder.LINE_UNIT_AMOUNT),
        (line(unit=None), invoice_builder.LINE_UNIT_AMOUNT),
        (line(quantity="0"), invoice_builder.LINE_QUANTITY),
        (line(quantity="0.00001"), invoice_builder.LINE_QUANTITY),
        (line(unit="0.01", quantity="0.0001"), invoice_builder.LINE_TOTAL_ZERO),
        (line(unit="999999.99", quantity="2"), invoice_builder.LINE_TOTAL_TOO_LARGE),
        (line(item=""), invoice_builder.LINE_ITEM_REQUIRED),
        (line(item=" \u00a0\u200b "), invoice_builder.LINE_ITEM_REQUIRED),
        (line(item="x" * 201), invoice_builder.LINE_ITEM_TOO_LONG),
        (line(item=f"Card {CARD}"), invoice_builder.LINE_ITEM_PAYMENT_DATA),
        (line(description="x" * 121), invoice_builder.LINE_DESCRIPTION_TOO_LONG),
        (line(description=f"Card {CARD}"), invoice_builder.LINE_PAYMENT_DATA),
    ],
)
def test_a_bad_line_is_refused_with_nothing_saved(
    fake: FakeProvider, bad: LineInput, message: str
) -> None:
    operator, lease = a_world(fake)
    outcome = invoice_builder.create_draft(
        operator, lease, MEMO, None, [line(), bad], user(Role.BILLING), today=TODAY
    )
    assert (outcome.ok, outcome.message) == (False, message.format(line=2))
    assert not InvoiceDraft.objects.exists()


def test_no_line_and_too_many_lines_are_refused(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    billing = user(Role.BILLING)
    assert invoice_builder.create_draft(operator, lease, "", None, [], billing).message == (
        invoice_builder.NO_LINES
    )
    many = [line() for _ in range(invoice_builder.MAX_LINES + 1)]
    assert invoice_builder.create_draft(operator, lease, "", None, many, billing).message == (
        invoice_builder.TOO_MANY_LINES
    )
    assert not InvoiceDraft.objects.exists()


def test_a_card_number_in_the_memo_is_refused(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    outcome = invoice_builder.create_draft(
        operator, lease, f"Pay with {CARD}", None, [line()], user(Role.BILLING)
    )
    assert (outcome.ok, outcome.field) == (False, "memo")
    assert not InvoiceDraft.objects.exists()


def test_a_due_date_in_the_past_is_refused_and_today_is_accepted(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    billing = user(Role.BILLING)
    past = invoice_builder.create_draft(
        operator, lease, "", TODAY - timedelta(days=1), [line()], billing, today=TODAY
    )
    assert (past.ok, past.message) == (False, invoice_builder.DUE_IN_PAST)
    draft = a_draft(operator, lease, [line()], due=TODAY)
    changed = invoice_builder.update_draft(
        draft.pk, lease, "", TODAY - timedelta(days=1), [line()], billing, today=TODAY
    )
    assert (changed.ok, changed.message) == (False, invoice_builder.DUE_IN_PAST)
    assert fresh(draft).due_date == TODAY


def test_without_a_due_date_the_invoice_is_due_fifteen_new_york_days_later(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 02:00 UTC on Oct 7 is still Oct 6 in New York.
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
    )
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line()], due=None)
    invoice = sent(draft).invoice
    assert invoice is not None
    assert (invoice.issue_date, invoice.due_date) == (TODAY, TODAY + timedelta(days=15))
    assert {charge.due_date for charge in charges_of(draft)} == {TODAY + timedelta(days=15)}


def test_a_draft_sent_after_its_due_date_passed_is_refused(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line()], due=TODAY)
    InvoiceDraft.objects.filter(pk=draft.pk).update(due_date=TODAY - timedelta(days=1))
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (outcome.ok, outcome.message) == (False, invoice_builder.DUE_IN_PAST)
    assert not Invoice.objects.exists() and not OneOffCharge.objects.exists()


def test_update_replaces_the_lines_as_a_set_while_a_draft(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    outcome = invoice_builder.update_draft(
        draft.pk, None, "New memo.", None, [line("Only line", "75.00")], user(Role.BILLING)
    )
    assert outcome.ok
    draft = fresh(draft)
    assert (draft.lease_id, draft.memo) == (None, "New memo.")
    assert [row.item for row in invoice_builder.lines_of(draft)] == ["Only line"]
    sent(draft)
    later = invoice_builder.update_draft(draft.pk, lease, "", None, [line()], user(Role.BILLING))
    assert (later.ok, later.message) == (False, invoice_builder.NOT_A_DRAFT)


# --- The fee-type category mapping (D6) ------------------------------------------------------


def test_the_fee_type_decides_the_category(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    damage = a_fee_type("damage", "Damage repair")
    sign_fee = FeeType.objects.get(code="sign_fee")
    draft = a_draft(
        operator,
        lease,
        [
            line("Damage repair", "300.00"),
            line("Sign fee", "150.00"),
            line("Typed line", "20.00"),
        ],
    )
    assert [(row.item, row.fee_type) for row in invoice_builder.lines_of(draft)] == [
        ("Damage repair", damage),
        ("Sign fee", sign_fee),
        ("Typed line", None),
    ]
    sent(draft)
    damage_charge, sign_charge, other_charge = charges_of(draft)
    assert (damage_charge.category, damage_charge.fee_type_id) == (OneOffCategory.DAMAGE, None)
    assert (sign_charge.category, sign_charge.fee_type_id) == (OneOffCategory.SETUP, sign_fee.pk)
    assert sign_charge.collection == CollectionMethod.EMAIL
    assert (other_charge.category, other_charge.fee_type_id) == (OneOffCategory.OTHER, None)
    # A billed setup charge is not edited like a checklist one: its invoice is out.
    edited = one_off_service.edit_charge(
        sign_charge.pk,
        cost=None,
        price=D("1.00"),
        vendor="",
        due_date=TODAY,
        actor=None,
    )
    assert (edited.ok, edited.message) == (False, one_off_service.EDIT_ONE_OFF)


def test_an_nsf_or_inactive_fee_types_name_is_a_typed_item(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    nsf_fee = FeeType.objects.get(code=NSF_FEE_FIRST)
    inactive = a_fee_type("old_fee", "Old fee")
    FeeType.objects.filter(pk=inactive.pk).update(active=False)
    for fee_type in (nsf_fee, inactive):
        draft = a_draft(operator, lease, [line(fee_type.name), line(f" {fee_type.name.upper()} ")])
        assert [row.fee_type_id for row in invoice_builder.lines_of(draft)] == [None, None]
    sent(draft)
    assert [charge.category for charge in charges_of(draft)] == [OneOffCategory.OTHER] * 2


def test_a_lease_takes_one_setup_charge_per_fee_type(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    sign_fee = FeeType.objects.get(code="sign_fee")
    twice = invoice_builder.create_draft(
        operator,
        lease,
        "",
        None,
        [line(sign_fee.name), line(" sign FEE ")],
        user(Role.BILLING),
    )
    assert twice.message == invoice_builder.LINE_SAME_FEE_TYPE.format(line=2, name="Sign fee")
    # Already charged on the lease by a first invoice: the second send writes nothing.
    sent(a_draft(operator, lease, [line(sign_fee.name)]))
    second = a_draft(operator, lease, [line("Poster"), line(sign_fee.name)])
    outcome = invoice_builder.send(second.pk, user(Role.BILLING))
    assert not outcome.ok and outcome.message.startswith("Line 2: ")
    assert OneOffCharge.objects.count() == 1 and Invoice.objects.count() == 1
    assert fresh(second).status == InvoiceDraftStatus.DRAFT


# --- The Item column (PLAN-012b D1 to D4) ----------------------------------------------------


def test_a_catalogue_name_typed_in_any_case_is_that_item_and_reaches_the_provider_as_text(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    sign_fee = FeeType.objects.get(code="sign_fee")
    draft = a_draft(operator, lease, [line("  sIGN FEE ", "150.00")])
    [row] = invoice_builder.lines_of(draft)
    assert (row.item, row.fee_type_id, row.description) == ("Sign fee", sign_fee.pk, "")
    sent(draft)
    [charge] = charges_of(draft)
    assert (charge.category, charge.fee_type_id) == (OneOffCategory.SETUP, sign_fee.pk)
    assert charge.description == "Sign fee"
    assert "Detail:" not in charge.note
    run_queued_jobs()
    [request] = created_invoices(fake)
    [item] = request.amounts.line_items
    assert (item.description, item.details, item.total) == ("Sign fee", "", D("150.00"))


def test_a_typed_item_with_a_detail_is_an_other_charge_with_the_detail_as_line_details(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    detail = "Replaced the left panel after the storm."
    draft = a_draft(
        operator,
        lease,
        [line("Storm repair", "80.00", description=detail), line("Poster", "0.25", "0.5")],
    )
    first, second = invoice_builder.lines_of(draft)
    assert (first.item, first.fee_type_id, first.description) == ("Storm repair", None, detail)
    sent(draft)
    repair, poster = charges_of(draft)
    assert (repair.category, repair.fee_type_id) == (OneOffCategory.OTHER, None)
    assert repair.description == "Storm repair"
    assert repair.note.endswith(f" Detail: {detail}")
    assert "Detail:" not in poster.note
    items = list(InvoiceLineItem.objects.order_by("position"))
    assert [(row.description, row.details) for row in items] == [
        ("Storm repair", detail),
        ("Poster", ""),
    ]
    run_queued_jobs()
    [request] = created_invoices(fake)
    assert [(item.description, item.details) for item in request.amounts.line_items] == [
        ("Storm repair", detail),
        # The fallback for a total that is not the product keeps its rule.
        ("Poster (0.5 × 0.25)", ""),
    ]
    validation.amounts(request.amounts)


def test_the_fallback_line_keeps_the_detail(fake: FakeProvider, worker: None) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Cleaning hours", "0.25", "0.5", description="Lobby")])
    sent(draft)
    run_queued_jobs()
    [request] = created_invoices(fake)
    [item] = request.amounts.line_items
    assert (item.description, item.quantity, item.unit_price, item.details) == (
        "Cleaning hours (0.5 × 0.25)",
        D(1),
        D("0.13"),
        "Lobby",
    )


def test_save_as_catalogue_item_creates_one_audited_offered_item_the_line_names(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    highest = max(row.sort_order for row in FeeType.objects.all())
    draft = a_draft(operator, lease, [line("Window film", "45.50", save_item=True)])
    saved = FeeType.objects.get(name="Window film")
    assert (saved.code, saved.default_price, saved.default_cost) == (
        "window-film",
        D("45.50"),
        None,
    )
    assert (saved.refundable, saved.active, saved.sort_order) == (False, True, highest + 1)
    [row] = invoice_builder.lines_of(draft)
    assert (row.item, row.fee_type_id) == ("Window film", saved.pk)
    [audit] = AuditLog.objects.filter(action=invoice_builder.FEE_TYPE_CREATED_ACTION)
    assert (audit.entity_type, audit.entity_id) == ("fee_type", str(saved.pk))
    assert audit.after is not None and audit.after["draft"] == draft.number
    assert audit.after["code"] == "window-film" and audit.after["default_price"] == "45.50"
    assert saved in invoice_builder.offered_fee_types()
    # The next draft's Item box offers it.
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/invoices/new/operator/{operator.pk}/").content.decode()
    assert '<option value="Window film"></option>' in page
    # A catalogue item bills as a setup charge.
    sent(draft)
    [charge] = charges_of(draft)
    assert (charge.category, charge.fee_type_id) == (OneOffCategory.SETUP, saved.pk)


def test_the_same_new_name_saved_on_two_lines_makes_one_catalogue_item(fake: FakeProvider) -> None:
    operator, _ = a_world(fake)
    draft = a_draft(
        operator,
        None,
        [
            line("Key copy", "5.00", save_item=True),
            line(" key COPY ", "5.00", "2", save_item=True),
            line("key copy", "5.00"),
        ],
    )
    [saved] = FeeType.objects.filter(code__startswith="key-copy")
    assert [(row.item, row.fee_type_id) for row in invoice_builder.lines_of(draft)] == [
        ("Key copy", saved.pk)
    ] * 3
    assert AuditLog.objects.filter(action=invoice_builder.FEE_TYPE_CREATED_ACTION).count() == 1


def test_a_new_name_saved_twice_on_a_lease_is_one_setup_charge_and_refused(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    outcome = invoice_builder.create_draft(
        operator,
        lease,
        "",
        None,
        [line("Key copy", save_item=True), line("key copy")],
        user(Role.BILLING),
    )
    assert (outcome.ok, outcome.field) == (False, "lines-1-item")
    assert outcome.message == invoice_builder.LINE_SAME_FEE_TYPE.format(line=2, name="key copy")
    assert not InvoiceDraft.objects.exists() and not FeeType.objects.filter(name="Key copy")


@pytest.mark.parametrize(
    ("name", "taken", "code"),
    [
        ("Window film", ["window-film"], "window-film-2"),
        ("Window film", ["window-film", "window-film-2"], "window-film-3"),
        # A category's code bills under the category: a saved item never takes it.
        ("Damage", [], "damage-2"),
        ("nsf_fee_later", [], "nsf_fee_later-2"),
        # No ASCII letter or digit: the fallback code.
        ("Ünïcödé ★", [], "unicode"),
        ("★ ☆", [], "item"),
        # At most 64 characters, the suffix included.
        ("a" * 70, ["a" * 64], "a" * 62 + "-2"),
    ],
)
def test_a_saved_items_code_is_its_slug_with_a_suffix_on_a_collision(
    fake: FakeProvider, name: str, taken: list[str], code: str
) -> None:
    operator, _ = a_world(fake)
    for index, existing in enumerate(taken):
        FeeType.objects.create(code=existing, name=f"Other {index}", active=True)
    a_draft(operator, None, [line(name, save_item=True)])
    assert FeeType.objects.get(name=name).code == code


def test_save_on_an_offered_name_is_ignored(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    before = FeeType.objects.count()
    sign_fee = FeeType.objects.get(code="sign_fee")
    draft = a_draft(operator, lease, [line("SIGN FEE", save_item=True)])
    [row] = invoice_builder.lines_of(draft)
    assert row.fee_type_id == sign_fee.pk and FeeType.objects.count() == before
    assert not AuditLog.objects.filter(action=invoice_builder.FEE_TYPE_CREATED_ACTION).exists()


@pytest.mark.parametrize("held", ["inactive", "nsf"])
def test_save_on_a_name_held_by_an_inactive_or_nsf_fee_type_is_refused(
    fake: FakeProvider, held: str
) -> None:
    operator, lease = a_world(fake)
    if held == "nsf":
        name = FeeType.objects.get(code=NSF_FEE_FIRST).name
    else:
        name = a_fee_type("old_fee", "Old fee").name
        FeeType.objects.filter(code="old_fee").update(active=False)
    before = FeeType.objects.count()
    billing = user(Role.BILLING)
    refused = invoice_builder.create_draft(
        operator, lease, "", None, [line(), line(f" {name.lower()}", save_item=True)], billing
    )
    assert (refused.ok, refused.field) == (False, "lines-1-save_item")
    assert refused.message == invoice_builder.LINE_SAVE_NOT_OFFERED.format(line=2)
    assert "A catalogue item with this name exists but is not offered" in refused.message
    assert not InvoiceDraft.objects.exists() and FeeType.objects.count() == before
    draft = a_draft(operator, lease, [line()])
    edited = invoice_builder.update_draft(
        draft.pk, lease, "", None, [line(name, save_item=True)], billing
    )
    assert edited.message == invoice_builder.LINE_SAVE_NOT_OFFERED.format(line=1)
    assert [row.item for row in invoice_builder.lines_of(fresh(draft))] == ["Sign repair"]
    assert FeeType.objects.count() == before


def test_save_on_edit_creates_the_item_under_the_draft(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line()])
    outcome = invoice_builder.update_draft(
        draft.pk,
        lease,
        "",
        None,
        [line("Gate remote", "30.00", save_item=True)],
        user(Role.BILLING),
    )
    assert outcome.ok, outcome.message
    saved = FeeType.objects.get(name="Gate remote")
    [row] = invoice_builder.lines_of(fresh(draft))
    assert row.fee_type_id == saved.pk
    [audit] = AuditLog.objects.filter(action=invoice_builder.FEE_TYPE_CREATED_ACTION)
    assert audit.after is not None and audit.after["draft_id"] == str(draft.pk)


def test_an_item_of_200_characters_and_a_detail_of_120_are_accepted(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    item, detail = "i" * 200, "d" * 120
    draft = a_draft(operator, lease, [line(item, "10.00", "2", description=detail)])
    [row] = invoice_builder.lines_of(draft)
    assert (row.item, row.description) == (item, detail)
    sent(draft)
    [charge] = charges_of(draft)
    # The charge's description is cut to fit, the quantity kept.
    assert charge.description == "i" * (120 - len(" (2 × 10.00)")) + " (2 × 10.00)"
    assert InvoiceLineItem.objects.get().description == item


def test_the_form_offers_the_catalogue_as_one_list_and_has_no_free_text(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    nsf_name = FeeType.objects.get(code=NSF_FEE_FIRST).name
    client, _ = client_for(Role.BILLING)
    for path in (
        f"/invoices/new/operator/{operator.pk}/",
        f"/invoices/drafts/{a_draft(operator, lease, [line()]).pk}/",
    ):
        page = client.get(path).content.decode()
        assert "Free text" not in page and "free text" not in page
        assert page.count('<datalist id="draft-items">') == 1
        assert '<option value="Sign fee"></option>' in page
        assert f'<option value="{nsf_name}">' not in page
        assert page.count('list="draft-items"') == page.count('class="draft-line"') > 0
        assert "Catalogue item or a new name" in page
        assert "<th>Item</th>" in page and "<th>Save as catalogue item</th>" in page
        assert 'name="lines-0-save_item"' in page and "lines-0-fee_type" not in page
    form_source = (Path(__file__).parents[1] / "ledger" / "forms.py").read_text(encoding="utf-8")
    assert "Free text" not in form_source


def test_the_page_saves_a_catalogue_item_and_refuses_an_empty_item_with_an_amount(
    fake: FakeProvider,
) -> None:
    operator, _ = a_world(fake)
    client, _ = client_for(Role.BILLING)
    path = f"/invoices/new/operator/{operator.pk}/"
    client.post(
        path, page_data([{"item": "", "description": "Detail only", "unit_amount": "5.00"}])
    )
    assert invoice_builder.LINE_ITEM_REQUIRED.format(line=1) in client.get(path).content.decode()
    assert not InvoiceDraft.objects.exists()
    client.post(
        path,
        page_data(
            [
                {"item": "Mailbox key", "unit_amount": "12.00", "save_item": "on"},
                {"item": "", "unit_amount": "", "save_item": "on"},  # blank: ignored
            ]
        ),
    )
    draft = InvoiceDraft.objects.get()
    saved = FeeType.objects.get(name="Mailbox key")
    [row] = invoice_builder.lines_of(draft)
    assert row.fee_type_id == saved.pk
    page = client.get(f"/invoices/drafts/{draft.pk}/").content.decode()
    assert 'value="Mailbox key"' in page and '<option value="Mailbox key"></option>' in page


def test_preview_and_print_show_the_item_and_the_detail_escaped(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(
        operator,
        lease,
        [line("<b>Sign</b> repair", "10.00", description="Left <i>panel</i>")],
    )
    viewer, _ = client_for(Role.VIEWER)
    for kind in ("preview", "print"):
        page = viewer.get(f"/invoices/drafts/{draft.pk}/{kind}/").content.decode()
        assert '<span class="line-item">&lt;b&gt;Sign&lt;/b&gt; repair</span>' in page
        assert '<span class="muted line-detail">Left &lt;i&gt;panel&lt;/i&gt;</span>' in page
        assert "<th>Item</th>" in page and "Free text" not in page


# --- Send (D6) ---------------------------------------------------------------------------


def test_the_two_line_draft_end_to_end_through_the_worker(fake: FakeProvider, worker: None) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    outcome = sent(draft)
    invoice = outcome.invoice
    assert invoice is not None and outcome.job is not None
    first, second = charges_of(draft)
    assert (first.lease_id, second.lease_id) == (lease.pk, lease.pk)
    assert (first.operator_id, second.operator_id) == (operator.pk, operator.pk)
    assert {first.collection, second.collection} == {CollectionMethod.EMAIL}
    assert Invoice.objects.get() == invoice and invoice.one_off_charge_id == first.pk
    assert (invoice.total, invoice.memo, invoice.due_date) == (
        D("150.00"),
        MEMO,
        TODAY + timedelta(days=15),
    )
    items = list(invoice.line_items.order_by("position"))
    assert [(item.position, item.one_off_charge_id) for item in items] == [
        (1, first.pk),
        (2, second.pk),
    ]
    [job] = Job.objects.all()
    assert (job.kind, job.entity_id) == (JobKind.INVOICE_CREATE, first.pk)
    run_queued_jobs()
    [request] = created_invoices(fake)
    assert [(item.description, item.total) for item in request.amounts.line_items] == [
        ("Arrears September", D("100.00")),
        ("Sign repair", D("50.00")),
    ]
    assert request.amounts.total == D("150.00") and request.reference == invoice.invoice_number
    assert request.memo == MEMO
    validation.amounts(request.amounts)
    assert len(calls(fake, "send_invoice_email")) == 1
    assert fresh(invoice).internal_status == InvoiceStatus.SENT
    draft = fresh(draft)
    assert (draft.status, draft.sent_invoice_id) == (InvoiceDraftStatus.SENT, invoice.pk)


def test_send_twice_makes_one_invoice(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    sent(draft)
    number = Invoice.objects.get().invoice_number
    again = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (again.ok, again.message) == (False, invoice_builder.ALREADY_SENT.format(number=number))
    client, _ = client_for(Role.BILLING)
    assert client.post(f"/invoices/drafts/{draft.pk}/send/").status_code == 302
    assert Invoice.objects.count() == 1 and OneOffCharge.objects.count() == 2
    assert Job.objects.count() == 1


def test_an_operator_without_a_provider_customer_cannot_send(fake: FakeProvider) -> None:
    operator = an_operator(fake, customer=None)
    draft = a_draft(operator, make_lease(operator=operator), [line()])
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (outcome.ok, outcome.message) == (False, invoice_builder.NO_PROVIDER_CUSTOMER)
    assert not OneOffCharge.objects.exists()


def test_a_draft_of_operator_a_cannot_go_on_operator_b_lease(fake: FakeProvider) -> None:
    operator_a, _ = a_world(fake)
    operator_b = an_operator(fake, customer="cus-b", bank="pm-b")
    lease_b = make_lease(operator=operator_b)
    refused = invoice_builder.create_draft(
        operator_a, lease_b, "", None, [line()], user(Role.BILLING)
    )
    assert (refused.ok, refused.message) == (False, invoice_builder.LEASE_OTHER_OPERATOR)
    draft = a_draft(operator_a, None, [line()])
    InvoiceDraft.objects.filter(pk=draft.pk).update(lease=lease_b)
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (outcome.ok, outcome.message) == (False, invoice_builder.LEASE_OTHER_OPERATOR)
    assert not OneOffCharge.objects.exists() and not Invoice.objects.exists()


def test_an_operator_level_draft_makes_operator_level_charges(fake: FakeProvider) -> None:
    operator, _ = a_world(fake)
    draft = a_draft(operator, None, two_lines())
    sent(draft)
    assert {(charge.lease_id, charge.operator_id) for charge in charges_of(draft)} == {
        (None, operator.pk)
    }


# --- Allocation across the lines (D6) -----------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("150.00", [(D("100.00"), "paid"), (D("50.00"), "paid")]),
        ("120.00", [(D("100.00"), "paid"), (D("20.00"), "partial")]),
        ("100.00", [(D("100.00"), "paid"), (D("0.00"), "owed")]),
        ("60.00", [(D("60.00"), "partial"), (D("0.00"), "owed")]),
    ],
)
def test_a_payment_pays_the_lines_in_order(
    fake: FakeProvider, worker: None, amount: str, expected: list[tuple[Decimal, str]]
) -> None:
    draft, invoice = sent_two(fake)
    pay(fake, invoice, amount)
    [payment] = Payment.objects.filter(status=PaymentStatus.SETTLED)
    first, second = charges_of(draft)
    assert payment.one_off_charge_id == first.pk and payment.amount == D(amount)
    assert paid_states(draft) == expected
    allocated = AuditLog.objects.filter(
        action=one_off_service.ALLOCATED_ACTION, entity_id=str(second.pk)
    )
    assert allocated.exists() == (expected[1][0] > 0)


def test_a_return_unwinds_from_the_last_line_first(fake: FakeProvider, worker: None) -> None:
    draft, invoice = sent_two(fake)
    pay(fake, invoice, "120.00")
    later = pay(fake, invoice, "30.00")
    assert paid_states(draft) == [(D("100.00"), "paid"), (D("50.00"), "paid")]
    ach_return(fake, later)
    deliver("update_transaction_settled", id=later)
    assert paid_states(draft) == [(D("100.00"), "paid"), (D("20.00"), "partial")]


def test_a_manual_payment_on_the_first_line_pays_the_invoice_in_order(
    fake: FakeProvider, worker: None
) -> None:
    draft, _ = sent_two(fake)
    first, _second = charges_of(draft)
    outcome = one_off_service.record_payment(
        first.pk, method="wire", amount=D("150.00"), paid_on=TODAY, note="Wire.", actor=None
    )
    assert outcome.ok and outcome.mark_due
    assert paid_states(draft) == [(D("100.00"), "paid"), (D("50.00"), "paid")]


# --- Cancel (D6) -------------------------------------------------------------------------


def test_cancel_before_sending_closes_the_draft(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    outcome = invoice_builder.cancel(draft.pk, user(Role.BILLING))
    assert (outcome.ok, fresh(draft).status) == (True, InvoiceDraftStatus.CANCELLED)
    again = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (again.ok, again.message) == (False, invoice_builder.ALREADY_CANCELLED)
    assert not fake.calls and not OneOffCharge.objects.exists()


def test_cancel_after_sending_voids_every_line(fake: FakeProvider, worker: None) -> None:
    draft, invoice = sent_two(fake)
    first, second = charges_of(draft)
    assert one_off_service.cancel_refusal(second) == one_off_service.CANCEL_LINE
    assert invoice_builder.cancel(draft.pk, user(Role.BILLING)).message == (
        one_off_service.VOID_NOTE_REQUIRED
    )
    outcome = invoice_builder.cancel(draft.pk, user(Role.BILLING), note="Wrong store.")
    assert outcome.ok and outcome.job is not None and outcome.job.kind == JobKind.INVOICE_DELETE
    run_queued_jobs()
    assert calls(fake, "delete_invoice")
    assert [charge.status for charge in charges_of(draft)] == [
        OneOffChargeStatus.VOIDED,
        OneOffChargeStatus.VOIDED,
    ]
    assert fresh(invoice).internal_status == InvoiceStatus.VOID
    assert receivables.open_balances(TODAY, operator=fresh(draft).operator_id) == []


def test_cancel_after_a_payment_is_refused(fake: FakeProvider, worker: None) -> None:
    draft, invoice = sent_two(fake)
    pay(fake, invoice, "30.00")
    outcome = invoice_builder.cancel(draft.pk, user(Role.BILLING), note="Wrong store.")
    assert (outcome.ok, outcome.message) == (False, one_off_service.CANCEL_PAID)
    assert not Job.objects.filter(kind=JobKind.INVOICE_DELETE).exists()


# --- Charge now (D6, 10d D4) -------------------------------------------------------------


def test_charge_now_on_a_multi_line_invoice_is_one_attempt_with_the_stored_key(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    draft = a_draft(operator, lease, two_lines())
    details = invoice_builder.charge_now_details(draft, user(Role.ADMIN))
    assert (details.refusal, details.total, details.last_four) == ("", D("150.00"), "6789")
    outcome = invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=False)
    assert outcome.ok, outcome.message
    first, second = charges_of(draft)
    assert {first.collection, second.collection} == {CollectionMethod.CHARGE_NOW}
    assert first.charge_now_key is not None and second.charge_now_key is None
    run_queued_jobs()
    assert len(created_invoices(fake)) == 1 and not calls(fake, "send_invoice_email")
    [(ref, request)] = calls(fake, "pay_invoice")
    assert isinstance(request, PayWithStoredMethod)
    assert request.idempotency_id == str(fresh(first).charge_now_key)
    assert ref == provider_ref(Invoice.objects.get())
    [payment] = Payment.objects.all()
    assert (payment.one_off_charge_id, payment.amount, payment.status) == (
        first.pk,
        D("150.00"),
        PaymentStatus.PENDING,
    )
    # The pay attempt is queued by the create, once the invoice is at the provider.
    assert sorted(Job.objects.values_list("kind", "status")) == [
        (JobKind.INVOICE_CREATE, JobStatus.DONE),
        (JobKind.INVOICE_PAY, JobStatus.DONE),
    ]


def test_charge_now_above_the_threshold_needs_the_confirmation(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    big = str(D(settings.LARGE_DEBIT_WARNING_AMOUNT))
    draft = a_draft(operator, lease, [line("Arrears", big), line("Fee", "1.00")])
    refused = invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=False)
    assert (refused.ok, refused.field) == (False, "confirm")
    assert not OneOffCharge.objects.exists()
    assert invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=True).ok


def test_charge_now_is_refused_when_the_flag_is_off(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    draft = a_draft(operator, lease, [line()])
    outcome = invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=True)
    assert (outcome.ok, outcome.message) == (False, one_off_service.CHARGE_NOW_DISABLED)


# --- Roles (D9) --------------------------------------------------------------------------


def test_a_viewer_cannot_send_and_billing_cannot_charge_now(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    draft = a_draft(operator, lease, two_lines())
    viewer, viewer_user = client_for(Role.VIEWER)
    billing, billing_user = client_for(Role.BILLING)
    assert invoice_builder.send(draft.pk, viewer_user).message == invoice_builder.BILLING_ONLY
    assert invoice_builder.charge_now(draft.pk, billing_user, confirmed=True).message == (
        invoice_builder.CHARGE_NOW_ADMIN
    )
    base = f"/invoices/drafts/{draft.pk}"
    for path in (f"{base}/send/", f"{base}/edit/", f"{base}/cancel/", f"{base}/charge-now/"):
        assert viewer.post(path).status_code == 403, path
    assert viewer.get(f"{base}/").status_code == 403
    assert billing.get(f"{base}/charge-now/").status_code == 403
    assert billing.post(f"{base}/charge-now/", {"confirm": "true"}).status_code == 403
    page = billing.get(f"{base}/").content.decode()
    assert 'id="send-draft"' in page and 'id="draft-charge-now"' not in page
    admin, _ = client_for(Role.ADMIN)
    assert 'id="draft-charge-now"' in admin.get(f"{base}/").content.decode()
    assert not OneOffCharge.objects.exists() and fresh(draft).status == InvoiceDraftStatus.DRAFT


def test_the_preview_and_the_print_view_render_for_a_viewer(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Poster", "33.33", quantity="3")])
    viewer, _ = client_for(Role.VIEWER)
    preview = viewer.get(f"/invoices/drafts/{draft.pk}/preview/")
    printed = viewer.get(f"/invoices/drafts/{draft.pk}/print/")
    assert (preview.status_code, printed.status_code) == (200, 200)
    for text in (preview.content.decode(), printed.content.decode()):
        assert draft.number in text and settings.COMPANY_NAME in text
        assert operator.display_name in text and lease.store.external_label in text
        assert "Poster" in text and "$99.99" in text and MEMO in text
        assert (TODAY + timedelta(days=15)).isoformat() in text
    page = printed.content.decode()
    assert "<nav" not in page and 'id="save-as-pdf"' in page and "@media print" in page
    sent(draft)
    number = Invoice.objects.get().invoice_number
    assert number in viewer.get(f"/invoices/drafts/{draft.pk}/print/").content.decode()


# --- The pages (D6) ----------------------------------------------------------------------


def page_data(lines: list[dict[str, str]], **header: str) -> dict[str, str]:
    data = {
        "creation_key": str(uuid.uuid4()),
        "lease": "",
        "memo": "",
        "due_date": "",
        "lines-TOTAL_FORMS": str(len(lines)),
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": str(invoice_builder.MAX_LINES),
        **header,
    }
    for index, row in enumerate(lines):
        data.update({f"lines-{index}-{name}": value for name, value in row.items()})
    return data


def test_new_invoice_on_the_store_page_preselects_the_lease_and_saves_a_draft(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    client, _ = client_for(Role.BILLING)
    store_page = client.get(f"/stores/{lease.store_id}/").content.decode()
    assert 'id="new-invoice"' in store_page
    path = f"/invoices/new/store/{lease.store_id}/"
    blank = client.get(path).content.decode()
    assert f'<option value="{lease.pk}" selected>' in blank
    assert blank.count('class="draft-line"') == 5  # blank rows without script
    response = client.post(
        path,
        page_data(
            [
                {"item": "Sign repair", "unit_amount": "250.00", "quantity": ""},
                {"item": "", "unit_amount": ""},
                {"item": "Removed", "unit_amount": "1.00", "DELETE": "on"},
            ],
            lease=str(lease.pk),
            memo=MEMO,
        ),
    )
    draft = InvoiceDraft.objects.get()
    assert response["Location"] == f"/invoices/drafts/{draft.pk}/"
    assert (draft.operator_id, draft.lease_id, draft.memo) == (operator.pk, lease.pk, MEMO)
    assert [(row.item, row.line_total) for row in invoice_builder.lines_of(draft)] == [
        ("Sign repair", D("250.00"))
    ]
    page = client.get(response["Location"]).content.decode()
    assert draft.number in page and 'value="Sign repair"' in page


def test_a_store_without_a_lease_today_offers_no_new_invoice(fake: FakeProvider) -> None:
    store = Store.objects.create(external_label="Store 0999", status=StoreStatus.ACTIVE)
    client, _ = client_for(Role.BILLING)
    assert 'id="new-invoice"' not in client.get(f"/stores/{store.pk}/").content.decode()
    response = client.get(f"/invoices/new/store/{store.pk}/")
    assert response.status_code == 302 and response["Location"] == f"/stores/{store.pk}/"


def test_a_hand_over_day_offers_both_leases(fake: FakeProvider) -> None:
    outgoing, lease = a_world(fake)
    incoming = an_operator(fake, customer="cus-in", bank="pm-in")
    second = make_lease(operator=incoming, store=lease.store, started_on=TODAY)
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/invoices/new/store/{lease.store_id}/").content.decode()
    assert f'<option value="{second.pk}" selected>' in page
    assert f'<option value="{lease.pk}">' in page
    assert outgoing.display_name in page


def test_new_invoice_on_the_operator_page_may_be_operator_level(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    client, _ = client_for(Role.BILLING)
    assert 'id="new-invoice"' in client.get(f"/operators/{operator.pk}/").content.decode()
    path = f"/invoices/new/operator/{operator.pk}/"
    assert "No lease (the operator)" in client.get(path).content.decode()
    client.post(path, page_data([{"item": "Late fee", "unit_amount": "25.00"}]))
    draft = InvoiceDraft.objects.get()
    assert (draft.operator_id, draft.lease_id) == (operator.pk, None)
    listing = client.get(f"/operators/{operator.pk}/").content.decode()
    assert f"/invoices/drafts/{draft.pk}/" in listing


def test_a_refused_page_shows_the_sentence_and_keeps_no_card_number(fake: FakeProvider) -> None:
    operator, _ = a_world(fake)
    client, _ = client_for(Role.BILLING)
    path = f"/invoices/new/operator/{operator.pk}/"
    client.post(
        path, page_data([{"item": "Repair", "description": f"Card {CARD}", "unit_amount": "5.00"}])
    )
    page = client.get(path).content.decode()
    assert "card or account numbers" in page and CARD not in page
    client.post(
        path, page_data([{"item": "Repair", "description": "x" * 121, "unit_amount": "5.00"}])
    )
    assert (
        invoice_builder.LINE_DESCRIPTION_TOO_LONG.format(line=1)
        in client.get(path).content.decode()
    )
    assert not InvoiceDraft.objects.exists()


def test_the_draft_page_saves_and_sends_then_shows_the_invoice(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Old line", "10.00")])
    client, _ = client_for(Role.BILLING)
    base = f"/invoices/drafts/{draft.pk}"
    client.post(
        f"{base}/edit/",
        page_data(
            [
                {"item": "Old line", "unit_amount": "10.00", "DELETE": "on"},
                {"item": "Arrears September", "unit_amount": "100.00"},
                {"item": "Sign repair", "unit_amount": "50.00", "quantity": "1"},
            ],
            lease=str(lease.pk),
        ),
    )
    assert [row.item for row in invoice_builder.lines_of(fresh(draft))] == [
        "Arrears September",
        "Sign repair",
    ]
    client.post(f"{base}/send/")
    run_queued_jobs()
    invoice = Invoice.objects.get()
    page = client.get(f"{base}/").content.decode()
    assert invoice.invoice_number in page and 'id="send-draft"' not in page
    for charge in charges_of(draft):
        assert f"/one-off-charges/{charge.pk}/" in page
    shown = invoice.provider_status
    assert shown and f'<dd id="sent-provider-status">{shown}</dd>' in page


def test_the_draft_page_cancels_a_draft(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line()])
    client, _ = client_for(Role.BILLING)
    assert client.get(f"/invoices/drafts/{draft.pk}/cancel/").status_code == 200
    client.post(f"/invoices/drafts/{draft.pk}/cancel/")
    assert fresh(draft).status == InvoiceDraftStatus.CANCELLED


# --- The P&L and the open balances (D6) --------------------------------------------------


def test_the_pnl_counts_each_line_by_its_category(fake: FakeProvider, worker: None) -> None:
    operator, lease = a_world(fake)
    arrears = a_fee_type("arrears", "Arrears")
    damage = a_fee_type("damage", "Damage repair")
    draft = a_draft(
        operator,
        lease,
        [line(arrears.name, "1000.00"), line(damage.name, "200.00")],
    )
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    pay(fake, fresh(invoice), "1100.00")
    selected = pnl.parse_query({"from": "2026-10", "to": "2026-10", "group": "store"})
    rows = [row for row in pnl.rows(selected) if row.store_id == lease.store_id]

    def total(name: str) -> Decimal:
        return sum((getattr(row, name) for row in rows), D("0"))

    assert (total("rent_billed"), total("fees_billed")) == (D("1000.00"), D("200.00"))
    assert (total("rent_collected"), total("fees_collected")) == (D("1000.00"), D("100.00"))


def test_the_open_balances_owe_each_line_once(fake: FakeProvider, worker: None) -> None:
    draft, invoice = sent_two(fake)
    operator_id = fresh(draft).operator_id
    rows = receivables.open_balances(TODAY, operator=operator_id)
    assert sorted(row.balance for row in rows) == [D("50.00"), D("100.00")]
    pay(fake, invoice, "120.00")
    [row] = receivables.open_balances(TODAY, operator=operator_id)
    assert (row.charge_id, row.balance) == (charges_of(draft)[1].pk, D("30.00"))


def test_a_later_line_is_linked_and_charged_only_through_the_first_but_takes_a_payment(
    fake: FakeProvider, worker: None
) -> None:
    draft, invoice = sent_two(fake)
    first, second = charges_of(draft)
    linked = one_off_service.link_invoice(
        second.pk, invoice.pk, confirm=True, note="Same invoice.", actor=None
    )
    assert (linked.ok, linked.message) == (False, one_off_service.LINK_LINE)
    assert one_off_service.charge_now_refusal(second) in (
        one_off_service.CHARGE_NOW_DISABLED,
        one_off_service.CHARGE_NOW_NOT_ELIGIBLE,
    )
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/one-off-charges/{second.pk}/").content.decode()
    for hidden in ('id="link-invoice"', 'id="cancel-charge"'):
        assert hidden not in page, hidden
    # PLAN-012c-a D1, D2: the later line shows its invoice and takes a payment.
    assert 'id="record-payment"' in page and invoice.invoice_number in page
    assert "No invoice linked" not in page
    first_page = client.get(f"/one-off-charges/{first.pk}/").content.decode()
    assert 'id="record-payment"' in first_page and 'id="edit-charge"' not in first_page
    assert 'id="link-invoice"' not in first_page


# --- Review findings of Batch B ------------------------------------------------------------


def _with_deposit_second(fake: FakeProvider) -> tuple[Operator, InvoiceDraft, Invoice]:
    """A two-line draft: a 100.00 repair, then the 50.00 security deposit, sent."""
    operator, lease = a_world(fake)
    deposit = FeeType.objects.get(code="security_deposit")
    lines = [line("Sign repair", "100.00"), line(deposit.name, "50.00")]
    draft = a_draft(operator, lease, lines)
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    return operator, draft, fresh(invoice)


@pytest.mark.parametrize(
    ("amount", "held"),
    [("150.00", "50.00"), ("120.00", "20.00"), ("90.00", "0.00")],
    ids=["in-full", "in-part", "first-line-only"],
)
def test_a_deposit_on_the_second_line_is_held_by_what_it_was_allocated(
    fake: FakeProvider, worker: None, amount: str, held: str
) -> None:
    operator, draft, invoice = _with_deposit_second(fake)
    pay(fake, invoice, amount)
    _, second = charges_of(draft)
    assert second.amount_paid == D(held)
    collected = D(amount) - D(held)
    october = {"from": "2026-10", "to": "2026-10"}
    assert pnl.deposits_held(pnl.parse_query(october)) == D(held)
    ledger = payments_ledger.totals(
        payments_ledger.ledger_payments(payments_ledger.parse_filter({}))
    )
    assert (ledger.deposits_held, ledger.settled) == (D(held), collected)
    report = reports.statement(operator, reports.parse_range(october, TODAY), TODAY)
    assert (report.deposits_held, report.paid) == (D(held), collected)
    rows = pnl.rows(pnl.parse_query({**october, "group": "store"}))
    assert sum((row.fees_collected for row in rows), D("0")) == collected


def test_a_refund_on_a_deposit_line_takes_back_from_the_deposit_first(
    fake: FakeProvider, worker: None
) -> None:
    """A refund is taken back from the last line first: here the deposit."""
    operator, draft, invoice = _with_deposit_second(fake)
    txn = pay(fake, invoice, "150.00")
    paid = Payment.objects.get(status=PaymentStatus.SETTLED, is_refund=False)
    refund = Payment.objects.create(
        kind=paid.kind,
        one_off_charge=paid.one_off_charge,
        amount=D("30.00"),
        method=paid.method,
        status=PaymentStatus.SETTLED,
        is_refund=True,
        parent_payment=paid,
        invoice=paid.invoice,
        initiated_on=paid.settled_on,
        settled_on=paid.settled_on,
    )
    # Recorded after the payment (the clock is pinned in this module).
    Payment.objects.filter(pk=refund.pk).update(created_at=NOW + timedelta(minutes=1))
    assert txn
    october = {"from": "2026-10", "to": "2026-10"}
    assert pnl.deposits_held(pnl.parse_query(october)) == D("20.00")
    ledger = payments_ledger.totals(
        payments_ledger.ledger_payments(payments_ledger.parse_filter({}))
    )
    assert (ledger.deposits_held, ledger.settled, ledger.refunded) == (
        D("20.00"),
        D("100.00"),
        D("0.00"),
    )
    report = reports.statement(operator, reports.parse_range(october, TODAY), TODAY)
    assert (report.deposits_held, report.paid, report.refunded) == (
        D("20.00"),
        D("100.00"),
        D("0.00"),
    )


def test_a_rescheduled_return_on_a_two_line_invoice_is_owed_once(
    fake: FakeProvider, worker: None
) -> None:
    """Paid, returned, rescheduled: the re-debit carries the whole payment, so the first
    line's balance and then the later line's are carried, in line order."""
    draft, invoice = sent_two(fake)
    operator_id = fresh(draft).operator_id
    txn = pay(fake, invoice, "150.00")
    assert receivables.open_balances(TODAY, operator=operator_id) == []
    ach_return(fake, txn)
    deliver("update_transaction_settled", id=txn)
    before = receivables.open_balances(TODAY, operator=operator_id)
    assert sorted(row.balance for row in before) == [D("50.00"), D("100.00")]
    returned = Payment.objects.get(status=PaymentStatus.RETURNED)
    outcome = nsf.reschedule(
        returned.pk,
        redebit_date=TODAY + timedelta(days=3),
        waive_fee=False,
        note="Owner agreed by phone.",
        actor=user(Role.BILLING),
        today=TODAY,
    )
    assert outcome.ok, outcome.message
    after = receivables.open_balances(TODAY, operator=operator_id)
    redebit = OneOffCharge.objects.get(category=OneOffCategory.REDEBIT)
    fee = OneOffCharge.objects.get(companion_of=redebit)
    assert {row.charge_id: row.balance for row in after} == {
        redebit.pk: D("150.00"),
        fee.pk: Decimal(fee.price),
    }
    assert receivables.total(after) - Decimal(fee.price) == receivables.total(before)


# --- PLAN-012a D1, D2: line kind, fees and surcharge -------------------------------------


def test_a_builder_invoice_has_charge_lines_and_no_fees_or_surcharge(
    fake: FakeProvider, worker: None
) -> None:
    draft, invoice = sent_two(fake)
    assert [row.kind for row in invoice.line_items.order_by("position")] == [
        InvoiceLineKind.CHARGE,
        InvoiceLineKind.CHARGE,
    ]
    assert (invoice.fees, invoice.surcharge) == (D("0.00"), D("0.00"))
    assert invoice.total == invoice.subtotal + invoice.tax == D("150.00")
    [request] = created_invoices(fake)
    # The provider contract is unchanged: no fees and no surcharge reach it.
    assert request.amounts.total == invoice.total
    assert not {"fees", "surcharge"} & {field.name for field in dataclasses.fields(Amounts)}


def _keywords_of_calls(repo_root: Path, callee: str) -> list[tuple[str, set[str]]]:
    """Every call of ``callee`` in application code: its place and its keyword names."""
    found = []
    for package in ("ledger", "payments"):
        for path in (repo_root / "backend" / package).rglob("*.py"):
            if "migrations" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and ast.unparse(node.func) == callee:
                    place = f"{path.relative_to(repo_root)}:{node.lineno}"
                    found.append((place, {kw.arg for kw in node.keywords if kw.arg}))
    return found


def test_every_invoice_writer_sets_fees_and_surcharge_explicitly(repo_root: Path) -> None:
    writers = _keywords_of_calls(repo_root, "Invoice.objects.create")
    assert len(writers) >= 6
    assert [place for place, names in writers if not {"fees", "surcharge"} <= names] == []


def test_every_line_item_writer_sets_the_kind_explicitly(repo_root: Path) -> None:
    writers = _keywords_of_calls(repo_root, "InvoiceLineItem")
    assert writers
    assert [place for place, names in writers if "kind" not in names] == []


# --- PLAN-012c-a: every line linked to its invoice, paid line by line ----------------------


def sent_lines(fake: FakeProvider, *lines: tuple[str, str]) -> tuple[InvoiceDraft, Invoice]:
    """A draft of ``lines`` (item, unit amount) sent, its invoice created by the worker."""
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line(item, unit) for item, unit in lines])
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    return draft, fresh(invoice)


def owners_invoice(fake: FakeProvider) -> tuple[InvoiceDraft, Invoice]:
    """INV-2026-0006's shape: Damage Repair 300.00 and a late fee 50.00."""
    return sent_lines(fake, ("Damage Repair", "300.00"), ("Late fee", "50.00"))


def wire(amount: str, reference: str) -> dict[str, str]:
    return {
        "creation_key": str(uuid.uuid4()),
        "method": "wire",
        "amount": amount,
        "paid_on": TODAY.isoformat(),
        "reference": reference,
        "note": f"Wire {reference} received.",
    }


def post_wire(charge: OneOffCharge, amount: str, reference: str) -> None:
    client, _ = client_for(Role.BILLING)
    response = client.post(f"/one-off-charges/{charge.pk}/payments/new/", wire(amount, reference))
    assert response.status_code == 302


def manual(charge: OneOffCharge, amount: str) -> Any:
    return one_off_service.record_payment(
        charge.pk, method="wire", amount=D(amount), paid_on=TODAY, note="Wire.", actor=None
    )


def marks(fake: FakeProvider) -> list[tuple[object, ...]]:
    return calls(fake, "mark_invoice_paid_cash_or_check")


def flag_of(charge: OneOffCharge) -> str:
    return str(fresh(charge).review_message)


def test_the_owners_invoice_paid_line_by_line_marks_it_once_and_clears_both_flags(
    fake: FakeProvider, worker: None
) -> None:
    draft, invoice = owners_invoice(fake)
    repair, late_fee = charges_of(draft)
    post_wire(repair, "300.00", "WIRE-300")
    run_queued_jobs()
    assert marks(fake) == []
    assert flag_of(repair) == offline_marking.PARTIAL_AMOUNT and fresh(repair).needs_review
    assert flag_of(late_fee) == "" and not fresh(late_fee).needs_review
    post_wire(late_fee, "50.00", "WIRE-50")
    assert Job.objects.filter(kind=JobKind.ONE_OFF_PAYMENT_MARK).count() == 1
    [job] = run_queued_jobs()
    assert (job.kind, job.entity_id, job.status) == (
        JobKind.ONE_OFF_PAYMENT_MARK,
        late_fee.pk,
        JobStatus.DONE,
    )
    [(ref, offline)] = marks(fake)
    assert ref == provider_ref(invoice)
    assert getattr(offline, "check_number", "") == "WIRE-50"
    assert getattr(offline, "note", "").startswith("Paid by wire on 10/06/2026. Wire WIRE-50")
    assert getattr(offline, "amount", None) == D("350.00")
    for charge in (repair, late_fee):
        row = fresh(charge)
        assert (row.status, row.balance, row.needs_review, row.review_message) == (
            OneOffChargeStatus.PAID,
            D("0.00"),
            False,
            "",
        )
        assert not one_off_service.review_message(row)
    assert fresh(invoice).provider_status == "PAID"
    cleared = AuditLog.objects.get(
        action="one_off_charge.provider_marked", entity_id=str(repair.pk)
    )
    assert cleared.after is not None and cleared.after["with"] == str(late_fee.pk)
    assert receivables.open_balances(TODAY, operator=fresh(draft).operator_id) == []
    # The late fee's page shows the invoice and its provider status.
    page = client_for(Role.BILLING)[0].get(f"/one-off-charges/{late_fee.pk}/").content.decode()
    assert invoice.invoice_number in page and "PAID" in page


def test_the_late_fee_first_then_the_repair_marks_once(fake: FakeProvider, worker: None) -> None:
    draft, invoice = owners_invoice(fake)
    repair, late_fee = charges_of(draft)
    first_pay = manual(late_fee, "50.00")
    assert first_pay.ok and not first_pay.mark_due
    assert flag_of(late_fee) == offline_marking.PARTIAL_AMOUNT and flag_of(repair) == ""
    post_wire(repair, "300.00", "WIRE-300")
    run_queued_jobs()
    [(ref, _)] = marks(fake)
    assert ref == provider_ref(invoice)
    assert (flag_of(repair), flag_of(late_fee)) == ("", "")
    assert [row.status for row in charges_of(draft)] == [OneOffChargeStatus.PAID] * 2


def test_a_three_line_invoice_paid_in_three_payments_marks_on_the_last(
    fake: FakeProvider, worker: None
) -> None:
    draft, invoice = sent_lines(
        fake, ("Repair", "100.00"), ("Late fee", "50.00"), ("Sign", "25.00")
    )
    first, second, third = charges_of(draft)
    for charge, amount in ((second, "50.00"), (first, "100.00")):
        outcome = manual(charge, amount)
        assert outcome.ok and not outcome.mark_due
    assert not one_off_service.invoice_covered(fresh(invoice))
    last = manual(third, "25.00")
    assert last.ok and last.mark_due
    assert one_off_service.invoice_covered(fresh(invoice))
    assert paid_states(draft) == [
        (D("100.00"), "paid"),
        (D("50.00"), "paid"),
        (D("25.00"), "paid"),
    ]


def test_one_payment_of_the_whole_invoice_on_the_first_line_is_marked_at_once(
    fake: FakeProvider, worker: None
) -> None:
    draft, invoice = owners_invoice(fake)
    repair, late_fee = charges_of(draft)
    post_wire(repair, "350.00", "WIRE-350")
    [job] = run_queued_jobs()
    assert (job.kind, job.entity_id) == (JobKind.ONE_OFF_PAYMENT_MARK, repair.pk)
    assert len(marks(fake)) == 1
    assert paid_states(draft) == [(D("300.00"), "paid"), (D("50.00"), "paid")]
    assert AuditLog.objects.filter(
        action=one_off_service.ALLOCATED_ACTION, entity_id=str(late_fee.pk)
    ).exists()
    assert (flag_of(repair), flag_of(late_fee)) == ("", "")


def test_coverage_is_to_the_cent(fake: FakeProvider, worker: None) -> None:
    draft, invoice = owners_invoice(fake)
    repair, late_fee = charges_of(draft)
    assert not manual(repair, "299.99").mark_due
    short = manual(late_fee, "50.00")
    assert short.ok and not short.mark_due
    assert not one_off_service.invoice_covered(fresh(invoice))
    assert flag_of(late_fee) == offline_marking.PARTIAL_AMOUNT
    cent = manual(repair, "0.01")
    assert cent.ok and cent.mark_due
    assert (
        one_off_service.mark_provider_invoice(repair.pk, cent.payment.pk, actor=None).action
        == rent_run.MarkAction.MARKED
    )
    assert len(marks(fake)) == 1
    assert (flag_of(repair), flag_of(late_fee)) == ("", "")


def test_the_first_line_overpaid_by_a_cent_reaches_the_later_line_and_marks(
    fake: FakeProvider, worker: None
) -> None:
    draft, _ = owners_invoice(fake)
    repair, _late_fee = charges_of(draft)
    assert not manual(repair, "299.99").mark_due
    assert manual(repair, "50.01").mark_due
    assert paid_states(draft) == [(D("300.00"), "paid"), (D("50.00"), "paid")]


def test_a_later_line_takes_at_most_its_own_balance(fake: FakeProvider, worker: None) -> None:
    draft, _ = owners_invoice(fake)
    repair, late_fee = charges_of(draft)
    over = manual(late_fee, "50.01")
    assert (over.ok, over.message, over.field) == (False, rent_run.PAY_OVER_BALANCE, "amount")
    # What the first line's payment already paid of it is not owed again.
    assert manual(repair, "320.00").ok
    assert fresh(late_fee).balance == D("30.00")
    again = manual(late_fee, "30.01")
    assert (again.ok, again.message) == (False, rent_run.PAY_OVER_BALANCE)
    assert manual(late_fee, "30.00").mark_due
    nothing = manual(late_fee, "1.00")
    assert (nothing.ok, nothing.message) == (False, rent_run.PAY_NOTHING_DUE)
    on_first = manual(repair, "0.01")
    assert (on_first.ok, on_first.message) == (False, rent_run.PAY_NOTHING_DUE)
    assert sum((row[0] for row in paid_states(draft)), D("0")) == D("350.00")
    assert not Payment.objects.filter(amount__in=[D("50.01"), D("30.01")]).exists()


def test_a_line_paid_on_its_own_is_skipped_by_the_first_lines_overflow(
    fake: FakeProvider, worker: None
) -> None:
    draft, _ = sent_lines(fake, ("Repair", "100.00"), ("Late fee", "50.00"), ("Sign", "25.00"))
    first, second, _third = charges_of(draft)
    assert manual(second, "50.00").ok
    outcome = manual(first, "125.00")
    assert outcome.ok and outcome.mark_due
    assert paid_states(draft) == [
        (D("100.00"), "paid"),
        (D("50.00"), "paid"),
        (D("25.00"), "paid"),
    ]
    refused = manual(first, "0.01")
    assert (refused.ok, refused.message) == (False, rent_run.PAY_NOTHING_DUE)


def test_a_waived_later_line_lowers_what_covers_the_invoice(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(
        operator,
        lease,
        [line("Repair", "100.00"), line("Late fee", "50.00"), line("Sign", "25.00")],
    )
    invoice = sent(draft).invoice
    assert invoice is not None
    first, second, third = charges_of(draft)
    # Not at the provider yet: the line may be waived; the provider invoice leaves it out.
    assert one_off_service.waive(third.pk, note="Goodwill.", actor=user(Role.ADMIN)).ok
    run_queued_jobs()
    assert created_invoices(fake)[0].amounts.total == D("150.00")
    Invoice.objects.filter(pk=invoice.pk).update(total=D("150.00"), subtotal=D("150.00"))
    assert one_off_service.invoice_billed(fresh(invoice)) == D("150.00")
    assert not manual(second, "50.00").mark_due
    last = manual(first, "100.00")
    assert last.ok and last.mark_due
    assert one_off_service.invoice_covered(fresh(invoice))
    assert fresh(third).status == OneOffChargeStatus.WAIVED


def test_a_builder_invoice_whose_total_differs_from_its_lines_is_not_marked(
    fake: FakeProvider, worker: None
) -> None:
    draft, invoice = owners_invoice(fake)
    repair, late_fee = charges_of(draft)
    Invoice.objects.filter(pk=invoice.pk).update(total=D("360.00"), subtotal=D("360.00"))
    assert not manual(repair, "300.00").mark_due
    covered = manual(late_fee, "50.00")
    assert covered.ok and not covered.mark_due
    assert flag_of(late_fee) == offline_marking.TOTAL_DIFFERS
    assert marks(fake) == []


def test_waive_on_a_later_line_is_refused_while_the_invoice_is_live(
    fake: FakeProvider, worker: None
) -> None:
    draft, invoice = owners_invoice(fake)
    repair, late_fee = charges_of(draft)
    assert one_off_service.live_provider_invoice(late_fee) is not None
    outcome = one_off_service.waive(late_fee.pk, note="Goodwill.", actor=user(Role.ADMIN))
    assert (outcome.ok, outcome.message) == (False, one_off_service.WAIVE_LIVE_INVOICE)
    admin, _ = client_for(Role.ADMIN)
    page = admin.get(f"/one-off-charges/{late_fee.pk}/").content.decode()
    assert 'id="waive-refusal"' in page and invoice.invoice_number in page
    waive_page = admin.get(f"/one-off-charges/{late_fee.pk}/waive/").content.decode()
    assert one_off_service.WAIVE_LIVE_INVOICE in waive_page
    admin.post(f"/one-off-charges/{late_fee.pk}/waive/", {"note": "Goodwill."})
    assert fresh(late_fee).status == OneOffChargeStatus.OWED
    for charge in (repair, late_fee):
        assert (
            'id="link-invoice"' not in admin.get(f"/one-off-charges/{charge.pk}/").content.decode()
        )
        assert list(one_off_service.invoices_of(charge)) == [invoice]


def test_the_delete_remedy_is_refused_on_a_waived_line_of_an_invoice_billing_others(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    sent(draft)
    _first, second = charges_of(draft)
    # Waived before the invoice reached the provider, which then made it with the first.
    assert one_off_service.waive(second.pk, note="Goodwill.", actor=user(Role.ADMIN)).ok
    run_queued_jobs()
    assert one_off_service.live_provider_invoice(fresh(second)) is not None
    assert one_off_service.delete_invoice_refusal(fresh(second)) == (
        one_off_service.DELETE_INVOICE_SHARED
    )
    outcome = one_off_service.delete_waived_invoice(second.pk, actor=user(Role.ADMIN))
    assert (outcome.ok, outcome.message) == (False, one_off_service.DELETE_INVOICE_SHARED)
    assert not Job.objects.filter(kind=JobKind.INVOICE_DELETE).exists()
    assert [row.pk for row in one_off_service.waived_with_live_invoice()] == [second.pk]


def test_a_failed_mark_flags_the_paying_line_and_its_retry_clears_both(
    fake: FakeProvider, worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft, invoice = owners_invoice(fake)
    repair, late_fee = charges_of(draft)
    assert manual(repair, "300.00").ok
    fake.fail_next("mark_invoice_paid_cash_or_check", ProviderUnavailable(503, "unavailable"))
    post_wire(late_fee, "50.00", "WIRE-50")
    [job] = run_queued_jobs()
    assert job.status in (JobStatus.FAILED, JobStatus.DEAD)
    assert flag_of(late_fee).startswith("Stax invoice not marked paid")
    assert flag_of(repair) == offline_marking.PARTIAL_AMOUNT
    assert one_off_service.retry_wait(fresh(late_fee)) is not None
    assert one_off_service.retry_wait(fresh(repair)) is None
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW + timedelta(minutes=2))
    result = one_off_service.retry_provider_mark(late_fee.pk, actor=None)
    assert result.action == rent_run.MarkAction.MARKED
    assert len(marks(fake)) == 2
    assert (flag_of(repair), flag_of(late_fee)) == ("", "")
    assert fresh(invoice).provider_status == "PAID"


def test_invoice_line_links_check_exits_0_on_clean_data_and_1_naming_an_orphan(
    fake: FakeProvider, worker: None
) -> None:
    draft, invoice = owners_invoice(fake)
    _repair, late_fee = charges_of(draft)
    out = io.StringIO()
    call_command("invoice_line_links", "--check", stdout=out)
    assert "Every builder invoice line names its charge." in out.getvalue()
    InvoiceLineItem.objects.filter(invoice=invoice, one_off_charge=late_fee).update(
        one_off_charge=None
    )
    out = io.StringIO()
    with pytest.raises(CommandError) as raised:
        call_command("invoice_line_links", "--check", stdout=out)
    assert raised.value.returncode == 1
    listing = out.getvalue()
    assert f"{invoice.invoice_number}: line 2 names no charge." in listing
    assert f"{invoice.invoice_number}: charge {late_fee.pk} has no line on it." in listing
    # Without --check it lists and exits 0.
    out = io.StringIO()
    call_command("invoice_line_links", stdout=out)
    assert "2 missing link(s)" in out.getvalue()


# --- PLAN-012b-b D4 to D8: the Rent line ------------------------------------------------------

OCTOBER = date(2026, 10, 1)
NOVEMBER = date(2026, 11, 1)
#: 14–31 Oct, 18 of 31 days of $1,000.00, rounded once (ADR-0012 §2).
FIRST_MONTH = D("580.65")
FIRST_TEXT = "14–31 Oct, 18 of 31 days of $1,000.00 = $580.65"


def a_rent_lease(operator: Operator, **fields: Any) -> Lease:
    """A wire lease of ``operator`` starting 14 October 2026 (rent $1,000.00, debit day
    1), saved the day before it starts: no charge yet."""
    values: dict[str, Any] = {
        "operator": operator,
        "started_on": date(2026, 10, 14),
        "debit_day": 1,
        "payment_channel": "wire",
        "rent": D("1000.00"),
    }
    values.update(fields)
    return make_lease(**values)


def rent_line(
    lease: Lease,
    month: date = OCTOBER,
    unit: str | None = None,
    *,
    description: str = "",
    quantity: str | None = None,
) -> LineInput:
    return LineInput(
        item="",
        description=description,
        quantity=D(quantity) if quantity is not None else None,
        unit_amount=D(unit) if unit is not None else None,
        rent_lease=lease.pk,
        rent_month=month,
    )


def move_in(fake: FakeProvider) -> tuple[Operator, Lease, list[LineInput]]:
    """The move-in invoice's lines: a $500.00 deposit, a $450.00 sign fee and the first
    rent (prorated)."""
    from tests.test_setup_charges import deposit_type, fee

    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    return (
        operator,
        lease,
        [
            line(deposit_type().name, "500.00"),
            line(fee("sign_fee").name, "450.00"),
            rent_line(lease),
        ],
    )


def refused(operator: Operator, lease: Lease | None, lines: list[LineInput]) -> str:
    outcome = invoice_builder.create_draft(
        operator, lease, MEMO, None, lines, user(Role.BILLING), today=TODAY
    )
    assert not outcome.ok
    return outcome.message


def the_rent(lease: Lease, month: date = OCTOBER) -> RentCharge:
    return RentCharge.objects.get(lease=lease, period_month=month)


def test_the_rent_line_defaults_to_the_prorated_first_month(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    draft = a_draft(operator, lease, [rent_line(lease)])
    [row] = invoice_builder.lines_of(draft)
    label = lease.store.external_label
    assert (row.rent_lease_id, row.rent_month, row.fee_type_id) == (lease.pk, OCTOBER, None)
    assert (row.item, row.description) == (f"Rent October 2026, store {label}", FIRST_TEXT)
    assert (row.quantity, row.unit_amount, row.line_total) == (D(1), FIRST_MONTH, FIRST_MONTH)
    # A full month is the rent in effect, without a proration text.
    later = a_draft(operator, lease, [rent_line(lease, NOVEMBER)])
    [full] = invoice_builder.lines_of(later)
    assert (full.unit_amount, full.description) == (D("1000.00"), "")
    assert not RentCharge.objects.exists() and not fake.calls


def test_the_rent_line_amount_is_editable_within_the_money_bounds(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    draft = a_draft(operator, lease, [rent_line(lease, unit="600.00", description="Agreed")])
    [row] = invoice_builder.lines_of(draft)
    assert (row.unit_amount, row.description) == (D("600.00"), "Agreed")
    edited = a_draft(operator, lease, [rent_line(lease, NOVEMBER, unit="900.00")])
    assert invoice_builder.lines_of(edited)[0].description == ""
    for bad in ("0.00", "0.001", "1000000.00"):
        message = refused(operator, lease, [rent_line(lease, unit=bad)])
        assert message == invoice_builder.LINE_UNIT_AMOUNT.format(line=1)
    message = refused(operator, lease, [rent_line(lease, quantity="2")])
    assert message == invoice_builder.LINE_RENT_QUANTITY.format(line=1)


def test_the_rent_month_must_be_a_month_of_the_lease(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    Lease.objects.filter(pk=lease.pk).update(ended_on=date(2026, 12, 15))
    label = lease.store.external_label
    assert refused(operator, lease, [rent_line(lease, date(2026, 10, 2))]) == (
        invoice_builder.LINE_RENT_MONTH.format(line=1)
    )
    assert refused(operator, lease, [rent_line(lease, date(2026, 9, 1))]) == (
        invoice_builder.LINE_RENT_BEFORE_START.format(
            line=1, store=label, start="October 2026", month="September 2026"
        )
    )
    assert refused(operator, lease, [rent_line(lease, date(2027, 1, 1))]) == (
        invoice_builder.LINE_RENT_AFTER_END.format(
            line=1, store=label, end="December 2026", month="January 2027"
        )
    )
    half = LineInput("", "", None, None, rent_lease=lease.pk)
    assert refused(operator, lease, [half]) == invoice_builder.LINE_RENT_INCOMPLETE.format(line=1)
    twice = a_rent_lease(operator, second_debit_day=15)
    assert refused(operator, twice, [rent_line(twice)]) == (
        invoice_builder.LINE_RENT_TWICE_MONTHLY.format(
            line=1, store=twice.store.external_label, month="October 2026"
        )
    )
    other = a_rent_lease(an_operator(fake, customer="cus-other", bank=None))
    assert refused(operator, lease, [rent_line(other)]) == (
        invoice_builder.LINE_RENT_LEASE.format(line=1)
    )
    assert not InvoiceDraft.objects.exists()


def test_a_pending_lease_not_started_yet_may_be_invoiced(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator, collection_status="prospective")
    draft = a_draft(operator, None, [rent_line(lease)])
    assert invoice_builder.lines_of(draft)[0].unit_amount == FIRST_MONTH


def test_one_rent_line_per_lease_and_month(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    words = {"store": lease.store.external_label, "month": "October 2026"}
    assert refused(operator, lease, [line(), rent_line(lease), rent_line(lease)]) == (
        invoice_builder.LINE_RENT_TWICE.format(line=3, **words)
    )
    first = a_draft(operator, lease, [line(), rent_line(lease)])
    assert refused(operator, lease, [rent_line(lease)]) == (
        invoice_builder.LINE_RENT_ON_DRAFT.format(line=1, draft=first.number, **words)
    )
    # Its own draft may be saved again; a cancelled draft holds nothing.
    outcome = invoice_builder.update_draft(
        first.pk, lease, MEMO, None, [rent_line(lease)], user(Role.BILLING), today=TODAY
    )
    assert outcome.ok, outcome.message
    assert invoice_builder.cancel(first.pk, user(Role.BILLING)).ok
    assert a_draft(operator, lease, [rent_line(lease)])
    # Rent alone has one line.
    assert refused(
        operator, lease, [rent_line(lease, NOVEMBER), rent_line(lease, date(2026, 12, 1))]
    ) == (invoice_builder.RENT_ALONE)


@pytest.mark.parametrize("paid", ["1000.00", "400.00"])
def test_a_rent_line_for_a_paid_or_partly_paid_month_is_refused(
    fake: FakeProvider, paid: str
) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    charge, _ = rent_run.create_month(lease, NOVEMBER, None)
    outcome = rent_run.record_manual_payment(
        charge.pk,
        method="wire",
        amount=D(paid),
        paid_on=TODAY,
        note="Wire received.",
        actor=None,
        today=TODAY,
    )
    assert outcome.ok, outcome.message
    assert refused(operator, lease, [rent_line(lease, NOVEMBER)]) == (
        invoice_builder.LINE_RENT_BILLED.format(
            line=1, store=lease.store.external_label, month="November 2026"
        )
    )


def test_a_rent_line_for_a_month_on_a_live_invoice_is_refused(fake: FakeProvider) -> None:
    operator, lease, lines = move_in(fake)
    sent(a_draft(operator, lease, lines))
    assert refused(operator, lease, [line(), rent_line(lease)]) == (
        invoice_builder.LINE_RENT_BILLED.format(
            line=2, store=lease.store.external_label, month="October 2026"
        )
    )


def test_a_rent_line_on_an_autopay_lease_is_refused(fake: FakeProvider) -> None:
    from tests.test_lease_end import a_schedule

    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    a_schedule(lease)
    assert refused(operator, lease, [rent_line(lease)]) == (
        invoice_builder.LINE_RENT_AUTOPAY.format(
            line=1, store=lease.store.external_label, month="October 2026"
        )
    )


def test_send_creates_the_charge_and_links_the_move_in_invoice(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease, lines = move_in(fake)
    draft = a_draft(operator, lease, lines, due=date(2026, 10, 20))
    outcome = sent(draft)
    invoice = outcome.invoice
    assert invoice is not None
    charge = the_rent(lease)
    assert (charge.amount_due, charge.due_on, charge.status) == (
        FIRST_MONTH,
        date(2026, 10, 20),
        "due",
    )
    assert (charge.proration_text, charge.billed_by_invoice_id) == (FIRST_TEXT, invoice.pk)
    deposit, sign = charges_of(draft)
    assert (invoice.one_off_charge_id, invoice.rent_charge_id) == (deposit.pk, None)
    assert invoice.total == D("500.00") + D("450.00") + FIRST_MONTH
    items = list(InvoiceLineItem.objects.filter(invoice=invoice).order_by("position"))
    assert [(item.one_off_charge_id, item.rent_charge_id) for item in items] == [
        (deposit.pk, None),
        (sign.pk, None),
        (None, charge.pk),
    ]
    assert outcome.job is not None and outcome.job.entity_id == deposit.pk
    assert list(rent_run.charge_invoices(charge)) == [invoice]
    created = AuditLog.objects.get(action="rent_charge.created", entity_id=str(charge.pk))
    assert created.after is not None and created.after["invoice_draft"] == str(draft.pk)
    assert AuditLog.objects.filter(
        action="rent_charge.billed_by_invoice", entity_id=str(charge.pk)
    ).exists()
    sent_row = AuditLog.objects.get(action=invoice_builder.SENT_ACTION)
    assert sent_row.after is not None and sent_row.after["rent_charges"] == [str(charge.pk)]
    run_queued_jobs()
    [request] = created_invoices(fake)
    rent_item = request.amounts.line_items[2]
    label = lease.store.external_label
    assert (rent_item.description, rent_item.unit_price, rent_item.details) == (
        f"Rent October 2026, store {label}",
        FIRST_MONTH,
        FIRST_TEXT,
    )
    assert request.amounts.total == fresh(invoice).total
    validation.amounts(request.amounts)


def test_the_run_and_the_catch_up_skip_a_month_billed_by_invoice(
    fake: FakeProvider, worker: None
) -> None:
    """Rent invoiced before the lease starts: the run on the debit day creates nothing
    for that month."""
    operator, lease, lines = move_in(fake)
    sent(a_draft(operator, lease, lines))
    before = RentCharge.objects.count()
    report = rent_run.create_charges(date(2026, 10, 14), actor=None)
    assert not report.caught_up and not report.created
    assert RentCharge.objects.count() == before == 1
    report = rent_run.create_charges(date(2026, 11, 1), actor=None)
    assert [charge.period_month for charge in report.created] == [NOVEMBER]
    assert the_rent(lease, NOVEMBER).billed_by_invoice_id is None


def test_rent_invoiced_after_the_run_takes_the_existing_charge(
    fake: FakeProvider, worker: None
) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    rent_run.create_charges(date(2026, 10, 14), actor=None)
    charge = the_rent(lease)
    assert charge.amount_due == FIRST_MONTH
    draft = a_draft(operator, lease, [rent_line(lease)])
    assert invoice_builder.lines_of(draft)[0].unit_amount == FIRST_MONTH
    invoice = sent(draft).invoice
    assert invoice is not None
    assert RentCharge.objects.filter(lease=lease).count() == 1
    taken = the_rent(lease)
    assert (taken.pk, taken.billed_by_invoice_id) == (charge.pk, invoice.pk)
    # Rent alone: the invoice is the rent charge's, and so is its job.
    assert (invoice.rent_charge_id, invoice.one_off_charge_id) == (charge.pk, None)
    job = Job.objects.get(kind=JobKind.INVOICE_CREATE)
    assert (job.entity_type, job.entity_id) == ("rent_charge", charge.pk)
    run_queued_jobs()
    [request] = created_invoices(fake)
    assert [item.unit_price for item in request.amounts.line_items] == [FIRST_MONTH]
    assert request.reference == invoice.invoice_number and request.memo == MEMO
    assert fresh(invoice).internal_status == InvoiceStatus.SENT
    # A second line naming another amount than the charge's is refused.
    other = a_rent_lease(operator)
    rent_run.create_charges(date(2026, 10, 14), actor=None)
    assert refused(operator, other, [rent_line(other, unit="100.00")]) == (
        invoice_builder.LINE_RENT_AMOUNT.format(
            line=1,
            store=other.store.external_label,
            month="October 2026",
            amount="$580.65",
        )
    )


def test_a_payment_on_the_invoice_pays_the_rent_charge_in_line_order(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease, lines = move_in(fake)
    invoice = sent(a_draft(operator, lease, lines)).invoice
    assert invoice is not None
    run_queued_jobs()
    pay(fake, fresh(invoice), "1000.00")
    charge = the_rent(lease)
    # 500.00 deposit, 450.00 sign fee, then 50.00 of the rent.
    assert (charge.amount_paid, charge.status) == (D("50.00"), "partial")
    assert AuditLog.objects.filter(
        action="rent_charge.payment_allocated", entity_id=str(charge.pk)
    ).exists()
    pay(fake, fresh(invoice), str(FIRST_MONTH - D("50.00")))
    charge = the_rent(lease)
    assert (charge.amount_paid, charge.status, charge.balance) == (FIRST_MONTH, "paid", D("0"))
    # The rent charge has no payment row of its own: the invoice's paid it.
    assert not Payment.objects.filter(rent_charge=charge).exists()


def test_a_manual_payment_on_the_rent_line_counts_towards_the_invoice(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease, lines = move_in(fake)
    draft = a_draft(operator, lease, lines)
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    deposit, sign = charges_of(draft)
    for row in (deposit, sign):
        outcome = one_off_service.record_payment(
            row.pk,
            method="wire",
            amount=row.price,
            paid_on=TODAY,
            note="Wire received.",
            actor=None,
            today=TODAY,
        )
        assert outcome.ok and not outcome.mark_due, outcome.message
    outcome = rent_run.record_manual_payment(
        the_rent(lease).pk,
        method="wire",
        amount=FIRST_MONTH,
        paid_on=TODAY,
        note="Wire received.",
        actor=None,
        today=TODAY,
        defer_mark=True,
    )
    assert outcome.ok and outcome.mark_due, outcome.message
    assert one_off_service.invoice_covered(fresh(invoice))
    assert one_off_service.invoice_billed(fresh(invoice)) == fresh(invoice).total


def test_the_grid_names_the_invoice_and_shows_rent_paid_in_advance(
    fake: FakeProvider, worker: None
) -> None:
    from ledger.services import grid

    operator, lease, lines = move_in(fake)
    invoice = sent(a_draft(operator, lease, lines)).invoice
    assert invoice is not None
    run_queued_jobs()
    built = grid.build(grid.parse_query({"from": "2026-10", "to": "2026-10"}, TODAY), TODAY)
    [row] = [row for row in built.rows if row.store.pk == lease.store_id]
    [cell] = row.cells
    assert cell.text == f"due {FIRST_MONTH:,.2f} {invoice.invoice_number}"
    pay(fake, fresh(invoice), str(fresh(invoice).total))
    built = grid.build(grid.parse_query({"from": "2026-10", "to": "2026-10"}, TODAY), TODAY)
    [row] = [row for row in built.rows if row.store.pk == lease.store_id]
    assert row.cells[0].status == "paid"
    assert row.cells[0].text == f"paid {FIRST_MONTH:,.2f} {invoice.invoice_number}"


def test_the_pnl_counts_the_rent_line_as_rent_and_the_deposit_nowhere(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease, lines = move_in(fake)
    invoice = sent(a_draft(operator, lease, lines)).invoice
    assert invoice is not None
    run_queued_jobs()
    pay(fake, fresh(invoice), str(fresh(invoice).total))
    selected = pnl.parse_query({"from": "2026-10", "to": "2026-10", "group": "store"})
    [row] = [row for row in pnl.rows(selected) if row.store_id == lease.store_id]
    assert (row.rent_billed, row.rent_collected) == (FIRST_MONTH, FIRST_MONTH)
    assert (row.fees_billed, row.fees_collected) == (D("450.00"), D("450.00"))
    assert pnl.deposits_held(selected) == D("500.00")
    totals = payments_ledger.totals(Payment.objects.all())
    assert (totals.settled, totals.deposits_held) == (D("450.00") + FIRST_MONTH, D("500.00"))


def test_charge_now_and_cancel_of_rent_alone_are_refused(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    draft = a_draft(operator, lease, [rent_line(lease)])
    outcome = invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=True)
    assert (outcome.ok, outcome.message) == (False, invoice_builder.RENT_ALONE_CHARGE_NOW)
    sent(draft)
    outcome = invoice_builder.cancel(draft.pk, user(Role.BILLING), note="Wrong month.")
    assert (outcome.ok, outcome.message) == (False, invoice_builder.RENT_ALONE_CANCEL)


def test_the_draft_page_offers_the_rent_kind_and_saves_a_rent_line(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    draft = a_draft(operator, lease, [line()])
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/invoices/drafts/{draft.pk}/").content.decode()
    for field in ("kind", "rent_lease", "rent_month"):
        assert f'id="id_lines-0-{field}"' in page
    assert 'type="month"' in page and 'id="rent-line-help"' in page
    data = {
        "lease": str(lease.pk),
        "memo": MEMO,
        "due_date": "",
        "lines-TOTAL_FORMS": "2",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "50",
        "lines-0-kind": "item",
        "lines-0-item": "Sign repair",
        "lines-0-unit_amount": "100.00",
        "lines-0-rent_lease": str(lease.pk),
        "lines-1-kind": "rent",
        "lines-1-rent_lease": str(lease.pk),
        "lines-1-rent_month": "2026-10",
    }
    client.post(f"/invoices/drafts/{draft.pk}/edit/", data)
    first, second = invoice_builder.lines_of(fresh(draft))
    assert (first.item, first.rent_lease_id) == ("Sign repair", None)
    assert (second.rent_lease_id, second.rent_month, second.unit_amount) == (
        lease.pk,
        OCTOBER,
        FIRST_MONTH,
    )
    page = client.get(f"/invoices/drafts/{draft.pk}/").content.decode()
    assert 'value="2026-10"' in page
    # Rent without its month is refused by the form, with nothing changed.
    data.pop("lines-1-rent_month")
    client.post(f"/invoices/drafts/{draft.pk}/edit/", data)
    page = client.get(f"/invoices/drafts/{draft.pk}/").content.decode()
    assert "Choose the lease and the month of the rent." in page
    assert len(invoice_builder.lines_of(fresh(draft))) == 2
