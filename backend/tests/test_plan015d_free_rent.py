"""PLAN-015d task 2: free rent by days (ledger.services.free_rent), the concession on the charges
the rent run makes, the discount line of the invoice builder and, since PLAN-015g §3A, the Rent
line sent to the processor at its net with the concession named in its details (docs/qa/15/PLAN-015d
§3 (2))."""

from __future__ import annotations

import json
import random
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any

import pytest

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    FreeRentFrom,
    InvoiceLineItem,
    InvoiceLineKind,
    Lease,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import free_rent, invoice_builder, leases, proration, rent_run
from ledger.services.pdf import invoice as invoice_pdf
from payments import one_off_invoices
from payments.errors import ValidationFailed
from payments.fake import FakeProvider
from payments.provider import InvoiceInput, InvoiceUpdate
from payments.stax import mapping, validation
from payments.stax.adapter import StaxProvider
from tests import test_invoice_builder as builder_tests
from tests.job_helpers import run_queued_jobs
from tests.stax_helpers import always, make_rig
from tests.test_invoice_builder import (
    NOVEMBER,
    TODAY,
    a_draft,
    a_rent_lease,
    created_invoices,
    fresh,
    line,
    rent_line,
    sent,
    user,
)
from tests.test_lease_services import make_lease, make_operator
from tests.test_one_off_charges import an_operator
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

# The builder file's fixtures, re-exported so pytest finds them here.
fixed_now = builder_tests.fixed_now
fake = builder_tests.fake
worker = builder_tests.worker

D = Decimal
DECEMBER = date(2026, 12, 1)
JANUARY = date(2027, 1, 1)


def with_free_rent(lease: Lease, days: int, start: str = FreeRentFrom.MOVE_IN) -> Lease:
    Lease.objects.filter(pk=lease.pk).update(free_rent_days=days, free_rent_from=start)
    return Lease.objects.get(pk=lease.pk)


def a_free_lease(days: int, start: str = FreeRentFrom.MOVE_IN, **fields: Any) -> Lease:
    values: dict[str, Any] = {
        "started_on": date(2026, 12, 10),
        "debit_day": 1,
        "payment_channel": "wire",
        "rent": D("3100.00"),
        "free_rent_days": days,
        "free_rent_from": start,
        "operator": make_operator(f"{uuid.uuid4().hex[:10]}@example.com"),
    }
    values.update(fields)
    return make_lease(**values)


# --- the period and its words -----------------------------------------------------------------


def test_from_the_move_in_date_the_start_is_the_first_free_day() -> None:
    lease = a_free_lease(30)
    assert free_rent.period(lease) == (date(2026, 12, 10), date(2027, 1, 8))
    assert free_rent.text(lease) == "Free rent per lease agreement (Dec 10 – Jan 8)"
    assert free_rent.card_text(lease) == "30 days, Dec 10 – Jan 8"


def test_from_the_first_of_the_following_month() -> None:
    lease = a_free_lease(30, FreeRentFrom.NEXT_MONTH)
    assert free_rent.period(lease) == (date(2027, 1, 1), date(2027, 1, 30))
    # A lease that starts on the 1st still counts from the 1st of the month after.
    first = a_free_lease(1, FreeRentFrom.NEXT_MONTH, started_on=date(2026, 12, 1))
    assert free_rent.period(first) == (JANUARY, JANUARY)
    assert free_rent.card_text(first) == "1 day, Jan 1"


def test_no_free_days_is_no_period_and_no_concession() -> None:
    lease = a_free_lease(0)
    assert free_rent.period(lease) is None and free_rent.text(lease) == ""
    assert free_rent.concession_for(lease, DECEMBER, D("2200.00")) == (D("0"), "")


# --- the concession of each month ---------------------------------------------------------


def test_a_prorated_first_month_inside_the_period_is_free_in_full_and_the_next_by_days() -> None:
    # Dec 10–31: 22 of 31 days of $3,100.00 = $2,200.00, all free; Jan 1–8: 8/31 of $3,100.00.
    lease = a_free_lease(30)
    december, words = free_rent.concession_for(lease, DECEMBER, D("2200.00"))
    january, _ = free_rent.concession_for(lease, JANUARY, D("3100.00"))
    assert (december, january) == (D("2200.00"), D("800.00"))
    assert words == "Free rent per lease agreement (Dec 10 – Jan 8)"
    assert free_rent.concession_for(lease, date(2027, 2, 1), D("3100.00"))[0] == D("0")


def test_a_free_period_longer_than_the_first_two_months() -> None:
    lease = a_free_lease(80)  # Dec 10 – Feb 27
    found = free_rent.month_concessions(lease)
    assert list(found) == [DECEMBER, JANUARY, date(2027, 2, 1)]
    assert free_rent.concession_for(lease, JANUARY, D("3100.00"))[0] == D("3100.00")
    assert found[date(2027, 2, 1)] == (D("3100.00") * 27 / 28).quantize(D("0.01"), ROUND_HALF_UP)


def test_366_days_reach_into_the_next_year_and_never_exceed_a_charge() -> None:
    lease = a_free_lease(366)
    assert free_rent.period(lease) == (date(2026, 12, 10), date(2027, 12, 10))
    for month, amount in free_rent.month_concessions(lease).items():
        due = rent_run.first_month(lease, month, D("3100.00")).amount
        assert D(0) <= free_rent.concession_for(lease, month, due)[0] <= due, month
        assert amount >= 0


def _exact_total(lease: Lease) -> Fraction:
    span = free_rent.period(lease)
    assert span is not None
    total = Fraction(0)
    month = span[0].replace(day=1)
    while month <= span[1]:
        first = max(month, lease.started_on or month)
        last = proration.last_day(month)
        billed = (last - first).days + 1
        free_first, free_last = max(first, span[0]), min(last, span[1])
        if free_first <= free_last:
            amount = Fraction(rent_run.first_month(lease, month, D("1000.00")).amount)
            total += amount * ((free_last - free_first).days + 1) / billed
        month = (last + timedelta(days=1)).replace(day=1)
    return total


def test_the_last_month_takes_the_cent_remainder() -> None:
    """Over many periods the months sum to the exact total rounded once, half up, and each
    month is within a cent of its own exact share."""
    rng = random.Random(20261108)  # noqa: S311 - test data, not security
    for _ in range(25):
        start = date(2026, 12, 1) + timedelta(days=rng.randrange(0, 60))
        days = rng.randrange(1, 120)
        lease = a_free_lease(days, started_on=start, rent=D("1000.00"))
        found = free_rent.month_concessions(lease)
        exact = _exact_total(lease)
        rounded = D(exact.numerator) / D(exact.denominator)
        assert sum(found.values(), D(0)) == rounded.quantize(D("0.01"), ROUND_HALF_UP), (
            start,
            days,
        )


def test_a_twice_monthly_lease_splits_the_concession_as_its_rent() -> None:
    lease = a_free_lease(
        45, FreeRentFrom.NEXT_MONTH, second_debit_day=15, started_on=date(2026, 12, 1)
    )
    # Jan 1–31 free in full: each debit's own amount; Feb 1–14: 14/28 of $3,100.00 = $1,550.00.
    first, _ = free_rent.concession_for(lease, date(2027, 2, 1), D("1550.00"), seq=1)
    second, _ = free_rent.concession_for(lease, date(2027, 2, 1), D("1550.00"), seq=2)
    assert (first, second) == (D("775.00"), D("775.00"))
    assert free_rent.concession_for(lease, JANUARY, D("1550.00"), seq=2)[0] == D("1550.00")


# --- the rent run's charges ---------------------------------------------------------------------


def test_create_month_sets_the_concession_and_the_catch_up_too() -> None:
    lease = a_free_lease(30)
    december = rent_run.catch_up_lease(lease, date(2027, 1, 5), None)
    assert [charge.period_month for charge in december] == [DECEMBER, JANUARY]
    dec, jan = (fresh_charge(charge) for charge in december)
    assert (dec.amount_due, dec.concession, dec.balance, dec.status) == (
        D("2200.00"),
        D("2200.00"),
        D("0.00"),
        RentChargeStatus.PAID,
    )
    assert (jan.amount_due, jan.concession, jan.balance, jan.status) == (
        D("3100.00"),
        D("800.00"),
        D("2300.00"),
        RentChargeStatus.DUE,
    )
    assert jan.concession_text == "Free rent per lease agreement (Dec 10 – Jan 8)"
    audit = AuditLog.objects.get(action="rent_charge.created", entity_id=str(jan.pk))
    assert audit.after["concession"] == "800.00"


def pay(charge: RentCharge, amount: Decimal, on: date) -> rent_run.PaymentOutcome:
    return rent_run.record_manual_payment(
        charge.pk,
        method="zelle",
        amount=amount,
        paid_on=on,
        note="Paid by Zelle.",
        actor=None,
        today=on,
    )


def fresh_charge(charge: RentCharge) -> RentCharge:
    return RentCharge.objects.get(pk=charge.pk)


def test_a_payment_of_the_net_pays_the_charge_and_never_more() -> None:
    lease = a_free_lease(30)
    [_, jan] = rent_run.catch_up_lease(lease, date(2027, 1, 5), None)
    over = pay(jan, D("2300.01"), date(2027, 1, 5))
    assert not over.ok
    done = pay(jan, D("2300.00"), date(2027, 1, 5))
    assert done.ok, done.message
    jan = fresh_charge(jan)
    assert (jan.status, jan.balance) == (RentChargeStatus.PAID, D("0.00"))
    assert not RentCharge.objects.filter(balance__lt=0).exists()


def test_a_charge_outside_the_period_has_no_concession() -> None:
    lease = a_free_lease(5, started_on=date(2026, 12, 1))
    charges = rent_run.catch_up_lease(lease, date(2027, 1, 5), None)
    jan = next(charge for charge in charges if charge.period_month == JANUARY)
    assert (fresh_charge(jan).concession, fresh_charge(jan).concession_text) == (D("0.00"), "")


# --- Edit lease: the concession of months not billed follows the period ---------------------


def test_changing_the_period_recomputes_unbilled_months_and_keeps_billed_ones() -> None:
    lease = a_free_lease(0, started_on=date(2026, 12, 1))
    charges = rent_run.catch_up_lease(lease, date(2027, 1, 5), None)
    dec, jan = (fresh_charge(charge) for charge in charges)
    paid = pay(dec, D("3100.00"), date(2026, 12, 2))
    assert paid.ok
    fields = {name: getattr(lease, name) for name in leases.LEASE_FIELDS}
    leases.update_lease(
        lease.pk,
        {**fields, "free_rent_days": 40},
        rent=D("3100.00"),
        rent_effective_from=None,
        actor=None,
        today=date(2027, 1, 5),
    )
    lease = Lease.objects.get(pk=lease.pk)
    dec, jan = fresh_charge(dec), fresh_charge(jan)
    assert dec.concession == D("0.00")  # billed and paid: kept
    assert jan.concession == (D("3100.00") * 9 / 31).quantize(D("0.01"), ROUND_HALF_UP)
    kept = free_rent.kept(lease)
    assert kept == (dec,)
    assert free_rent.Recomputed((), kept).warning == (
        "Dec 2026 already billed: it keeps the free rent it was billed with."
    )
    audit = AuditLog.objects.get(action=leases.FREE_RENT_SET_ACTION)
    assert (audit.before["free_rent_days"], audit.after["free_rent_days"]) == (0, 40)
    assert AuditLog.objects.filter(
        action=free_rent.RECOMPUTED_ACTION, entity_id=str(jan.pk)
    ).exists()


# --- the invoice builder's discount line and the provider body ----------------------------------


def test_a_rent_line_with_free_rent_shows_a_discount_line_and_bills_the_net(
    fake: FakeProvider, worker: None
) -> None:
    operator = an_operator(fake)
    lease = with_free_rent(a_rent_lease(operator), 30)  # Oct 14 – Nov 12
    draft = a_draft(operator, lease, [rent_line(lease, NOVEMBER)])
    preview = invoice_builder.preview(draft, TODAY)
    words = "Free rent per lease agreement (Oct 14 – Nov 12)"
    assert preview.discounts == (invoice_builder.DiscountLine(words, D("400.00")),)
    assert preview.total == D("600.00")
    invoice = sent(draft).invoice
    assert invoice is not None
    assert (invoice.subtotal, invoice.total) == (D("600.00"), D("600.00"))
    rows = list(InvoiceLineItem.objects.filter(invoice=invoice).order_by("position"))
    assert [(row.kind, row.line_total, row.rent_charge_id is not None) for row in rows] == [
        (InvoiceLineKind.CHARGE, D("1000.00"), True),
        (InvoiceLineKind.DISCOUNT, D("400.00"), False),
    ]
    assert rows[1].description == words
    charge = RentCharge.objects.get(lease=lease, period_month=NOVEMBER)
    assert (charge.concession, charge.balance) == (D("400.00"), D("600.00"))
    run_queued_jobs()
    [request] = created_invoices(fake)
    validation.amounts(request.amounts)
    # PLAN-015g §3A: to the processor the line goes at its net, the concession in its details.
    [item] = request.amounts.line_items
    assert (item.quantity, item.unit_price, item.total) == (D(1), D("600.00"), D("600.00"))
    assert item.details == f"Rent $1,000.00 less $400.00: {words}"
    assert request.amounts.total == D("600.00")
    body = mapping.invoice_body(request, "https://example.com/pay")
    assert body["total"] == D("600.00")
    assert body["meta"]["subtotal"] == "600.00"
    [line] = body["meta"]["lineItems"]
    assert (line["quantity"], line["price"], line["total"]) == (D(1), D("600.00"), D("600.00"))
    assert line["details"] == item.details
    assert "discountRate" not in json.dumps(body, default=str)


def test_the_invoice_pdf_shows_the_discount_negative_and_the_net_balance(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake)
    lease = with_free_rent(a_rent_lease(operator), 30)
    invoice = sent(a_draft(operator, lease, [rent_line(lease, NOVEMBER)])).invoice
    assert invoice is not None
    found = invoice_pdf.facts(fresh(invoice))
    assert [(line.amount, line.price) for line in found.lines] == [
        (D("1000.00"), D("1000.00")),
        (D("-400.00"), D("-400.00")),
    ]
    assert sum((line.amount for line in found.lines), D(0)) == found.subtotal == D("600.00")
    assert (found.total, found.balance) == (D("600.00"), D("600.00"))


def test_a_fully_free_rent_alone_invoice_is_refused_with_nothing_sent(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = with_free_rent(a_rent_lease(operator), 60)  # Oct 14 – Dec 12: November free
    draft = a_draft(operator, lease, [rent_line(lease, NOVEMBER)])
    assert invoice_builder.preview(draft, TODAY).total == D("0.00")
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (outcome.ok, outcome.message) == (False, invoice_builder.NOTHING_TO_BILL)
    assert not RentCharge.objects.filter(lease=lease).exists()
    assert not fake.calls


def test_a_line_without_a_discount_sends_no_discount_rate() -> None:
    from payments.provider import Amounts, InvoiceInput, LineItem

    request = InvoiceInput(
        customer_external_id="cus-1",
        amounts=Amounts(D("100.00"), D("100.00"), D(0), (LineItem("Sign", D(1), D("100.00")),)),
        due_on=TODAY,
    )
    [line] = mapping.invoice_body(request, "https://example.com/pay")["meta"]["lineItems"]
    assert "discountRate" not in line


# --- PLAN-015g §3A: the Rent line goes to the processor at its net -------------------------------


def a_stored_line(
    amount: str,
    concession: str,
    text: str = "Free rent per lease agreement (Dec 10 – Jan 8)",
    *,
    quantity: str = "1",
    unit: str | None = None,
    details: str = "",
) -> InvoiceLineItem:
    """An unsaved Rent line item of ``amount`` whose charge carries ``concession``."""
    rent = RentCharge(amount_due=D(amount), concession=D(concession), concession_text=text)
    return InvoiceLineItem(
        description="Rent December 2026, store Store 0101",
        quantity=D(quantity),
        unit_price=D(unit if unit is not None else amount),
        line_total=D(amount),
        details=details,
        rent_charge=rent,
    )


def test_the_concession_sentence_names_the_amount_the_concession_and_its_text() -> None:
    item = one_off_invoices._provider_line(a_stored_line("2000.00", "666.67"))
    assert (item.description, item.quantity, item.unit_price, item.total) == (
        "Rent December 2026, store Store 0101",
        D(1),
        D("1333.33"),
        D("1333.33"),
    )
    assert item.details == (
        "Rent $2,000.00 less $666.67: Free rent per lease agreement (Dec 10 – Jan 8)"
    )


def test_an_empty_concession_text_ends_the_sentence_at_the_amount() -> None:
    item = one_off_invoices._provider_line(a_stored_line("2000.00", "666.67", ""))
    assert item.details == "Rent $2,000.00 less $666.67"


def test_the_lines_own_details_come_first_then_the_concession() -> None:
    item = one_off_invoices._provider_line(
        a_stored_line("580.65", "161.29", details="14–31 Oct, 18 of 31 days of $1,000.00 = $580.65")
    )
    assert item.details == (
        "14–31 Oct, 18 of 31 days of $1,000.00 = $580.65; Rent $580.65 less $161.29: "
        "Free rent per lease agreement (Dec 10 – Jan 8)"
    )
    assert item.unit_price == D("419.36")


def test_a_line_whose_quantity_is_not_one_goes_as_one_at_its_net() -> None:
    # A builder Rent line is quantity 1; a stored line of half a unit is sent as one, net.
    item = one_off_invoices._provider_line(
        a_stored_line("1000.00", "250.00", quantity="0.5", unit="2000.00")
    )
    assert (item.quantity, item.unit_price, item.total) == (D(1), D("750.00"), D("750.00"))
    assert item.details.startswith("Rent $1,000.00 less $250.00: ")


def test_a_concession_of_one_cent_and_of_the_amount_less_one_cent() -> None:
    small = one_off_invoices._provider_line(a_stored_line("1000.00", "0.01"))
    large = one_off_invoices._provider_line(a_stored_line("1000.00", "999.99"))
    assert (small.unit_price, large.unit_price) == (D("999.99"), D("0.01"))
    amounts = one_off_invoices._line_amounts([a_stored_line("1000.00", "999.99")])
    assert validation.amounts(amounts).total == D("0.01")


def test_a_long_unicode_concession_text_is_carried_whole() -> None:
    # No length rule applies to a line's details at the provider boundary; the text is kept.
    text = ("Loyer gratuit — été ☀ " * 10)[:200]
    item = one_off_invoices._provider_line(a_stored_line("1000.00", "100.00", text))
    assert item.details == f"Rent $1,000.00 less $100.00: {text}"
    assert len(text) == 200


def test_a_line_without_a_concession_is_built_as_before() -> None:
    item = one_off_invoices._provider_line(a_stored_line("1000.00", "0.00", "", details="x"))
    assert (item.quantity, item.unit_price, item.details) == (D("1"), D("1000.00"), "x")


def test_a_net_zero_rent_line_is_refused_before_anything_is_sent() -> None:
    # A Rent line free in full beside a billed line (the builder lets the invoice through: its
    # net is positive) is refused by the provider-side validation; nothing is sent.
    sign = InvoiceLineItem(
        description="Sign", quantity=D(1), unit_price=D("100.00"), line_total=D("100.00")
    )
    amounts = one_off_invoices._line_amounts([sign, a_stored_line("580.65", "580.65")])
    assert (amounts.total, amounts.line_items[1].unit_price) == (D("100.00"), D("0.00"))
    rig = make_rig(always(200, {"id": "inv-1", "status": "DRAFT"}))
    request = InvoiceInput("cus-1", amounts, due_on=TODAY)
    with pytest.raises(ValidationFailed, match="unit_price must be greater than zero"):
        StaxProvider(rig.transport).create_invoice(request)
    assert rig.recorder.requests == []


def test_a_mixed_invoice_keeps_the_one_off_line_and_sums_to_the_net(
    fake: FakeProvider, worker: None
) -> None:
    operator = an_operator(fake)
    lease = with_free_rent(a_rent_lease(operator), 5)  # Oct 14 – Oct 18
    draft = a_draft(operator, lease, [line("Sign", "100.00", description="Own"), rent_line(lease)])
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    [request] = created_invoices(fake)
    sign, rent = request.amounts.line_items
    assert (sign.description, sign.quantity, sign.unit_price, sign.details) == (
        "Sign",
        D("1"),
        D("100.00"),
        "Own",
    )
    assert (rent.quantity, rent.unit_price) == (D(1), D("419.36"))
    assert rent.details == (
        "14–31 Oct, 18 of 31 days of $1,000.00 = $580.65; Rent $580.65 less $161.29: "
        "Free rent per lease agreement (Oct 14 – Oct 18)"
    )
    validation.amounts(request.amounts)
    assert request.amounts.total == request.amounts.subtotal == fresh(invoice).total == D("519.36")
    body = mapping.invoice_body(request, "https://example.com/pay")
    assert body["total"] == D("519.36") and body["meta"]["subtotal"] == "519.36"
    assert [line["total"] for line in body["meta"]["lineItems"]] == [D("100.00"), D("419.36")]
    assert "discountRate" not in json.dumps(body, default=str)


def test_the_update_body_sends_the_same_net_line() -> None:
    amounts = one_off_invoices._line_amounts([a_stored_line("2000.00", "666.67")])
    body = mapping.invoice_update_body(InvoiceUpdate(amounts=amounts))
    assert body["total"] == D("1333.33")
    [line] = body["meta"]["lineItems"]
    assert (line["quantity"], line["price"], line["total"]) == (D(1), D("1333.33"), D("1333.33"))
    assert line["details"].startswith("Rent $2,000.00 less $666.67: ")
    assert "discountRate" not in json.dumps(body, default=str)


def test_nothing_in_the_app_sends_or_reads_a_discount_rate() -> None:
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    hits = [
        str(path.relative_to(backend))
        for path in backend.rglob("*.py")
        if "tests" not in path.parts
        and (
            "discountRate" in (text := path.read_text(encoding="utf-8")) or "discount_rate" in text
        )
    ]
    assert hits == []
