"""Setup charges (docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md Batch A: D1 to D6, D9,
D12; §6 test plan).

The service, the lease checklist, the setup charge pages, the manual payment and its
provider marking through the worker, the invoice link, the payment provider's
transactions for a setup invoice (pay, return, refund), the deposit rules of the
payments ledger and the role gates. Nothing reaches a network: the payment provider is
a :class:`~payments.fake.FakeProvider`.

Fee types are made by :func:`fee` (get or create) and never read from the seed: a
``transaction=True`` test empties the database after it, seed included.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import Group
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AuditLog,
    FeeType,
    InsurancePremium,
    Invoice,
    InvoiceDraft,
    Lease,
    OneOffCharge,
    OneOffChargeStatus,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import one_off_charges as service
from ledger.services import payments_ledger, refunds, rent_run, returns
from ledger.services.one_off_charges import ChargeInput, SetupRefused
from payments import jobs, offline_marking
from payments.errors import ProviderUnavailable
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind, JobStatus
from payments.provider import InvoiceRecord, TransactionRecord
from tests.auth_helpers import iter_routes
from tests.job_helpers import run_queued_jobs
from tests.test_lease_services import _race, lease_fields, make_lease, make_operator
from tests.test_operator_views import client_for
from tests.test_webhook_handlers import deliver
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="row locks and concurrent transactions need PostgreSQL",
)

D = Decimal
DAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SETTLED_AT = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)
CUSTOMER = "cus-setup-1"
INVOICE_REF = "fake-inv-setup-0001"
TXN = "txn-setup-0001"
NOTE = "Wire received from the operator's bank."
CARD = "4111 1111 1111 1111"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


@pytest.fixture
def pinned_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: DAY)


# --- Builders ----------------------------------------------------------------------------


def fee(code: str = "sign_fee", **defaults: Any) -> FeeType:
    """A catalogue item as seeded: active and shown in the setup checklist (PLAN-012d D9;
    ``show_in_setup=False`` for one the checklist hides)."""
    values: dict[str, Any] = {
        "name": code.replace("_", " ").capitalize(),
        "active": True,
        "show_in_setup": True,
    }
    values.update(defaults)
    row, _ = FeeType.objects.get_or_create(code=code, defaults=values)
    if any(getattr(row, name) != value for name, value in values.items()):
        FeeType.objects.filter(pk=row.pk).update(**values)
        row.refresh_from_db()
    return row


def deposit_type() -> FeeType:
    return fee("security_deposit", name="Security deposit", refundable=True)


def a_lease(customer: str | None = None, **overrides: Any) -> Lease:
    """A lease of its own store and operator; ``customer`` is the operator's provider
    customer ID (one operator per ID)."""
    if "operator" not in overrides:
        tag = uuid.uuid4().hex[:8]
        overrides["operator"] = make_operator(email=f"{tag}@example.com", customer=customer)
    return make_lease(**overrides)


def item(fee_type: FeeType | None = None, price: str = "1000.00", **fields: Any) -> ChargeInput:
    values: dict[str, Any] = {
        "fee_type": fee_type or fee(),
        "price": D(price),
        "cost": D("500.00"),
        "vendor": "Bright Signs LLC",
        "due_date": DAY,
    }
    values.update(fields)
    return ChargeInput(**values)


def a_charge(price: str = "1000.00", fee_type: FeeType | None = None, **lease: Any) -> OneOffCharge:
    outcome = service.add_charge(a_lease(**lease).pk, item(fee_type, price), actor=None)
    assert outcome.ok and outcome.charge is not None, outcome.message
    return outcome.charge


def pay(charge: OneOffCharge, amount: str, **overrides: Any) -> rent_run.PaymentOutcome:
    values: dict[str, Any] = {
        "method": PaymentMethod.WIRE,
        "amount": D(amount),
        "paid_on": DAY,
        "note": NOTE,
        "actor": None,
        "today": DAY,
    }
    values.update(overrides)
    return service.record_payment(charge.pk, **values)


def lease_of(charge: OneOffCharge) -> Lease:
    """The lease of a setup charge, which always has one (PLAN-010d D1 made it nullable)."""
    assert charge.lease is not None
    return charge.lease


def fresh(charge: OneOffCharge) -> OneOffCharge:
    return OneOffCharge.objects.get(pk=charge.pk)


def unlinked_invoice(
    operator_id: uuid.UUID | None,
    total: str = "1000.00",
    number: str = "INV-2026-0101",
    ref: str = "",
) -> Invoice:
    assert operator_id is not None
    invoice = Invoice.objects.create(
        invoice_number=number,
        operator_id=operator_id,
        issue_date=DAY,
        due_date=DAY,
        subtotal=D(total),
        total=D(total),
    )
    if ref:
        ExternalRef.objects.create(
            entity_type="invoice",
            entity_id=invoice.pk,
            provider="stax_pay",
            external_kind="invoice",
            external_id=ref,
        )
    return invoice


def provider_invoice(fake: FakeProvider, total: str = "1000.00", status: str = "SENT") -> None:
    fake.invoices[INVOICE_REF] = InvoiceRecord(
        external_id=INVOICE_REF,
        raw_status=status,
        fetched_at=NOW,
        customer_external_id=CUSTOMER,
        total=D(total),
        raw_summary={"invoice_date_at": "2026-10-15"},
    )


def invoiced(fake: FakeProvider, price: str = "1000.00") -> OneOffCharge:
    """A sign fee charge linked to the provider invoice ``INVOICE_REF``."""
    charge = a_charge(price, customer=CUSTOMER)
    invoice = unlinked_invoice(lease_of(charge).operator_id, price, ref=INVOICE_REF)
    provider_invoice(fake, price)
    outcome = service.link_invoice(charge.pk, invoice.pk, confirm=False, note="", actor=None)
    assert outcome.ok, outcome.message
    fake.calls.clear()
    return fresh(charge)


def provider_txn(fake: FakeProvider, total: str = "1000.00", **fields: Any) -> TransactionRecord:
    values: dict[str, Any] = {
        "method": "bank",
        "kind": "charge",
        "invoice_external_id": INVOICE_REF,
        "customer_external_id": CUSTOMER,
        "success": True,
        "created_at": datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
    }
    values.update(fields)
    external_id = values.pop("external_id", TXN)
    record = TransactionRecord(
        external_id=external_id, raw_status="", fetched_at=NOW, total=D(total), **values
    )
    fake.transactions[external_id] = record
    return record


def actions(entity_id: object) -> list[str]:
    return list(
        AuditLog.objects.filter(entity_id=str(entity_id))
        .order_by("id")
        .values_list("action", flat=True)
    )


# --- Seeds (D1, D2, D9) -------------------------------------------------------------------

SEED = importlib.import_module("ledger.migrations.0013_seed_fee_types")


def test_the_seed_makes_the_seven_fee_types_with_first_year_insurance_inactive() -> None:
    SEED.seed(django_apps, None)
    rows = {
        row.code: row
        for row in FeeType.objects.filter(code__in=[code for code, *_ in SEED.FEE_TYPES])
    }
    assert set(rows) == {
        "access_fee",
        "security_deposit",
        "sign_fee",
        "first_year_insurance",
        "architecture_fee",
        "business_purchase_fee",
        "phone_service",
    }
    insurance = rows["first_year_insurance"]
    assert (insurance.active, insurance.default_cost, insurance.default_price) == (
        False,
        D("300.00"),
        D("695.00"),
    )
    assert {code for code, row in rows.items() if row.refundable} == {"security_deposit"}
    for code, row in rows.items():
        if code != "first_year_insurance":
            assert (row.active, row.default_cost, row.default_price) == (True, None, None), code
    assert [row.code for row in sorted(rows.values(), key=lambda r: r.sort_order)] == [
        code for code, *_ in SEED.FEE_TYPES
    ]


def test_the_seed_leaves_an_existing_type_as_it_is() -> None:
    FeeType.objects.filter(code="sign_fee").delete()
    fee("sign_fee", name="Front sign", default_price=D("900.00"))
    SEED.seed(django_apps, None)
    row = FeeType.objects.get(code="sign_fee")
    assert (row.name, row.default_price) == ("Front sign", D("900.00"))


def test_premium_costs_are_set_for_2025_and_2026_only_where_empty() -> None:
    InsurancePremium.objects.all().delete()
    for year, cost in ((2024, None), (2025, None), (2026, D("250.00")), (2027, None)):
        InsurancePremium.objects.create(policy_year=year, amount=D("695.00"), cost=cost)
    SEED.seed(django_apps, None)
    costs = dict(InsurancePremium.objects.values_list("policy_year", "cost"))
    assert costs == {2024: None, 2025: D("300.00"), 2026: D("250.00"), 2027: None}


def test_the_admin_group_gets_view_add_and_change_on_the_two_admin_models() -> None:
    Group.objects.get_or_create(name="admin")
    SEED.seed(django_apps, None)
    codenames = set(
        Group.objects.get(name="admin")
        .permissions.filter(content_type__model__in=["feetype", "insurancepremium"])
        .values_list("codename", flat=True)
    )
    assert codenames == {
        f"{action}_{model}"
        for model in ("feetype", "insurancepremium")
        for action in ("view", "add", "change")
    }
    for name in ("billing", "viewer"):
        group, _ = Group.objects.get_or_create(name=name)
        assert not group.permissions.filter(content_type__model="feetype").exists()


# --- Creation from the checklist (D5) ------------------------------------------------------


def checklist(
    items: list[ChargeInput], key: uuid.UUID | None = None
) -> tuple[Lease, bool, list[OneOffCharge]]:
    return service.create_lease_with_checklist(
        lease_fields(
            operator=make_operator(email=f"{uuid.uuid4().hex[:8]}@example.com", customer=None)
        ),
        rent=D("3500.00"),
        creation_key=key or uuid.uuid4(),
        items=items,
        actor=None,
    )


def test_the_checklist_creates_exactly_the_ticked_charges_with_the_typed_amounts() -> None:
    sign, access = fee("sign_fee"), fee("access_fee")
    lease, created, charges = checklist(
        [
            item(sign, "1000.00", cost=D("500.00"), vendor="Bright Signs"),
            item(access, "0.01", cost=None, vendor="", due_date=date(2026, 1, 31)),
        ]
    )
    assert created and len(charges) == 2
    rows = {
        row.fee_type.code: row
        for row in OneOffCharge.objects.filter(lease=lease)
        if row.fee_type is not None
    }
    assert set(rows) == {"sign_fee", "access_fee"}
    assert (rows["sign_fee"].price, rows["sign_fee"].cost, rows["sign_fee"].vendor) == (
        D("1000.00"),
        D("500.00"),
        "Bright Signs",
    )
    access_row = rows["access_fee"]
    assert (access_row.price, access_row.cost, access_row.due_date) == (
        D("0.01"),
        None,
        date(2026, 1, 31),
    )
    assert all(row.status == OneOffChargeStatus.OWED for row in rows.values())
    assert all(row.balance == row.price for row in rows.values())
    assert actions(rows["sign_fee"].pk) == [service.CREATED_ACTION]


def test_an_empty_checklist_creates_the_lease_only() -> None:
    lease, created, charges = checklist([])
    assert created and charges == []
    assert not OneOffCharge.objects.filter(lease=lease).exists()


def test_a_resubmitted_checklist_makes_one_lease_and_one_set_of_charges() -> None:
    key = uuid.uuid4()
    first, created, charges = checklist([item()], key)
    again, created_again, more = checklist([item()], key)
    assert (again.pk, created, created_again, len(charges), more) == (first.pk, True, False, 1, [])
    assert OneOffCharge.objects.filter(lease=first).count() == 1


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"vendor": f"Card {CARD}"}, service.TEXT_PAYMENT_DATA),
        ({"price": D("0.00")}, service.PRICE_NOT_POSITIVE),
        ({"price": D("10.001")}, service.NOT_CENTS),
        ({"cost": D("-1.00")}, service.COST_NEGATIVE),
    ],
)
def test_a_refused_row_saves_nothing_not_even_the_lease(
    override: dict[str, Any], message: str
) -> None:
    leases_before = Lease.objects.count()
    with pytest.raises(SetupRefused) as raised:
        checklist([item(fee("access_fee")), item(fee("sign_fee"), **override)])
    assert raised.value.message == message
    assert CARD not in str(raised.value)
    assert Lease.objects.count() == leases_before
    assert not OneOffCharge.objects.exists()


def test_an_inactive_fee_type_is_refused() -> None:
    inactive = fee("first_year_insurance", active=False)
    with pytest.raises(SetupRefused) as raised:
        checklist([item(inactive)])
    assert raised.value.message == service.FEE_TYPE_INACTIVE


def test_one_charge_per_fee_type_per_lease_but_a_legacy_row_may_sit_beside_it() -> None:
    charge = a_charge()
    again = service.add_charge(lease_of(charge).pk, item(), actor=None)
    assert (again.ok, again.message) == (False, service.ALREADY_ON_LEASE.format(name="Sign fee"))
    OneOffCharge.objects.create(
        lease=lease_of(charge),
        fee_type=charge.fee_type,
        price=D("50.00"),
        due_date=DAY,
        legacy=True,
    )
    assert OneOffCharge.objects.filter(lease=lease_of(charge)).count() == 2


# --- Manual payments (D6) --------------------------------------------------------------------


def test_a_wire_covering_the_price_pays_the_charge_and_a_partial_leaves_it_partial() -> None:
    whole, part = a_charge(), a_charge("1000.00", fee("access_fee"))
    assert pay(whole, "1000.00").ok
    assert pay(part, "400.00").ok
    whole, part = fresh(whole), fresh(part)
    assert (whole.status, whole.amount_paid, whole.balance) == (
        OneOffChargeStatus.PAID,
        D("1000.00"),
        D("0.00"),
    )
    assert (part.status, part.amount_paid, part.balance) == (
        OneOffChargeStatus.PARTIAL,
        D("400.00"),
        D("600.00"),
    )
    payment = Payment.objects.get(one_off_charge=whole)
    assert (payment.kind, payment.method, payment.status) == (
        PaymentKind.ONE_OFF,
        PaymentMethod.WIRE,
        PaymentStatus.SETTLED,
    )
    assert actions(whole.pk)[-1] == service.PAYMENT_ACTION


def test_a_price_of_one_cent_is_paid_by_one_cent() -> None:
    charge = a_charge("0.01")
    assert pay(charge, "0.01").ok
    assert fresh(charge).status == OneOffChargeStatus.PAID


@pytest.mark.parametrize(
    ("amount", "overrides", "message"),
    [
        ("1000.01", {}, rent_run.PAY_OVER_BALANCE),
        ("0", {}, rent_run.PAY_NOT_POSITIVE),
        ("10.005", {}, rent_run.PAY_NOT_CENTS),
        ("10.00", {"method": PaymentMethod.ACH}, rent_run.PAY_METHOD),
        ("10.00", {"note": "  "}, rent_run.PAY_NOTE_REQUIRED),
        ("10.00", {"paid_on": date(2026, 10, 16)}, rent_run.PAY_FUTURE),
        ("10.00", {"note": f"Paid with {CARD}"}, rent_run.PAY_NOTE_PAYMENT_DATA),
    ],
)
def test_a_refused_payment_records_nothing(
    amount: str, overrides: dict[str, Any], message: str
) -> None:
    charge = a_charge()
    outcome = pay(charge, amount, **overrides)
    assert (outcome.ok, outcome.message) == (False, message)
    assert not Payment.objects.exists()
    assert fresh(charge).status == OneOffChargeStatus.OWED


def test_a_paid_or_waived_charge_takes_no_payment() -> None:
    paid = a_charge()
    pay(paid, "1000.00")
    assert pay(paid, "1.00").message == rent_run.PAY_NOTHING_DUE
    waived = a_charge("1000.00", fee("access_fee"))
    assert service.waive(waived.pk, note="Owner decision.", actor=None).ok
    assert pay(waived, "1.00").message == rent_run.PAY_WAIVED
    assert Payment.objects.count() == 1


def test_a_resubmitted_payment_form_records_one_payment() -> None:
    charge = a_charge()
    key = uuid.uuid4()
    first = pay(charge, "300.00", creation_key=key)
    second = pay(charge, "300.00", creation_key=key)
    assert (first.ok, second.ok, second.replayed, second.message) == (
        True,
        True,
        True,
        rent_run.PAY_ALREADY,
    )
    assert Payment.objects.count() == 1


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_record_payment_clicks_make_one_payment() -> None:
    charge = a_charge()
    key = uuid.uuid4()
    results = _race(
        lambda: pay(charge, "1000.00", creation_key=key),
        lambda: pay(charge, "1000.00", creation_key=key),
    )
    assert sorted(result.replayed for result in results) == [False, True]
    assert Payment.objects.count() == 1
    assert fresh(charge).status == OneOffChargeStatus.PAID


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_checklist_submissions_make_one_lease_and_one_set() -> None:
    sign, access = fee("sign_fee"), fee("access_fee")
    fields = lease_fields(operator=make_operator(email="race@example.com", customer=None))
    key = uuid.uuid4()

    def submit() -> tuple[Lease, bool, list[OneOffCharge]]:
        return service.create_lease_with_checklist(
            fields,
            rent=D("3500.00"),
            creation_key=key,
            items=[item(sign), item(access)],
            actor=None,
        )

    results = _race(submit, submit)
    assert sorted(created for _, created, _ in results) == [False, True]
    assert Lease.objects.count() == 1
    assert OneOffCharge.objects.count() == 2


# --- Edit and waive (D6) ---------------------------------------------------------------------


def edit(charge: OneOffCharge, **changes: Any) -> service.Outcome:
    values: dict[str, Any] = {
        "cost": charge.cost,
        "price": charge.price,
        "vendor": charge.vendor,
        "due_date": charge.due_date,
        "actor": None,
    }
    values.update(changes)
    return service.edit_charge(charge.pk, **values)


def test_edit_changes_cost_price_vendor_and_due_date_and_is_audited() -> None:
    charge = a_charge()
    outcome = edit(
        charge, cost=None, price=D("1200.00"), vendor="Other Signs", due_date=date(2026, 11, 1)
    )
    assert (outcome.ok, outcome.message) == (True, service.SAVED)
    charge = fresh(charge)
    assert (charge.cost, charge.price, charge.vendor, charge.due_date, charge.balance) == (
        None,
        D("1200.00"),
        "Other Signs",
        date(2026, 11, 1),
        D("1200.00"),
    )
    entry = AuditLog.objects.get(action="one_off_charge.edited")
    assert entry.before is not None and entry.after is not None
    assert (entry.before["price"], entry.after["price"]) == ("1000.00", "1200.00")
    assert edit(charge).message == service.UNCHANGED


def test_the_price_is_locked_once_a_payment_exists_but_the_rest_may_change() -> None:
    charge = a_charge()
    pay(charge, "100.00")
    refused = edit(fresh(charge), price=D("2000.00"))
    assert (refused.ok, refused.message, refused.field) == (False, service.PRICE_LOCKED, "price")
    assert fresh(charge).price == D("1000.00")
    assert edit(fresh(charge), vendor="New vendor", cost=D("450.00")).ok
    assert fresh(charge).vendor == "New vendor"


def test_edit_refuses_a_card_number_and_a_waived_charge() -> None:
    charge = a_charge()
    outcome = edit(charge, vendor=f"Vendor {CARD}")
    assert (outcome.ok, outcome.message) == (False, service.TEXT_PAYMENT_DATA)
    service.waive(charge.pk, note="Waived.", actor=None)
    assert edit(fresh(charge), vendor="x").message == service.EDIT_WAIVED


def test_waive_needs_a_visible_note() -> None:
    charge = a_charge()
    for note in ("", "   ", "​​"):
        outcome = service.waive(charge.pk, note=note, actor=None)
        assert (outcome.ok, outcome.message) == (False, service.WAIVE_NOTE_REQUIRED)
    card = service.waive(charge.pk, note=f"See {CARD}", actor=None)
    assert (card.ok, card.message) == (False, service.TEXT_PAYMENT_DATA)
    assert fresh(charge).status == OneOffChargeStatus.OWED


def test_waive_with_a_note_marks_the_charge_waived_and_audits_it() -> None:
    charge = a_charge()
    user = client_for(Role.ADMIN)[1]
    outcome = service.waive(charge.pk, note="Owner waived the sign.", actor=user)
    assert (outcome.ok, outcome.message) == (True, service.WAIVED)
    charge = fresh(charge)
    assert (charge.status, charge.waive_note, charge.waived_by) == (
        OneOffChargeStatus.WAIVED,
        "Owner waived the sign.",
        user,
    )
    assert charge.waived_at is not None
    assert service.waive(charge.pk, note="Again.", actor=None).message == service.WAIVE_ALREADY
    assert "one_off_charge.waived" in actions(charge.pk)


def test_waive_is_refused_once_a_payment_settled() -> None:
    charge = a_charge()
    pay(charge, "1.00")
    outcome = service.waive(charge.pk, note="Too late.", actor=None)
    assert (outcome.ok, outcome.message) == (False, service.WAIVE_SETTLED)
    assert fresh(charge).status == OneOffChargeStatus.PARTIAL


# --- Linking an invoice: the rent rules (D6) ------------------------------------------------


def test_an_unlinked_invoice_of_the_operator_is_linked() -> None:
    charge = a_charge()
    invoice = unlinked_invoice(lease_of(charge).operator_id)
    assert [row.invoice for row in rent_run.unlinked_invoices(charge)] == [invoice]
    outcome = service.link_invoice(charge.pk, invoice.pk, confirm=False, note="", actor=None)
    assert (outcome.ok, outcome.message) == (
        True,
        rent_run.LINK_DONE.format(number="INV-2026-0101"),
    )
    invoice.refresh_from_db()
    assert (invoice.one_off_charge_id, invoice.rent_charge_id) == (charge.pk, None)
    entry = AuditLog.objects.get(action="invoice.linked")
    assert entry.after is not None and entry.after["one_off_charge"] == str(charge.pk)
    again = service.link_invoice(charge.pk, invoice.pk, confirm=False, note="", actor=None)
    assert (again.ok, again.message) == (True, rent_run.LINK_SAME)


def test_another_operators_invoice_or_one_linked_elsewhere_is_refused() -> None:
    charge = a_charge()
    stranger = unlinked_invoice(make_operator(email="other@example.com", customer=None).pk)
    rent_charge = RentCharge.objects.create(
        lease=lease_of(charge),
        period_month=date(2026, 10, 1),
        due_on=DAY,
        amount_due=D("1000.00"),
        status=RentChargeStatus.DUE,
    )
    taken = unlinked_invoice(lease_of(charge).operator_id, number="INV-2026-0102")
    Invoice.objects.filter(pk=taken.pk).update(rent_charge=rent_charge)
    for invoice in (stranger, taken):
        outcome = service.link_invoice(charge.pk, invoice.pk, confirm=True, note="x", actor=None)
        assert (outcome.ok, outcome.message) == (False, rent_run.LINK_CHOOSE)
    assert not Invoice.objects.filter(one_off_charge=charge).exists()
    # The rent charge's link page never offers an invoice of a setup charge either.
    linked = unlinked_invoice(lease_of(charge).operator_id, number="INV-2026-0103")
    service.link_invoice(charge.pk, linked.pk, confirm=False, note="", actor=None)
    assert linked.pk not in [row.invoice.pk for row in rent_run.unlinked_invoices(rent_charge)]
    refused = rent_run.link_invoice(rent_charge.pk, linked.pk, confirm=True, note="x", actor=None)
    assert refused.message == rent_run.LINK_CHOOSE


def test_a_second_invoice_is_refused_and_a_differing_total_needs_confirmation_and_note() -> None:
    charge = a_charge()
    differs = unlinked_invoice(lease_of(charge).operator_id, total="900.00")
    no_confirm = service.link_invoice(charge.pk, differs.pk, confirm=False, note="", actor=None)
    assert (no_confirm.ok, no_confirm.message) == (False, rent_run.LINK_CONFIRM)
    no_note = service.link_invoice(charge.pk, differs.pk, confirm=True, note=" ", actor=None)
    assert (no_note.ok, no_note.message) == (False, rent_run.LINK_NOTE_REQUIRED)
    done = service.link_invoice(
        charge.pk, differs.pk, confirm=True, note="Discount agreed.", actor=None
    )
    assert done.message == rent_run.LINK_DONE_DIFFERS.format(number="INV-2026-0101")
    charge = fresh(charge)
    assert charge.needs_review
    assert rent_run.LINKED_TOTAL_DIFFERS in service.review_message(charge)
    second = unlinked_invoice(lease_of(charge).operator_id, number="INV-2026-0104")
    refused = service.link_invoice(charge.pk, second.pk, confirm=True, note="x", actor=None)
    assert (refused.ok, refused.message) == (False, rent_run.LINK_CHARGE_HAS_INVOICE)


def test_an_invoice_event_never_auto_links_an_invoice_of_a_setup_charge(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    # A rent charge of the same operator, amount and month the invoice would fit.
    RentCharge.objects.create(
        lease=lease_of(charge),
        period_month=date(2026, 10, 1),
        due_on=DAY,
        amount_due=D("1000.00"),
        status=RentChargeStatus.DUE,
    )
    deliver("update_invoice", id=INVOICE_REF)
    invoice = Invoice.objects.get(one_off_charge=charge)
    assert invoice.rent_charge_id is None


# --- The provider invoice is marked through the worker (D6) ----------------------------------


def test_a_full_payment_queues_the_mark_and_the_worker_marks_the_invoice_paid(
    fake: FakeProvider, worker: None
) -> None:
    charge = invoiced(fake)
    outcome = pay(charge, "1000.00")
    assert (outcome.ok, outcome.mark_due, outcome.provider) == (True, True, None)
    assert outcome.payment is not None
    assert fake.calls == []  # the service never calls the provider
    job = jobs.enqueue(JobKind.ONE_OFF_PAYMENT_MARK, charge, {"payment": outcome.payment.pk}, None)
    assert (job.entity_type, job.entity_id) == ("one_off_charge", charge.pk)
    [done] = run_queued_jobs()
    assert (done.status, done.result) == (JobStatus.DONE, rent_run.MARK_DONE)
    assert [name for name, _ in fake.calls] == [
        "get_invoice",
        "mark_invoice_paid_cash_or_check",
        "get_invoice",
    ]
    invoice = Invoice.objects.get(one_off_charge=charge)
    assert invoice.provider_status == "PAID"
    assert "one_off_charge.provider_marked" in actions(charge.pk)
    marked = AuditLog.objects.get(action="one_off_charge.provider_marked")
    assert marked.after is not None and INVOICE_REF not in str(marked.after)


def test_a_failed_mark_keeps_the_payment_and_flags_the_charge(
    fake: FakeProvider, worker: None
) -> None:
    charge = invoiced(fake)
    outcome = pay(charge, "1000.00")
    assert outcome.payment is not None
    fake.fail_next("mark_invoice_paid_cash_or_check", ProviderUnavailable(503))
    jobs.enqueue(JobKind.ONE_OFF_PAYMENT_MARK, charge, {"payment": outcome.payment.pk}, None)
    [done] = run_queued_jobs()
    assert done.status == JobStatus.FAILED
    charge = fresh(charge)
    assert (charge.status, charge.needs_review) == (OneOffChargeStatus.PAID, True)
    assert "Not marked paid in Stax: Unavailable" in service.review_message(charge)


def later(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    moment = timezone.now() + timedelta(seconds=seconds)
    monkeypatch.setattr("django.utils.timezone.now", lambda: moment)


def failed_mark(fake: FakeProvider) -> OneOffCharge:
    """An invoiced charge paid in full whose marking failed in the worker."""
    charge = invoiced(fake)
    outcome = pay(charge, "1000.00")
    assert outcome.payment is not None
    fake.fail_next("mark_invoice_paid_cash_or_check", ProviderUnavailable(503))
    jobs.enqueue(JobKind.ONE_OFF_PAYMENT_MARK, charge, {"payment": outcome.payment.pk}, None)
    [done] = run_queued_jobs()
    assert done.status == JobStatus.FAILED
    fake.calls.clear()
    return fresh(charge)


def test_a_failed_mark_is_retried_after_a_minute_and_clears_the_flag(
    fake: FakeProvider, worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    charge = failed_mark(fake)
    assert service.retry_wait(charge) == 60
    too_soon = service.retry_provider_mark(charge.pk, actor=None)
    assert (too_soon.action, too_soon.retry_in) == (rent_run.MarkAction.RETRY_TOO_SOON, 60)
    assert fake.calls == []
    later(monkeypatch, 59)
    assert service.retry_provider_mark(charge.pk, actor=None).retry_in == 1
    later(monkeypatch, 60)
    assert service.retry_wait(fresh(charge)) == 0
    jobs.enqueue(JobKind.ONE_OFF_PAYMENT_RETRY_MARK, charge, None, None)
    [done] = run_queued_jobs()
    assert (done.status, done.result) == (JobStatus.DONE, rent_run.MARK_DONE)
    assert [name for name, _ in fake.calls] == [
        "get_invoice",
        "mark_invoice_paid_cash_or_check",
        "get_invoice",
    ]
    charge = fresh(charge)
    assert (charge.needs_review, charge.review_message) == (False, "")
    assert Invoice.objects.get(one_off_charge=charge).provider_status == "PAID"
    assert service.retry_wait(charge) is None
    nothing = service.retry_provider_mark(charge.pk, actor=None)
    assert nothing.action is rent_run.MarkAction.NOTHING_TO_RETRY
    # The rent charge's retry finds nothing on a setup charge's audit trail.
    assert rent_run.charge_retry_wait(rent_run.RENT, charge) is None


def test_nothing_to_retry_without_a_failed_mark() -> None:
    charge = a_charge()
    assert service.retry_wait(charge) is None
    result = service.retry_provider_mark(charge.pk, actor=None)
    assert result.action is rent_run.MarkAction.NOTHING_TO_RETRY


def test_the_retry_kind_acts_on_setup_charges_only() -> None:
    charge = a_charge()
    with pytest.raises(ValueError, match="does not act on this entity"):
        jobs.enqueue(JobKind.ONE_OFF_PAYMENT_RETRY_MARK, lease_of(charge), None, None)
    with pytest.raises(ValueError, match="does not act on this entity"):
        jobs.enqueue(JobKind.PAYMENT_RETRY_MARK, charge, None, None)
    assert jobs.ENTITY_OF_KIND[JobKind.ONE_OFF_PAYMENT_RETRY_MARK] == "one_off_charge"


def test_the_page_shows_the_wait_then_the_button_and_queues_one_retry(
    fake: FakeProvider, worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    charge = failed_mark(fake)
    client, _ = client_for(Role.BILLING)
    paths = routes(charge)
    page = client.get(paths["ledger:one_off_charges:detail"]).content.decode()
    assert 'id="retry-mark"' not in page
    assert '<p id="retry-wait" class="muted">Retry available in 60 seconds.</p>' in page
    later(monkeypatch, 61)
    page = client.get(paths["ledger:one_off_charges:detail"]).content.decode()
    assert 'id="retry-mark"' in page
    response = client.post(paths["ledger:one_off_charges:retry_mark"])
    assert response["Location"] == paths["ledger:one_off_charges:detail"]
    retry = Job.objects.filter(kind=JobKind.ONE_OFF_PAYMENT_RETRY_MARK)
    assert [(job.entity_id, job.status) for job in retry] == [(charge.pk, JobStatus.QUEUED)]
    # While that job waits, the button is gone and a second click queues nothing.
    page = client.get(paths["ledger:one_off_charges:detail"]).content.decode()
    assert 'id="retry-mark"' not in page
    client.post(paths["ledger:one_off_charges:retry_mark"])
    assert retry.count() == 1
    assert fake.calls == []  # the page never calls the provider
    viewer, _ = client_for(Role.VIEWER)
    assert (
        'id="retry-mark"' not in viewer.get(paths["ledger:one_off_charges:detail"]).content.decode()
    )
    assert viewer.post(paths["ledger:one_off_charges:retry_mark"]).status_code == 403
    assert retry.count() == 1


def test_a_partial_payment_on_an_invoiced_charge_makes_no_call_and_flags_it(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake)
    outcome = pay(charge, "400.00")
    assert outcome.provider is not None and not outcome.mark_due
    assert outcome.provider.action is rent_run.MarkAction.NOT_UPDATED
    assert fake.calls == []
    charge = fresh(charge)
    assert charge.review_message == offline_marking.PARTIAL_AMOUNT
    assert "Not marked paid in Stax: part of the amount" in service.review_message(charge)
    assert "one_off_charge.provider_not_updated" in actions(charge.pk)


def test_a_payment_without_an_invoice_queues_nothing() -> None:
    outcome = pay(a_charge(), "1000.00")
    assert outcome.provider is not None
    assert (outcome.provider.action, outcome.mark_due) == (rent_run.MarkAction.NO_INVOICE, False)


def test_the_setup_mark_kind_acts_on_setup_charges_only() -> None:
    charge = a_charge()
    with pytest.raises(ValueError, match="does not act on this entity"):
        jobs.enqueue(
            JobKind.ONE_OFF_PAYMENT_MARK, lease_of(charge), {"payment": uuid.uuid4()}, None
        )
    with pytest.raises(ValueError, match="does not act on this entity"):
        jobs.enqueue(JobKind.PAYMENT_MARK, charge, {"payment": uuid.uuid4()}, None)
    assert jobs.ENTITY_OF_KIND[JobKind.ONE_OFF_PAYMENT_MARK] == "one_off_charge"


# --- Payment provider transactions for a setup invoice (consumers) -----------------------------


def test_a_transaction_for_a_setup_invoice_lands_on_the_setup_charge(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    provider_txn(fake)
    assert deliver("create_transaction", id=TXN) == "processed"
    payment = Payment.objects.get(one_off_charge=charge)
    assert (payment.kind, payment.method, payment.status, payment.rent_charge_id) == (
        PaymentKind.ONE_OFF,
        PaymentMethod.ACH,
        PaymentStatus.PENDING,
        None,
    )
    assert fresh(charge).status == OneOffChargeStatus.OWED  # pending pays nothing
    fake.transactions[TXN] = replace(fake.transactions[TXN], settled_at=SETTLED_AT)
    deliver("update_transaction_settled", id=TXN)
    charge = fresh(charge)
    assert (charge.status, charge.amount_paid) == (OneOffChargeStatus.PAID, D("1000.00"))
    assert "one_off_charge.payment_applied" in actions(charge.pk)


def test_an_ach_return_reopens_the_setup_charge_and_enters_the_returns_queue(
    fake: FakeProvider,
) -> None:
    charge = invoiced(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    deliver("update_transaction_settled", id=TXN)
    assert fresh(charge).status == OneOffChargeStatus.PAID
    fake.transactions[TXN] = replace(
        fake.transactions[TXN], returned=True, return_code="R01", return_reason="NSF"
    )
    deliver("update_transaction", id=TXN)
    charge = fresh(charge)
    assert (charge.status, charge.amount_paid, charge.needs_review) == (
        OneOffChargeStatus.OWED,
        D("0.00"),
        True,
    )
    payment = Payment.objects.get(one_off_charge=charge)
    [queued] = list(returns.open_returns())
    assert queued == payment
    row = returns.row(payment)
    assert (row.store, row.operator) == (lease_of(charge).store, lease_of(charge).operator)
    assert service.RETURNED in service.review_message(charge)
    assert "one_off_charge.ach_returned" in actions(charge.pk)
    page = client_for(Role.VIEWER)[0].get("/returns/").content.decode()
    assert f"/one-off-charges/{charge.pk}/" in page


def test_a_dispute_flags_the_setup_charge(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    deliver("update_transaction_settled", id=TXN)
    assert deliver("create_dispute", id=TXN) == "processed"
    charge = fresh(charge)
    assert charge.needs_review
    assert service.DISPUTED in service.review_message(charge)


def test_a_differing_provider_total_flags_the_setup_charge(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    provider_invoice(fake, total="950.00")
    deliver("update_invoice", id=INVOICE_REF)
    charge = fresh(charge)
    assert charge.needs_review and charge.price == D("1000.00")
    assert "one_off_charge.total_mismatch" in actions(charge.pk)


def test_an_ach_setup_payment_is_refunded_by_the_rent_rules(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    deliver("update_transaction_settled", id=TXN)
    payment = Payment.objects.get(one_off_charge=charge)
    facts = refunds.payment_facts(payment)
    assert facts.can_refund and facts.refundable == D("1000.00")
    outcome = refunds.refund_payment(payment.pk, amount=D("200.00"), confirmed=True, actor=None)
    assert outcome.ok, outcome.message
    refund = Payment.objects.get(one_off_charge=charge, is_refund=True)
    assert (refund.kind, refund.parent_payment_id, refund.amount) == (
        PaymentKind.ONE_OFF,
        payment.pk,
        D("200.00"),
    )
    if refund.status == PaymentStatus.SETTLED:
        assert fresh(charge).status == OneOffChargeStatus.PARTIAL
    entry = AuditLog.objects.get(action=refunds.REFUNDED_ACTION)
    assert entry.after is not None and entry.after["charge"]["fee_type"] == "sign_fee"


def test_a_deposit_refund_is_never_refundable_or_voidable() -> None:
    charge = a_charge("2500.00", deposit_type())
    pay(charge, "2500.00")
    paid_back = Payment.objects.create(
        one_off_charge=charge,
        kind=PaymentKind.REFUND_DEPOSIT,
        method=PaymentMethod.WIRE,
        amount=D("2500.00"),
        status=PaymentStatus.SETTLED,
        settled_on=DAY,
        note="Deposit returned at lease end.",
    )
    facts = refunds.payment_facts(paid_back)
    assert (facts.refund_refusal, facts.void_refusal) == (
        refunds.IS_DEPOSIT_REFUND,
        refunds.IS_DEPOSIT_REFUND,
    )
    refused = refunds.refund_payment(paid_back.pk, amount=D("1.00"), confirmed=True, actor=None)
    assert (refused.ok, refused.message) == (False, refunds.IS_DEPOSIT_REFUND)
    # Paying the deposit back does not reopen the charge.
    assert fresh(charge).status == OneOffChargeStatus.PAID
    service.recompute(fresh(charge))
    assert fresh(charge).amount_paid == D("2500.00")


# --- The payments ledger: kinds, charge and deposits (D4, D7) ------------------------------


def test_the_ledger_totals_leave_deposits_out_and_show_them_held() -> None:
    sign = a_charge("1000.00")
    deposit = a_charge("2500.00", deposit_type())
    pay(sign, "1000.00")
    pay(deposit, "2500.00")
    Payment.objects.create(
        one_off_charge=deposit,
        kind=PaymentKind.REFUND_DEPOSIT,
        method=PaymentMethod.ZELLE,
        amount=D("2000.00"),
        status=PaymentStatus.SETTLED,
        settled_on=DAY,
        note="Part of the deposit returned.",
    )
    totals = payments_ledger.totals(
        payments_ledger.ledger_payments(payments_ledger.PaymentFilter())
    )
    assert (totals.settled, totals.refunded, totals.deposits_held, totals.count) == (
        D("1000.00"),
        D("0.00"),
        D("500.00"),
        3,
    )


def test_the_ledger_filters_and_shows_a_setup_payment_by_its_lease() -> None:
    charge = a_charge()
    pay(charge, "1000.00")
    selected = payments_ledger.PaymentFilter(
        operator=lease_of(charge).operator_id, store=lease_of(charge).store_id
    )
    [row] = payments_ledger.rows_for(payments_ledger.ledger_payments(selected))
    assert (row.operator, row.store) == (lease_of(charge).operator, lease_of(charge).store)
    [record] = [
        payments_ledger.csv_record(item)
        for item in payments_ledger.rows_for(payments_ledger.ledger_payments(selected))
    ]
    assert record[payments_ledger.CSV_HEADER.index("Charge kind")] == "one_off"


def test_the_ledger_and_payment_pages_name_the_kind_and_the_setup_charge() -> None:
    charge = a_charge()
    pay(charge, "1000.00")
    payment = Payment.objects.get()
    client, _ = client_for(Role.VIEWER)
    listing = client.get("/payments/").content.decode()
    assert f"/one-off-charges/{charge.pk}/" in listing
    assert "Setup fee: Sign fee" in listing
    assert 'id="total-deposits-held"' in listing
    detail = client.get(f"/payments/{payment.pk}/").content.decode()
    assert 'id="payment-setup-charge"' in detail and "Sign fee" in detail


# --- Screens and roles (D5, D6, D12) ----------------------------------------------------------

WRITE_ROUTES = {
    "ledger:one_off_charges:payment": Role.BILLING,
    "ledger:one_off_charges:retry_mark": Role.BILLING,
    "ledger:one_off_charges:link": Role.BILLING,
    "ledger:one_off_charges:edit": Role.BILLING,
    "ledger:one_off_charges:waive": Role.ADMIN,
    # PLAN-010d D2, D6, D9: creating a one-off charge (PLAN-012 D6: replaced by New
    # invoice, it answers 410 to billing), cancelling it and the provider retry.
    "ledger:operators:new_charge": Role.BILLING,
    "ledger:leases:new_charge": Role.BILLING,
    "ledger:one_off_charges:cancel": Role.BILLING,
    "ledger:one_off_charges:retry": Role.BILLING,
    # PLAN-010d D4, D9: Charge now, admin.
    "ledger:one_off_charges:charge_now": Role.ADMIN,
    # PLAN-011 D0, D10: deleting a waived charge's live provider invoice, admin.
    "ledger:one_off_charges:delete_invoice": Role.ADMIN,
    # PLAN-011b D4, PLAN-014 D6: "Send a message now", billing.
    "ledger:one_off_charges:send_message": Role.BILLING,
    # PLAN-012e D10: Split into installments, billing.
    "ledger:one_off_charges:split": Role.BILLING,
    # PLAN-014 F3: Stop automatic retry, billing.
    "ledger:one_off_charges:stop_retry": Role.BILLING,
}

#: PLAN-012e-2 D4: Add setup charge moved into the invoice builder; its route is a read-only
#: redirect there (GET only), refused below billing.
REDIRECT_ROUTES = {"ledger:leases:add_setup_charge": Role.BILLING}

POST_ONLY_ROUTES = {
    "ledger:one_off_charges:retry_mark",
    "ledger:one_off_charges:retry",
    "ledger:one_off_charges:delete_invoice",
    "ledger:one_off_charges:send_message",
    "ledger:one_off_charges:stop_retry",
}


def routes(charge: OneOffCharge) -> dict[str, str]:
    base = f"/one-off-charges/{charge.pk}/"
    return {
        "ledger:one_off_charges:detail": base,
        "ledger:one_off_charges:payment": f"{base}payments/new/",
        "ledger:one_off_charges:retry_mark": f"{base}retry-mark/",
        "ledger:one_off_charges:link": f"{base}link-invoice/",
        "ledger:one_off_charges:edit": f"{base}edit/",
        "ledger:one_off_charges:waive": f"{base}waive/",
        "ledger:leases:add_setup_charge": f"/leases/{charge.lease_id}/setup-charges/new/",
        "ledger:operators:new_charge": f"/operators/{lease_of(charge).operator_id}/charges/new/",
        "ledger:leases:new_charge": f"/leases/{charge.lease_id}/charges/new/",
        "ledger:one_off_charges:cancel": f"{base}cancel/",
        "ledger:one_off_charges:retry": f"{base}retry/",
        "ledger:one_off_charges:charge_now": f"{base}charge-now/",
        "ledger:one_off_charges:delete_invoice": f"{base}delete-invoice/",
        "ledger:one_off_charges:send_message": f"{base}send-message/",
        "ledger:one_off_charges:split": f"{base}split/",
        "ledger:one_off_charges:stop_retry": f"{base}stop-retry/",
    }


def test_every_setup_route_is_classified_with_its_role() -> None:
    ledger = [name for name, _, _ in iter_routes() if name and name.startswith("ledger:")]
    names = {
        name
        for name in ledger
        if "setup" in name
        or name.startswith("ledger:one_off_charges:")
        or name.endswith(":new_charge")
    }
    read_only = {"ledger:one_off_charges:detail", "ledger:one_off_charges:export"}
    assert names == {*read_only, *WRITE_ROUTES, *REDIRECT_ROUTES}
    assert read_only | set(REDIRECT_ROUTES) <= registry.READ_ONLY_ROUTE_NAMES
    for name in REDIRECT_ROUTES:
        assert registry.rule_for(name) is None, name
    for name, role in WRITE_ROUTES.items():
        rule = registry.rule_for(name)
        assert rule is not None and rule.min_role is role and rule.methods == {"POST"}, name
    for prefix in ("admin:ledger_feetype_", "admin:ledger_insurancepremium_"):
        rule = registry.rule_for(f"{prefix}change")
        assert rule is not None and rule.min_role is Role.ADMIN


def test_a_viewer_reads_the_page_and_every_write_route_refuses_it() -> None:
    charge = a_charge()
    client, _ = client_for(Role.VIEWER)
    paths = routes(charge)
    page = client.get(paths["ledger:one_off_charges:detail"]).content.decode()
    assert (
        "Sign fee" in page and 'id="record-payment"' not in page and 'id="waive-charge"' not in page
    )
    for name in [*WRITE_ROUTES, *REDIRECT_ROUTES]:
        # The retry is a button only: it takes no GET (405), whatever the role.
        expected = 405 if name in POST_ONLY_ROUTES else 403
        assert client.get(paths[name]).status_code == expected, name
        assert client.post(paths[name], {"note": "x", "price": "1.00"}).status_code == 403, name
    assert not Payment.objects.exists() and not Job.objects.exists()
    assert fresh(charge).status == OneOffChargeStatus.OWED


def test_billing_cannot_waive() -> None:
    charge = a_charge()
    client, _ = client_for(Role.BILLING)
    paths = routes(charge)
    detail = client.get(paths["ledger:one_off_charges:detail"]).content.decode()
    assert 'id="waive-charge"' not in detail
    assert client.get(paths["ledger:one_off_charges:waive"]).status_code == 403
    assert client.post(paths["ledger:one_off_charges:waive"], {"note": "x"}).status_code == 403
    assert fresh(charge).status == OneOffChargeStatus.OWED


def test_an_admin_waives_through_the_page_and_an_empty_note_is_held_as_an_error() -> None:
    charge = a_charge()
    client, _ = client_for(Role.ADMIN)
    paths = routes(charge)
    detail = client.get(paths["ledger:one_off_charges:detail"]).content.decode()
    assert 'id="waive-charge"' in detail
    refused = client.post(paths["ledger:one_off_charges:waive"], {"note": "​"})
    assert refused["Location"] == paths["ledger:one_off_charges:waive"]
    waive_page = client.get(paths["ledger:one_off_charges:waive"]).content.decode()
    assert "A note is required" in waive_page
    done = client.post(paths["ledger:one_off_charges:waive"], {"note": "Owner decision."})
    assert done["Location"] == paths["ledger:one_off_charges:detail"]
    assert fresh(charge).status == OneOffChargeStatus.WAIVED


@pytest.mark.usefixtures("pinned_today")
def test_billing_records_a_wire_through_the_page_and_a_resubmission_records_nothing() -> None:
    charge = a_charge()
    client, _ = client_for(Role.BILLING)
    paths = routes(charge)
    data = {
        "creation_key": str(uuid.uuid4()),
        "method": "wire",
        "amount": "1000.00",
        "paid_on": "2026-10-15",
        "reference": "W-1",
        "note": NOTE,
    }
    response = client.post(paths["ledger:one_off_charges:payment"], data)
    assert response["Location"] == paths["ledger:one_off_charges:detail"]
    client.post(paths["ledger:one_off_charges:payment"], data)
    assert Payment.objects.count() == 1
    page = client.get(paths["ledger:one_off_charges:detail"]).content.decode()
    assert has_token(page, "Paid") and "W-1" in page
    assert not Job.objects.exists()  # no invoice, nothing to mark


@pytest.mark.usefixtures("pinned_today")
def test_the_page_queues_the_setup_mark_job_for_an_invoiced_charge(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    client, _ = client_for(Role.BILLING)
    key = str(uuid.uuid4())
    client.post(
        routes(charge)["ledger:one_off_charges:payment"],
        {
            "creation_key": key,
            "method": "zelle",
            "amount": "1000.00",
            "paid_on": "2026-10-15",
            "reference": "",
            "note": NOTE,
        },
    )
    [job] = Job.objects.all()
    assert (job.kind, job.entity_id, str(job.replay_key)) == (
        JobKind.ONE_OFF_PAYMENT_MARK,
        charge.pk,
        key,
    )
    assert fake.calls == []
    page = client.get(routes(charge)["ledger:one_off_charges:detail"]).content.decode()
    assert 'id="job-status"' in page


def test_billing_links_edits_and_adds_through_the_pages() -> None:
    charge = a_charge()
    client, _ = client_for(Role.BILLING)
    paths = routes(charge)
    invoice = unlinked_invoice(lease_of(charge).operator_id)
    assert "INV-2026-0101" in client.get(paths["ledger:one_off_charges:link"]).content.decode()
    client.post(paths["ledger:one_off_charges:link"], {"invoice": str(invoice.pk), "note": ""})
    assert Invoice.objects.get(pk=invoice.pk).one_off_charge_id == charge.pk
    client.post(
        paths["ledger:one_off_charges:edit"],
        {"cost": "", "price": "1100.00", "vendor": "V", "due_date": "2026-11-01"},
    )
    assert (fresh(charge).price, fresh(charge).cost) == (D("1100.00"), None)
    # PLAN-012e-2 D4: Add setup charge opens the invoice builder with the item on line 1;
    # sent by e-mail, the line is the setup charge.
    access = fee("access_fee")
    ExternalRef.objects.create(
        entity_type="lease",
        entity_id=lease_of(charge).pk,
        provider="stax_pay",
        external_kind="customer",
        external_id="cus-setup-builder",
    )
    opened = client.get(paths["ledger:leases:add_setup_charge"] + "?item=access_fee")
    builder = opened["Location"]
    assert "collect=draft" in builder and "item=access_fee" in builder
    assert 'value="Access fee"' in client.get(builder).content.decode()
    # PLAN-012f-b D15: Save, then Send e-mail from its prompt (the Drafts table's send).
    lease = lease_of(charge)
    data = builder_form(lease, "Access fee", "250.00", collect="draft")
    added = client.post(builder, data)
    draft = InvoiceDraft.objects.get()
    assert added["Location"] == f"/invoices/drafts/{draft.pk}/saved/"
    sent = client.post(f"/invoices/drafts/{draft.pk}/send/?store={lease.store_id}")
    new = OneOffCharge.objects.get(lease=lease, fee_type=access)
    assert (new.price, new.collection) == (D("250.00"), "email")
    assert sent["Location"] == f"/stores/{lease.store_id}/?tab=invoices#tab-invoices"
    client.post(builder, {**data, "creation_key": str(uuid.uuid4())})
    second = InvoiceDraft.objects.exclude(pk=draft.pk).get()
    duplicate = client.post(f"/invoices/drafts/{second.pk}/send/?store={lease.store_id}")
    assert duplicate["Location"].startswith("/invoices/drafts/")
    page = client.get(duplicate["Location"]).content.decode()
    assert "already has a charge for Access fee" in page
    assert OneOffCharge.objects.filter(lease=lease, fee_type=access).count() == 1
    # PLAN-012d D2 to D4: the charges are on the store page; PLAN-012e-2 D5: a setup
    # charge is made from Create invoice (the bar's Add setup charge is gone).
    store_page = client.get(f"/stores/{lease.store_id}/?tab=charges&filter=all").content.decode()
    assert 'id="store-charges"' in store_page and 'id="create-invoice"' in store_page
    assert "bar-add-setup-charge" not in store_page and "/setup-charges/new/" not in store_page
    assert f"/one-off-charges/{new.pk}/" in store_page
    lease_page = client.get(f"/leases/{charge.lease_id}/").content.decode()
    assert 'id="setup-charges"' not in lease_page and 'id="store-charges-link"' in lease_page


def test_a_card_number_is_refused_on_the_builder_the_add_form_opens_and_never_held() -> None:
    # PLAN-012e-2 D4: the add form is the invoice builder now; its line detail is the
    # free text the vendor was.
    charge = a_charge()
    client, _ = client_for(Role.BILLING)
    path = client.get(routes(charge)["ledger:leases:add_setup_charge"])["Location"]
    data = builder_form(lease_of(charge), "Access fee", "250.00", description=f"V {CARD}")
    client.post(path, data)
    page = client.get(path).content.decode()
    assert "may not contain card or account numbers" in page
    # The whole number, not a fragment: a random identifier on the page may hold "4111".
    assert CARD not in page and CARD.replace(" ", "") not in page
    assert OneOffCharge.objects.count() == 1


def builder_form(
    lease: Lease, item: str, amount: str, *, description: str = "", **fields: str
) -> dict[str, str]:
    """The invoice builder's New invoice form with one line (PLAN-012e-2 D3)."""
    return {
        "creation_key": str(uuid.uuid4()),
        "lease": str(lease.pk),
        "memo": "",
        "due_date": "",
        "lines-TOTAL_FORMS": "1",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "50",
        "lines-0-item": item,
        "lines-0-description": description,
        "lines-0-unit_amount": amount,
        **fields,
    }


# --- The lease checklist screen (D5) ----------------------------------------------------------


def lease_form(**overrides: Any) -> dict[str, Any]:
    from tests.test_lease_views import form_data

    return form_data(**overrides)


def test_the_new_lease_page_lists_one_row_per_checklist_item_with_the_price_as_a_hint() -> None:
    fee("sign_fee", default_price=D("1000.00"))
    fee("first_year_insurance", active=False)
    client, _ = client_for(Role.ADMIN)
    page = client.get("/leases/new/").content.decode()
    assert 'id="setup-checklist"' in page and 'id="setup-sign_fee"' in page
    assert 'id="setup-first_year_insurance"' not in page
    # PLAN-012d D9: the catalogue's price is a placeholder, never a value; no tick box.
    assert 'placeholder="1000.00"' in page and 'value="1000.00"' not in page
    assert "setup_sign_fee_tick" not in page
    assert 'id="setup-total"' in page and "Setup charges: <span" in page
    assert 'data-total-for="setup-checklist">$0.00</span>' in page
    assert "ledger/table_total.js" in page


def test_the_checklist_hides_an_item_without_the_flag() -> None:
    fee("sign_fee", default_price=D("1000.00"))
    fee("phone_fee", default_price=D("50.00"), show_in_setup=False)
    client, _ = client_for(Role.ADMIN)
    page = client.get("/leases/new/").content.decode()
    assert 'id="setup-sign_fee"' in page and 'id="setup-phone_fee"' not in page
    codes = {row.code for row in service.checklist_types()}
    assert "sign_fee" in codes and "phone_fee" not in codes
    # The invoice builder still offers it (the flag is the checklist's only).
    assert "phone_fee" in {row.code for row in service.offered_types()}
    # A posted price for the hidden item charges nothing.
    client.post("/leases/new/", lease_form(setup_phone_fee_price="50.00"))
    assert Lease.objects.count() == 1 and not OneOffCharge.objects.exists()


def test_the_checklist_post_charges_the_rows_with_a_price_once() -> None:
    sign, access = fee("sign_fee", default_price=D("450.00")), fee("access_fee")
    client, _ = client_for(Role.ADMIN)
    data = lease_form(
        **{
            "setup_sign_fee_price": "1000.00",
            "setup_sign_fee_cost": "500.00",
            "setup_sign_fee_vendor": "Bright Signs",
            # A cost and a vendor without a price: not charged.
            "setup_access_fee_cost": "20.00",
            "setup_access_fee_vendor": "Keys Inc",
            "setup_access_fee_price": "",
        }
    )
    response = client.post("/leases/new/", data)
    lease = Lease.objects.get()
    assert response["Location"] == f"/leases/{lease.pk}/"
    [charge] = OneOffCharge.objects.all()
    # Charged at the typed value, not the catalogue's default.
    assert (charge.fee_type, charge.price, charge.cost, charge.due_date) == (
        sign,
        D("1000.00"),
        D("500.00"),
        date(2026, 1, 1),  # the lease's start date
    )
    client.post("/leases/new/", data)
    assert (Lease.objects.count(), OneOffCharge.objects.count()) == (1, 1)
    assert not OneOffCharge.objects.filter(fee_type=access).exists()


def test_an_untouched_checklist_charges_nothing_and_never_the_placeholder() -> None:
    fee("sign_fee", default_price=D("450.00"))
    fee("access_fee", default_price=D("300.00"))
    client, _ = client_for(Role.ADMIN)
    response = client.post("/leases/new/", lease_form())
    assert response.status_code == 302
    assert Lease.objects.count() == 1 and not OneOffCharge.objects.exists()


def test_a_row_with_a_card_number_saves_nothing_and_the_total_line_is_rendered() -> None:
    fee("sign_fee")
    fee("access_fee")
    client, _ = client_for(Role.ADMIN)
    data = lease_form(
        setup_sign_fee_price="10.00", setup_access_fee_price="1234.50", setup_sign_fee_vendor=CARD
    )
    client.post("/leases/new/", data)
    page = client.get("/leases/new/").content.decode()
    assert "may not contain card or account numbers" in page
    # The whole number, not a fragment: a random identifier on the page may hold "4111".
    assert CARD not in page and CARD.replace(" ", "") not in page
    assert (Lease.objects.count(), OneOffCharge.objects.count()) == (0, 0)
    # The held form shows the total of the prices typed, rendered by the server.
    assert 'data-total-for="setup-checklist">$1,244.50</span>' in page


def test_the_lease_edit_page_has_no_checklist() -> None:
    lease = a_lease()
    client, _ = client_for(Role.ADMIN)
    assert 'id="setup-checklist"' not in client.get(f"/leases/{lease.pk}/edit/").content.decode()


# --- Django admin (D1, D9, D12) -------------------------------------------------------------


def test_an_admin_edits_a_fee_type_and_a_premium_cost_in_the_admin_and_it_is_audited() -> None:
    from tests.auth_helpers import make_user, verified_client

    sign = fee("sign_fee")
    premium = InsurancePremium.objects.create(policy_year=2031, amount=D("695.00"))
    user, device = make_user("admin-catalogue", Role.ADMIN, superuser=True)
    assert device is not None
    client = verified_client(user, device)
    response = client.post(
        f"/admin/ledger/feetype/{sign.pk}/change/",
        {
            "code": "sign_fee",
            "name": "Front sign",
            "default_cost": "",
            "default_price": "1000.00",
            "active": "on",
            "sort_order": "30",
        },
    )
    assert response.status_code == 302
    assert FeeType.objects.get(pk=sign.pk).name == "Front sign"
    client.post(
        f"/admin/ledger/insurancepremium/{premium.pk}/change/",
        {"policy_year": "2031", "amount": "695.00", "cost": "300.00"},
    )
    assert InsurancePremium.objects.get(pk=premium.pk).cost == D("300.00")
    entries = AuditLog.objects.filter(actor=user).order_by("id")
    assert [entry.action for entry in entries] == [
        "fee_type.changed_in_admin",
        "insurance_premium.changed_in_admin",
    ]
    assert entries[0].after is not None and entries[0].after["name"] == "Front sign"
    delete = client.post(f"/admin/ledger/feetype/{sign.pk}/delete/", {"post": "yes"})
    assert delete.status_code == 403
    assert FeeType.objects.filter(pk=sign.pk).exists()


def test_billing_cannot_post_to_the_fee_catalogue_in_the_admin() -> None:
    sign = fee("sign_fee")
    client, _ = client_for(Role.BILLING)
    response = client.post(f"/admin/ledger/feetype/{sign.pk}/change/", {"name": "x"})
    assert response.status_code == 403
    assert FeeType.objects.get(pk=sign.pk).name == "Sign fee"
